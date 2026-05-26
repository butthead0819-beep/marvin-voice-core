"""Shadow-mode integration helpers — bridge race coordinator to voice_controller.

shadow mode 設計：
  - J1: regex_judge on raw STT 文字
  - J3: cleaner_judge with **precomputed cleaner**（cleaner_call 直接回 caller 已
        clean 過的字串）→ 零額外 LLM
  - J2: 暫不啟（避免每 utterance 多打 Groq；收夠 outcome data 再決定）
  - 結果寫 records/judge_outcomes.jsonl，不影響現行 dispatch

把 race+telemetry wiring 包成兩個 helper：`make_shadow_specs` 與
`run_shadow_race`。voice_controller 只要一行 create_task 呼叫 run_shadow_race。
"""
from __future__ import annotations

import pytest

from intent_agents.base import DeclarativeIntentAgent, IntentSchema
from intent_bus import IntentContext
from intent_judges.race import race
from intent_judges.voice_integration import (
    make_shadow_specs,
    new_utterance_id,
    run_shadow_race,
)

pytestmark = pytest.mark.asyncio


class _StubAgent(DeclarativeIntentAgent):
    def __init__(self, name, patterns,
                 mode_compatible=frozenset({"normal"})):
        self.name = name
        self.mode_compatible = mode_compatible
        self._schemas = [
            IntentSchema(f"{name}_intent_{i}", conf, [pat])
            for i, (pat, conf) in enumerate(patterns)
        ]

    def declare_intents(self):
        return self._schemas


def _ctx(query: str = "打開 YouTube", raw_text: str = None) -> IntentContext:
    return IntentContext(
        speaker="alice",
        raw_text=raw_text if raw_text is not None else query,
        query=query,
        original_raw=raw_text,
        wake_intent=0.9, stream_active=False, game_mode=False, is_owner=False,
        now=0.0, mode="normal",
    )


# ── make_shadow_specs ─────────────────────────────────────────────────────


async def test_make_shadow_specs_returns_two_named_specs():
    specs = make_shadow_specs("raw", "cleaned", [])
    assert len(specs) == 2
    assert {s.name for s in specs} == {"j1_regex", "j3_cleaner_precomputed"}


async def test_shadow_specs_j1_matches_raw_stt_text():
    """J1 必須跑在 raw STT 上；caller 傳入的 ctx.query 是 cleaned text 也不影響。"""
    music = _StubAgent("music", [("打開那個影片", 0.95)])
    # 模擬 prod：voice_controller 給的 ctx 已是 cleaned ctx（query="打開 YouTube"）
    ctx = _ctx(query="打開 YouTube")
    specs = make_shadow_specs(
        raw_text="打開那個影片", cleaned_text="打開 YouTube",
        agents=[music],
    )
    result = await race(ctx, specs)
    # J1 內部 replace 成 raw → 命中「打開那個影片」
    assert result.winning_judge == "j1_regex"
    assert result.winner.name == "music"


async def test_shadow_specs_j3_uses_precomputed_cleaned_text():
    """raw 不命中、cleaned 命中 → J3 應該贏。J3 不該再呼叫真 cleaner。"""
    music = _StubAgent("music", [("打開 YouTube", 0.95)])
    ctx = _ctx(query="打開 YouTube")  # prod 傳的 cleaned ctx
    specs = make_shadow_specs(
        raw_text="嗯打...那個", cleaned_text="打開 YouTube",
        agents=[music],
    )
    result = await race(ctx, specs)
    assert result.winning_judge == "j3_cleaner_precomputed"
    assert result.winner.name == "music"


async def test_shadow_specs_both_miss_returns_dense_zero():
    music = _StubAgent("music", [("播放.*", 0.95)])
    ctx = _ctx(query="今天天氣不錯")
    specs = make_shadow_specs("今天天氣不錯", "今天天氣不錯", [music])
    result = await race(ctx, specs)
    assert result.winner.confidence == 0.0
    assert len(result.outcomes) == 2
    assert all(o.status == "completed" for o in result.outcomes)


# ── new_utterance_id ──────────────────────────────────────────────────────


async def test_new_utterance_id_contains_speaker():
    uid = new_utterance_id("alice")
    assert "alice" in uid


async def test_new_utterance_id_truncates_long_speaker():
    """speaker 名很長（如 Discord 用戶 ID）→ 截斷避免 utt_id 爆炸。"""
    uid = new_utterance_id("a" * 100)
    assert len(uid) < 50  # 寬鬆 bound，主要是不能無限長


