"""IntentBus ↔ LLMRescueAgent wiring tests (slice 2)。

當 bus 收不到 above-threshold 的 bid → 呼叫注入的 LLMRescueAgent.synthesize()，
拿到 enriched ctx → 重投 dispatch()。

契約：
- 只在 ctx.depth == 0 時嘗試 rescue（避免 rescue→rescue 無窮迴圈）
- shadow_mode=True：synthesize 跑但不重投（只 log，給校準週用）
- rescue agent 例外 / 回 None / 重投仍無 winner → bus 仍回 None，caller 自處
- LLMRescueAgent=None（未注入）→ 完全等同 slice 1 行為
"""
from __future__ import annotations

import logging
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from intent_bus import Bid, IntentBus, IntentContext


def _ctx(query="希望下次可以找到好聽的歌", depth=0):
    return IntentContext(
        speaker="Alice", raw_text=query, query=query, original_raw=query,
        wake_intent=0.9, stream_active=False, game_mode=False,
        is_owner=False, now=0.0, mode="normal", depth=depth,
    )


class _StubAgent:
    """Minimal IntentAgent stub — bid() 回 caller 指定的 Bid。"""
    def __init__(self, name, bid_fn):
        self.name = name
        self._bid_fn = bid_fn
    def bid(self, ctx):
        return self._bid_fn(ctx)


class _StubRescue:
    """Minimal LLMRescueAgent stub — synthesize() 回 caller 指定 ctx (or None)。

    跟 bus 之間的契約只有 `async synthesize(ctx) -> IntentContext | None`，
    bus 不該耦合 LLMRescueAgent 的內部結構。
    """
    name = "LLMRescue"
    def __init__(self, result):
        self._result = result
        self.calls: list[IntentContext] = []
    async def synthesize(self, ctx):
        self.calls.append(ctx)
        if isinstance(self._result, BaseException):
            raise self._result
        return self._result


# ── happy path：no winner → rescue → re-dispatch 找到 winner ────────────────

@pytest.mark.asyncio
async def test_rescue_triggers_when_no_bids_above_threshold():
    """所有 agent 都 dense 0.0 → bus 觸發 rescue，重投讓 regex agent 命中。"""
    handler_called = AsyncMock()

    def _zero_bid(ctx):
        return Bid(name="zero", confidence=0.0, handler=AsyncMock(), reason="no_match")

    def _rewrite_match(ctx):
        # 只在 rewritten query 命中；原 query 走 dense 0.0
        if ctx.query == "下一首":
            return Bid(name="skip", confidence=0.9, handler=handler_called, reason="skip")
        return Bid(name="skip", confidence=0.0, handler=AsyncMock(), reason="no_match")

    rescued_ctx = replace(
        _ctx(), query="下一首", depth=1, dispatch_source="llm_rescue",
        pragmatic_signal="negative", pragmatic_target="current_song",
    )
    rescue = _StubRescue(result=rescued_ctx)
    bus = IntentBus(
        [_StubAgent("zero", _zero_bid), _StubAgent("skip", _rewrite_match)],
        llm_rescue_agent=rescue,
    )

    winner = await bus.dispatch(_ctx())

    assert winner is not None
    assert winner.name == "skip"
    handler_called.assert_awaited_once()
    assert len(rescue.calls) == 1
    assert rescue.calls[0].query == "希望下次可以找到好聽的歌"


@pytest.mark.asyncio
async def test_rescue_triggers_when_no_agent_bids_at_all():
    """空 bid list 也算 no winner（不只是 below threshold），同樣該觸發 rescue。"""
    class _SilentAgent:
        name = "silent"
        def bid(self, ctx):
            return None  # 不出價（雖然違反 dense bid 慣例，但 bus 仍要能處理）

    rescue = _StubRescue(result=None)
    bus = IntentBus([_SilentAgent()], llm_rescue_agent=rescue)
    await bus.dispatch(_ctx())
    assert len(rescue.calls) == 1


# ── 短路：有 winner 不該 rescue ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rescue_not_called_when_winner_exists():
    """正常 regex 路徑（有 above-threshold winner）→ 完全不該碰 LLM rescue。"""
    handler = AsyncMock()

    def _winner_bid(ctx):
        return Bid(name="win", confidence=0.9, handler=handler, reason="match")

    rescue = _StubRescue(result=None)
    bus = IntentBus([_StubAgent("win", _winner_bid)], llm_rescue_agent=rescue)
    await bus.dispatch(_ctx())

    handler.assert_awaited_once()
    assert rescue.calls == []  # rescue 完全沒被呼叫


# ── depth guard：rescue 過的 ctx 不能再 rescue ───────────────────────────────

