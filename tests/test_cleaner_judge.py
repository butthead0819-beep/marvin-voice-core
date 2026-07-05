"""J3 ClenerJudge unit tests — cleaner adapter + J1 reuse, mocked cleaner.

J3 是 race 的 slow fallback：
  1. raw STT → cleaner_call(ctx) → cleaned_text
  2. dataclasses.replace(ctx, query=cleaned, raw_text=cleaned)
  3. regex_judge(cleaned_ctx, agents) → J1 風格 Bid（含 handler）
  4. 直接回 J1 信心（cleaner 不自報信心 → 沒 cap 邏輯）

cleaner_call 用 DI 注入，prod 接現有 stt_cleaner.py，test 傳 fake，零外部依賴。
"""
from __future__ import annotations

import asyncio
import types

import pytest

from intent_agents.base import DeclarativeIntentAgent, IntentSchema
from intent_agents.hallucination_guard_agent import HallucinationGuardAgent
from intent_bus import IntentContext
from intent_judges.cleaner_judge import cleaner_judge

pytestmark = pytest.mark.asyncio


def _real_guard() -> HallucinationGuardAgent:
    """真 guard，掛空 kw 列表的 stub controller（避免行為 drift）。"""
    ctrl = types.SimpleNamespace(_STRONG_PLAY_KW=[], _WEAK_PLAY_KW=[])
    return HallucinationGuardAgent(ctrl)


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


def _ctx(query: str = "嗯打...打開那個", mode: str = "normal",
         wake_intent: float | None = 0.9, speaker: str = "alice") -> IntentContext:
    return IntentContext(
        speaker=speaker, raw_text=query, query=query, original_raw=query,
        wake_intent=wake_intent, stream_active=False, game_mode=False,
        is_owner=False, now=0.0, mode=mode,
    )


def _fake_cleaner(cleaned: str, delay_ms: int = 0):
    async def _call(ctx):
        if delay_ms:
            await asyncio.sleep(delay_ms / 1000)
        return cleaned
    return _call


# ── happy path ────────────────────────────────────────────────────────────


async def test_cleaner_judge_returns_bid_when_cleaned_matches_regex():
    music = _StubAgent("music", [("打開.*", 0.95)])
    bid = await cleaner_judge(
        _ctx("嗯打...打開那個"), [music],
        cleaner_call=_fake_cleaner("打開 YouTube"),
    )
    assert bid.name == "music"
    assert bid.confidence == 0.95  # 直接走 J1 信心，沒 cap


async def test_cleaner_judge_returns_winning_handler_for_dispatch():
    music = _StubAgent("music", [("打開.*", 0.95)])
    bid = await cleaner_judge(
        _ctx(), [music], cleaner_call=_fake_cleaner("打開 YouTube"),
    )
    assert callable(bid.handler)


async def test_cleaner_judge_works_when_cleaner_returns_same_text():
    """no-op cleaner（text 已經乾淨）→ 仍走 regex 拿 Bid。"""
    music = _StubAgent("music", [("打開.*", 0.95)])
    bid = await cleaner_judge(
        _ctx("打開 YouTube"), [music],
        cleaner_call=_fake_cleaner("打開 YouTube"),
    )
    assert bid.confidence == 0.95


# ── miss paths ────────────────────────────────────────────────────────────


async def test_cleaner_judge_returns_dense_zero_when_cleaned_misses_regex():
    music = _StubAgent("music", [("播放.*", 0.95)])
    bid = await cleaner_judge(
        _ctx(), [music], cleaner_call=_fake_cleaner("今天天氣不錯"),
    )
    assert bid.confidence == 0.0


async def test_cleaner_judge_returns_dense_zero_when_cleaner_drops_empty():
    """cleaner 認定是 hallucination 回空字串 → dense zero。"""
    music = _StubAgent("music", [(".*", 0.95)])
    bid = await cleaner_judge(
        _ctx(), [music], cleaner_call=_fake_cleaner(""),
    )
    assert bid.confidence == 0.0