async def test_new_utterance_id_handles_empty_speaker():
    uid = new_utterance_id("")
    assert uid  # 不該回空字串


async def test_new_utterance_ids_are_unique_in_tight_loop():
    """ns timestamp 在 μs 解析度系統會碰撞，counter 保障 process-local unique。"""
    ids = {new_utterance_id("alice") for _ in range(100)}
    assert len(ids) == 100


# ── run_shadow_race (end-to-end with jsonl write) ─────────────────────────


async def test_run_shadow_race_writes_outcome_jsonl(tmp_path):
    """E2E：run_shadow_race 跑完整 race 並 append 一行到指定 jsonl。"""
    music = _StubAgent("music", [("打開.*", 0.95)])
    out_path = tmp_path / "outcomes.jsonl"
    ctx = _ctx(query="打開 YouTube", raw_text="打開 YouTube")
    await run_shadow_race(
        ctx=ctx,
        raw_text="打開 YouTube",
        cleaned_text="打開 YouTube",
        agents=[music],
        utterance_id="utt-1",
        outcome_path=out_path,
    )
    assert out_path.exists()
    lines = out_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert "utt-1" in lines[0]


async def test_run_shadow_race_does_not_raise_on_agent_exception(tmp_path):
    """shadow mode 絕對不能炸到 voice_controller —— agent bid 例外要被吞。"""
    class _Boom(DeclarativeIntentAgent):
        name = "boom"
        mode_compatible = frozenset({"normal"})

        def bid(self, ctx):
            raise RuntimeError("agent broke")

    music = _StubAgent("music", [("打開.*", 0.95)])
    out_path = tmp_path / "outcomes.jsonl"
    ctx = _ctx(query="打開 X", raw_text="打開 X")
    # 不該 raise
    await run_shadow_race(
        ctx=ctx, raw_text="打開 X", cleaned_text="打開 X",
        agents=[_Boom(), music], utterance_id="utt-1",
        outcome_path=out_path,
    )


async def test_run_shadow_race_excludes_guard_from_fast_path(tmp_path):
    """議題 A：J1 回 guard（empty_after_strip / wake_loop）→ J3 找到真 intent
    才該贏。Shadow 路徑必須傳 fast_path_excludes={'guard'}。"""
    # guard 只在短 raw（如 "麻煩"）觸發，模擬真實 empty_after_strip 行為
    class _Guard(DeclarativeIntentAgent):
        name = "guard"
        mode_compatible = frozenset({"normal"})

        def bid(self, ctx):
            from intent_bus import Bid
            async def _noop():
                pass
            if len((ctx.raw_text or "").strip()) <= 3:
                return Bid(name="guard", confidence=0.96, handler=_noop,
                           reason="empty_after_strip")
            return Bid(name="guard", confidence=0.0, handler=_noop,
                       reason="not_short")

    # music agent — cleaned text 命中
    music = _StubAgent("music", [("播放.*", 0.95)])
    out_path = tmp_path / "outcomes.jsonl"
    ctx = _ctx(query="麻煩播放孤勇者", raw_text="麻煩")
    await run_shadow_race(
        ctx=ctx,
        raw_text="麻煩",  # J1 看 raw，guard 攔下
        cleaned_text="麻煩播放孤勇者",  # J3 看 cleaned，music 命中
        agents=[_Guard(), music],
        utterance_id="utt-guard",
        outcome_path=out_path,
    )
    import json
    line = out_path.read_text(encoding="utf-8").strip()
    record = json.loads(line)
    # 即使 J1 的 guard 0.96 過 threshold，也不該 fast-path
    assert record["winner_name"] == "music", (
        f"guard 不該觸發 fast-path；實際 winner={record['winner_name']}"
    )


async def test_run_shadow_race_swallows_write_failure(tmp_path, monkeypatch):
    """jsonl 寫入失敗（disk full / permission）也不能炸 voice_controller。"""
    music = _StubAgent("music", [("打開.*", 0.95)])
    bad_path = tmp_path / "outcomes.jsonl"

    def _boom(*args, **kwargs):
        raise PermissionError("disk write blocked")

    monkeypatch.setattr(
        "intent_judges.voice_integration.write_race_outcome", _boom,
    )
    ctx = _ctx(query="打開 X", raw_text="打開 X")
    # 不該 raise
    await run_shadow_race(
        ctx=ctx, raw_text="打開 X", cleaned_text="打開 X",
        agents=[music], utterance_id="utt-1", outcome_path=bad_path,
    )