@pytest.mark.asyncio
async def test_rescue_skipped_when_depth_already_incremented():
    """rescue 重投的 ctx depth>0；若該次仍無 winner，不能再 rescue（無窮迴圈防護）。"""
    def _zero(ctx):
        return Bid(name="zero", confidence=0.0, handler=AsyncMock(), reason="no_match")

    rescue = _StubRescue(result=replace(_ctx(), query="x", depth=1))
    bus = IntentBus([_StubAgent("zero", _zero)], llm_rescue_agent=rescue)

    # depth=1 進來代表這已經是 rescue 後的 ctx；不該再觸發 rescue
    await bus.dispatch(_ctx(depth=1))
    assert rescue.calls == []


# ── shadow mode：跑 LLM 但不重投，只 log ──────────────────────────────────────

@pytest.mark.asyncio
async def test_shadow_mode_calls_rescue_but_does_not_redispatch(caplog):
    """shadow_mode=True：synthesize 被呼叫（收數據），但回的 ctx 不重投 dispatch。
    用於校準週——觀察 LLM 解析品質，但不讓 LLM 影響真實對話路徑。"""
    handler = AsyncMock()

    def _bid_fn(ctx):
        # 若 rescue 真的重投，這個 handler 會被叫到 → 測試會看到 assert 失敗
        if ctx.dispatch_source == "llm_rescue":
            return Bid(name="x", confidence=0.9, handler=handler, reason="match")
        return Bid(name="x", confidence=0.0, handler=AsyncMock(), reason="no_match")

    rescued_ctx = replace(_ctx(), query="下一首", depth=1, dispatch_source="llm_rescue")
    rescue = _StubRescue(result=rescued_ctx)
    bus = IntentBus(
        [_StubAgent("x", _bid_fn)],
        llm_rescue_agent=rescue,
        rescue_shadow_mode=True,
    )

    with caplog.at_level(logging.INFO, logger="cogs.voice_controller.intent_bus"):
        winner = await bus.dispatch(_ctx())

    assert len(rescue.calls) == 1, "shadow mode 仍要呼叫 synthesize 收數據"
    handler.assert_not_awaited()  # 重要：絕對不能重投
    assert winner is None
    # log 要看得出 shadow 跑過了，方便人工比對
    assert any("shadow" in r.message.lower() for r in caplog.records)


# ── 容錯：rescue 失敗不能炸 bus ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rescue_exception_does_not_crash_bus():
    """synthesize 炸了 → bus 回 None（caller 走原本 fallback），不該往上拋。"""
    def _zero(ctx):
        return Bid(name="z", confidence=0.0, handler=AsyncMock(), reason="no_match")

    rescue = _StubRescue(result=RuntimeError("LLM gateway down"))
    bus = IntentBus([_StubAgent("z", _zero)], llm_rescue_agent=rescue)
    winner = await bus.dispatch(_ctx())
    assert winner is None


@pytest.mark.asyncio
async def test_rescue_returning_none_falls_through_to_none_winner():
    """LLM 信心不夠 → synthesize 回 None → bus 也回 None（caller 自處）。"""
    def _zero(ctx):
        return Bid(name="z", confidence=0.0, handler=AsyncMock(), reason="no_match")

    rescue = _StubRescue(result=None)
    bus = IntentBus([_StubAgent("z", _zero)], llm_rescue_agent=rescue)
    assert await bus.dispatch(_ctx()) is None


@pytest.mark.asyncio
async def test_rescue_redispatch_with_still_no_winner_returns_none():
    """rescue 重投後仍沒人贏（LLM 改寫品質差）→ 不無窮迴圈，回 None。"""
    def _zero(ctx):
        # 不管 query 多少都 dense 0.0
        return Bid(name="z", confidence=0.0, handler=AsyncMock(), reason="no_match")

    rescued_ctx = replace(_ctx(), query="下一首", depth=1, dispatch_source="llm_rescue")
    rescue = _StubRescue(result=rescued_ctx)
    bus = IntentBus([_StubAgent("z", _zero)], llm_rescue_agent=rescue)

    winner = await bus.dispatch(_ctx())
    assert winner is None
    assert len(rescue.calls) == 1  # 第二次 dispatch 因 depth>0 不該再 rescue


# ── 向後相容：未注入 rescue_agent ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bus_without_rescue_agent_behaves_as_before():
    """llm_rescue_agent=None（既有 prod 設定）→ no winner 直接回 None，不嘗試 rescue。"""
    def _zero(ctx):
        return Bid(name="z", confidence=0.0, handler=AsyncMock(), reason="no_match")

    bus = IntentBus([_StubAgent("z", _zero)])  # 不傳 llm_rescue_agent
    assert await bus.dispatch(_ctx()) is None