async def test_cleaner_judge_returns_dense_zero_when_cleaner_returns_whitespace():
    music = _StubAgent("music", [(".*", 0.95)])
    bid = await cleaner_judge(
        _ctx(), [music], cleaner_call=_fake_cleaner("   \t"),
    )
    assert bid.confidence == 0.0


async def test_cleaner_judge_returns_dense_zero_on_cleaner_exception():
    music = _StubAgent("music", [("打開.*", 0.95)])

    async def _broken(ctx):
        raise RuntimeError("cleaner LLM down")

    bid = await cleaner_judge(_ctx(), [music], cleaner_call=_broken)
    assert bid.confidence == 0.0


async def test_cleaner_judge_returns_dense_zero_on_cleaner_timeout():
    music = _StubAgent("music", [("打開.*", 0.95)])
    bid = await cleaner_judge(
        _ctx(), [music],
        cleaner_call=_fake_cleaner("打開 YouTube", delay_ms=500),
        timeout_s=0.05,
    )
    assert bid.confidence == 0.0


# ── short-circuit ─────────────────────────────────────────────────────────


async def test_cleaner_judge_skips_cleaner_on_empty_query():
    called = {"n": 0}

    async def _cleaner(ctx):
        called["n"] += 1
        return "打開 X"

    bid = await cleaner_judge(_ctx(""), [], cleaner_call=_cleaner)
    assert bid.confidence == 0.0
    assert called["n"] == 0  # 不該打 cleaner


# ── ctx 透傳 ──────────────────────────────────────────────────────────────


async def test_cleaner_judge_preserves_ctx_metadata_when_cleaning():
    captured = {}

    class _Recorder(DeclarativeIntentAgent):
        name = "rec"
        mode_compatible = frozenset({"stream"})

        def declare_intents(self):
            return [IntentSchema("rec_intent", 0.95, ["打開.*"])]

        def gate(self, ctx):
            captured["mode"] = ctx.mode
            captured["wake_intent"] = ctx.wake_intent
            captured["speaker"] = ctx.speaker
            return None

    ctx = _ctx(query="x", mode="stream", wake_intent=0.7, speaker="bob")
    await cleaner_judge(
        ctx, [_Recorder()], cleaner_call=_fake_cleaner("打開 X"),
    )
    assert captured["mode"] == "stream"
    assert captured["wake_intent"] == 0.7
    assert captured["speaker"] == "bob"


# ── cleaner over-collapse guard（cleaner 別毀 query / guard 別誤壓）────────────


async def test_cleaner_overcollapse_to_wakeword_does_not_swallow():
    """cleaner 把有內容的問句塌縮成只剩喚醒詞 → guard 出價 swallow，但那是 cleaner
    artifact 不是真幻覺（原句有內容）→ cleaner_judge 回 dense_zero，不傳播 swallow，
    讓 race 落回 J1 / Marvin fallback。這是本機 Phase 1 的確定性靜默 blocker。"""
    music = _StubAgent("music", [("播放.*", 0.95)])
    bid = await cleaner_judge(
        _ctx("講個笑話給我聽"), [_real_guard(), music],
        cleaner_call=_fake_cleaner("馬文"),
    )
    assert bid.confidence == 0.0
    assert bid.name != "guard"
    assert "overcollapse" in bid.reason.lower()


async def test_cleaner_keeps_guard_swallow_when_original_is_wake_only():
    """原句本身就是純喚醒碎片（真幻覺）→ cleaner 回喚醒詞屬正確，guard swallow 應保留。"""
    music = _StubAgent("music", [("播放.*", 0.95)])
    bid = await cleaner_judge(
        _ctx("馬文"), [_real_guard(), music],
        cleaner_call=_fake_cleaner("馬文"),
    )
    assert bid.name == "guard"
    assert bid.confidence == 0.96


# ── telemetry ─────────────────────────────────────────────────────────────


async def test_cleaner_judge_attaches_cleaning_to_reason_for_telemetry():
    music = _StubAgent("music", [("打開.*", 0.95)])
    bid = await cleaner_judge(
        _ctx("嗯打...打開那個"), [music],
        cleaner_call=_fake_cleaner("打開 YouTube"),
    )
    assert "嗯打...打開那個" in bid.reason
    assert "打開 YouTube" in bid.reason
