"""Rescue outcome sink — 把 LLM rescue 的結果分類後 emit 給觀察層（slice 3）。

設計目標：
LLM 改寫後重投 bus 的結果是 regex 強化的金礦，但只用 log 很難 mine。
這層把每次 rescue 的結果結構化寫成 record，分類成：

  - "convergent" : 改寫後命中既有 regex agent + 無 pragmatic 落差
                   → daily ritual clustering 後可提案擴充 IntentSchema patterns
  - "divergent"  : 改寫後命中 + 有 pragmatic_signal（positive/negative）
                   → 永遠不該變 regex，餵推薦系統 (skip 訊號歸因延伸)
  - "unmatched"  : rescue 重投仍無 winner（LLM 改寫品質差 / 完全沒對應 agent）
                   → 落回原 agent_gaps.jsonl 路徑
  - "shadow"     : shadow_mode=True 期間（synthesize 跑了但沒重投，校準週用）

contract（這個 slice 只測 emit 行為）：
- sink 是 sync callable(dict)；bus try/except 包好不斷 wake path
- record 必有：original_query / rewritten_query / gap_class / speaker / ts
- 命中 (gap_class != unmatched/shadow) 時帶 winner_agent / winner_reason
- 有 pragmatic_signal/target 一律穿透到 record
- shadow 與真實重投走同一個 sink（用 gap_class 區分），下游分析統一入口
"""
from __future__ import annotations

import logging
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from intent_bus import Bid, IntentBus, IntentContext


def _ctx(query="希望下次可以找到好聽的歌"):
    return IntentContext(
        speaker="Alice", raw_text=query, query=query, original_raw=query,
        wake_intent=0.9, stream_active=False, game_mode=False,
        is_owner=False, now=12345.0, mode="normal", depth=0,
    )


class _StubAgent:
    def __init__(self, name, bid_fn):
        self.name = name
        self._bid_fn = bid_fn
    def bid(self, ctx):
        return self._bid_fn(ctx)


class _StubRescue:
    name = "LLMRescue"
    def __init__(self, result):
        self._result = result
    async def synthesize(self, ctx):
        if isinstance(self._result, BaseException):
            raise self._result
        return self._result


def _winner_only_on_rewrite(rewritten_query: str, agent_name="skip", reason="skip"):
    """Bid factory：只在 ctx.query == rewritten_query 時上 0.9，否則 dense 0.0。"""
    def _fn(ctx):
        if ctx.query == rewritten_query:
            return Bid(name=agent_name, confidence=0.9, handler=AsyncMock(), reason=reason)
        return Bid(name=agent_name, confidence=0.0, handler=AsyncMock(), reason="no_match")
    return _fn


# ── gap_class 分類 ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_convergent_when_rewrite_matches_and_no_pragmatic_signal():
    """rescue 命中 regex agent + 無 pragmatic divergence → regex 可挖掘的訊號。"""
    rescued = replace(_ctx(), query="下一首", depth=1, dispatch_source="llm_rescue",
                      pragmatic_signal=None, pragmatic_target=None)
    records = []
    bus = IntentBus(
        [_StubAgent("skip", _winner_only_on_rewrite("下一首"))],
        llm_rescue_agent=_StubRescue(rescued),
        rescue_outcome_sink=records.append,
    )
    await bus.dispatch(_ctx())

    assert len(records) == 1
    r = records[0]
    assert r["gap_class"] == "convergent"
    assert r["winner_agent"] == "skip"
    assert r["pragmatic_signal"] is None


@pytest.mark.asyncio
async def test_divergent_when_rewrite_matches_with_negative_pragmatic_signal():
    """字面正向 → 真意對 current_song 負向；命中後仍標 divergent，餵推薦不餵 regex。"""
    rescued = replace(_ctx(), query="下一首", depth=1, dispatch_source="llm_rescue",
                      pragmatic_signal="negative", pragmatic_target="current_song")
    records = []
    bus = IntentBus(
        [_StubAgent("skip", _winner_only_on_rewrite("下一首"))],
        llm_rescue_agent=_StubRescue(rescued),
        rescue_outcome_sink=records.append,
    )
    await bus.dispatch(_ctx())

    assert records[0]["gap_class"] == "divergent"
    assert records[0]["pragmatic_signal"] == "negative"
    assert records[0]["pragmatic_target"] == "current_song"


@pytest.mark.asyncio
async def test_neutral_signal_treated_as_convergent_not_divergent():
    """neutral 不是落差 — 只 positive/negative 才走 divergent bin。"""
    rescued = replace(_ctx(), query="下一首", depth=1, dispatch_source="llm_rescue",
                      pragmatic_signal="neutral", pragmatic_target=None)
    records = []
    bus = IntentBus(
        [_StubAgent("skip", _winner_only_on_rewrite("下一首"))],
        llm_rescue_agent=_StubRescue(rescued),
        rescue_outcome_sink=records.append,
    )
    await bus.dispatch(_ctx())
    assert records[0]["gap_class"] == "convergent"


@pytest.mark.asyncio
async def test_unmatched_when_rewrite_still_finds_no_winner():
    """rescue 改寫了但 regex 仍沒人接 → unmatched，落回 agent_gaps.jsonl 路徑。"""
    rescued = replace(_ctx(), query="完全不對的改寫", depth=1, dispatch_source="llm_rescue")

    def _all_zero(ctx):
        return Bid(name="z", confidence=0.0, handler=AsyncMock(), reason="no_match")

    records = []
    bus = IntentBus(
        [_StubAgent("z", _all_zero)],
        llm_rescue_agent=_StubRescue(rescued),
        rescue_outcome_sink=records.append,
    )
    await bus.dispatch(_ctx())

    assert len(records) == 1
    assert records[0]["gap_class"] == "unmatched"
    assert records[0]["winner_agent"] is None


@pytest.mark.asyncio
async def test_shadow_mode_emits_with_shadow_class_and_no_winner():
    """shadow mode：synthesize 跑了 + 寫 record（收數據），但 winner 永遠 None。"""
    rescued = replace(_ctx(), query="下一首", depth=1, dispatch_source="llm_rescue",
                      pragmatic_signal="negative", pragmatic_target="current_song")

    handler = AsyncMock()

    def _would_match(ctx):
        # 若 shadow 不小心重投了，handler 會被叫 → 失敗
        if ctx.query == "下一首":
            return Bid(name="skip", confidence=0.9, handler=handler, reason="skip")
        return Bid(name="skip", confidence=0.0, handler=AsyncMock(), reason="no_match")

    records = []
    bus = IntentBus(
        [_StubAgent("skip", _would_match)],
        llm_rescue_agent=_StubRescue(rescued),
        rescue_shadow_mode=True,
        rescue_outcome_sink=records.append,
    )
    await bus.dispatch(_ctx())

    handler.assert_not_awaited()
    assert records[0]["gap_class"] == "shadow"
    assert records[0]["winner_agent"] is None
    # shadow 仍要保留 pragmatic 欄位 — 校準週要分析這些
    assert records[0]["pragmatic_signal"] == "negative"


# ── record schema 完整性 ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_record_contains_required_fields_for_offline_analysis():
    """daily ritual 要能 join speaker + ts 看人/時間分佈，必要欄位一個都不能少。"""
    rescued = replace(_ctx(), query="下一首", depth=1, dispatch_source="llm_rescue")
    records = []
    bus = IntentBus(
        [_StubAgent("skip", _winner_only_on_rewrite("下一首"))],
        llm_rescue_agent=_StubRescue(rescued),
        rescue_outcome_sink=records.append,
    )
    await bus.dispatch(_ctx())

    r = records[0]
    for field in ("original_query", "rewritten_query", "gap_class",
                  "speaker", "ts", "winner_agent", "winner_reason",
                  "pragmatic_signal", "pragmatic_target"):
        assert field in r, f"record 缺欄位 {field}"
    assert r["original_query"] == "希望下次可以找到好聽的歌"
    assert r["rewritten_query"] == "下一首"
    assert r["speaker"] == "Alice"


# ── 短路：沒有 rescue 發生 → 不該 emit ──────────────────────────────────────

@pytest.mark.asyncio
async def test_no_emit_when_regex_wins_directly():
    """正常 regex 路徑（無 rescue）→ outcome sink 完全不該被觸發。
    這個 sink 是 rescue-specific 觀察層，不是 dispatch 廣播。"""
    records = []
    bus = IntentBus(
        [_StubAgent("skip", lambda ctx: Bid(name="skip", confidence=0.9,
                                            handler=AsyncMock(), reason="skip"))],
        llm_rescue_agent=_StubRescue(None),  # 注入但不會被叫到
        rescue_outcome_sink=records.append,
    )
    await bus.dispatch(_ctx(query="下一首"))
    assert records == []


@pytest.mark.asyncio
async def test_no_emit_when_synthesize_returns_none():
    """LLM 信心不夠 / 主動拒絕 → synthesize 回 None → 不算一次「rescue 嘗試」，不寫 record。
    （否則低信心 / 短句噪音會塞爆 jsonl）"""
    def _zero(ctx):
        return Bid(name="z", confidence=0.0, handler=AsyncMock(), reason="no_match")

    records = []
    bus = IntentBus(
        [_StubAgent("z", _zero)],
        llm_rescue_agent=_StubRescue(None),
        rescue_outcome_sink=records.append,
    )
    await bus.dispatch(_ctx())
    assert records == []


@pytest.mark.asyncio
async def test_no_emit_when_synthesize_raises():
    """synthesize 炸了 → 連 record 都寫不出來，與「沒嘗試」一致語意。"""
    def _zero(ctx):
        return Bid(name="z", confidence=0.0, handler=AsyncMock(), reason="no_match")

    records = []
    bus = IntentBus(
        [_StubAgent("z", _zero)],
        llm_rescue_agent=_StubRescue(RuntimeError("LLM down")),
        rescue_outcome_sink=records.append,
    )
    await bus.dispatch(_ctx())
    assert records == []


# ── 容錯 + 向後相容 ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sink_exception_does_not_crash_bus(caplog):
    """sink 自己炸了（jsonl write 失敗 / disk full）→ WARNING log 但 bus 繼續走。"""
    rescued = replace(_ctx(), query="下一首", depth=1, dispatch_source="llm_rescue")

    def _bad_sink(record):
        raise IOError("disk full")

    bus = IntentBus(
        [_StubAgent("skip", _winner_only_on_rewrite("下一首"))],
        llm_rescue_agent=_StubRescue(rescued),
        rescue_outcome_sink=_bad_sink,
    )
    with caplog.at_level(logging.WARNING, logger="cogs.voice_controller.intent_bus"):
        winner = await bus.dispatch(_ctx())

    # rescue 重投仍要成功（sink 失敗不能拖垮 dispatch）
    assert winner is not None
    assert winner.name == "skip"
    assert any("sink" in r.message.lower() for r in caplog.records)


@pytest.mark.asyncio
async def test_no_sink_configured_uses_no_op():
    """未注入 sink（既有 prod 設定）→ rescue 路徑照走，只是不寫 record。"""
    rescued = replace(_ctx(), query="下一首", depth=1, dispatch_source="llm_rescue")
    bus = IntentBus(
        [_StubAgent("skip", _winner_only_on_rewrite("下一首"))],
        llm_rescue_agent=_StubRescue(rescued),
        # 不傳 rescue_outcome_sink
    )
    winner = await bus.dispatch(_ctx())
    assert winner is not None  # 不該因為缺 sink 而壞掉
