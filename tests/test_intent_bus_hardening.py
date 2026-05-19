"""TDD：IntentBus Phase 2 hardening — bid budget + tie collision warning.

從 5/18 NotebookLM review 補入。守住：
  P0a. bid() sync 契約：> _BID_BUDGET_MS 的 bid → WARNING log（不中斷）
  P0b. tie 視覺化：winner.confidence == 第二名 → WARNING log
       （目前 tie-break 是註冊順序穩定排序，未來加 agent 容易踩；
        log 出來就能讓 ops 看見隱式行為）
"""
from __future__ import annotations

import logging
import time
from unittest.mock import AsyncMock

import pytest

from intent_bus import IntentBus, IntentContext, Bid


def _ctx():
    return IntentContext(
        speaker="Alice", raw_text="x", query="x", original_raw=None,
        wake_intent=None, stream_active=False, game_mode=False,
        is_owner=False, now=100.0,
    )


class _StubAgent:
    def __init__(self, name, bid_fn):
        self.name = name
        self._bid_fn = bid_fn
    def bid(self, ctx):
        return self._bid_fn(ctx)


def _bid(name, conf):
    return Bid(name=name, confidence=conf, handler=AsyncMock(), reason=name)


# ── P0a: bid 預算偵測 ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_slow_bid_logs_warning(caplog):
    """bid() 跑超過 _BID_BUDGET_MS 要在 log 留 WARNING（守 sync ≤5ms 契約）。"""
    def _slow(ctx):
        time.sleep(0.02)  # 20ms，遠超 5ms 預算
        return _bid("slow", 0.9)

    bus = IntentBus([_StubAgent("slow", _slow)])
    with caplog.at_level(logging.WARNING, logger="cogs.voice_controller.intent_bus"):
        await bus.dispatch(_ctx())

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "slow bid 應該觸發至少一條 WARNING"
    combined = " ".join(r.message for r in warnings)
    assert "slow" in combined
    assert "ms" in combined or "預算" in combined or "budget" in combined.lower()


@pytest.mark.asyncio
async def test_fast_bid_no_warning(caplog):
    """快速 bid（<5ms）不該產 WARNING — 避免 log 噪音。"""
    bus = IntentBus([_StubAgent("fast", lambda c: _bid("fast", 0.9))])
    with caplog.at_level(logging.WARNING, logger="cogs.voice_controller.intent_bus"):
        await bus.dispatch(_ctx())

    bid_warnings = [
        r for r in caplog.records
        if r.levelno >= logging.WARNING
        and ("budget" in r.message.lower() or "預算" in r.message or "took" in r.message.lower())
    ]
    assert not bid_warnings, f"fast bid 不該觸發 budget warning: {[r.message for r in bid_warnings]}"


@pytest.mark.asyncio
async def test_slow_bid_still_counts_toward_winner():
    """budget warning 是 observability，不該影響 winner 選擇。"""
    def _slow(ctx):
        time.sleep(0.01)  # 10ms
        return _bid("slow", 0.95)

    bus = IntentBus([_StubAgent("slow", _slow)])
    winner = await bus.dispatch(_ctx())
    assert winner is not None
    assert winner.name == "slow"


# ── P0b: tie collision warning ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_tied_top_bids_log_warning(caplog):
    """winner.confidence == 第二名 → WARNING，曝光隱式 tie-break。"""
    bus = IntentBus([
        _StubAgent("first",  lambda c: _bid("first",  0.95)),
        _StubAgent("second", lambda c: _bid("second", 0.95)),
    ])
    with caplog.at_level(logging.WARNING, logger="cogs.voice_controller.intent_bus"):
        winner = await bus.dispatch(_ctx())

    assert winner.name == "first"  # 註冊順序穩定排序

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "tied top bids 應該觸發至少一條 WARNING"
    combined = " ".join(r.message for r in warnings)
    assert "first" in combined and "second" in combined
    assert "tie" in combined.lower() or "collision" in combined.lower() or "同分" in combined


@pytest.mark.asyncio
async def test_distinct_top_no_tie_warning(caplog):
    """winner 領先第二名 → 不該觸發 tie warning。"""
    bus = IntentBus([
        _StubAgent("hi",  lambda c: _bid("hi",  0.95)),
        _StubAgent("mid", lambda c: _bid("mid", 0.55)),
    ])
    with caplog.at_level(logging.WARNING, logger="cogs.voice_controller.intent_bus"):
        await bus.dispatch(_ctx())

    tie_warnings = [
        r for r in caplog.records
        if r.levelno >= logging.WARNING
        and ("tie" in r.message.lower() or "collision" in r.message.lower() or "同分" in r.message)
    ]
    assert not tie_warnings, f"不該有 tie warning: {[r.message for r in tie_warnings]}"


@pytest.mark.asyncio
async def test_three_way_tie_logs_all_colliders(caplog):
    """3+ agent 同分 → warning 應該列全部碰撞者，便於 debug。"""
    bus = IntentBus([
        _StubAgent("a", lambda c: _bid("a", 0.9)),
        _StubAgent("b", lambda c: _bid("b", 0.9)),
        _StubAgent("c", lambda c: _bid("c", 0.9)),
        _StubAgent("d", lambda c: _bid("d", 0.5)),  # 不參與 top tie
    ])
    with caplog.at_level(logging.WARNING, logger="cogs.voice_controller.intent_bus"):
        winner = await bus.dispatch(_ctx())

    assert winner.name == "a"
    combined = " ".join(r.message for r in caplog.records if r.levelno >= logging.WARNING)
    assert "a" in combined and "b" in combined and "c" in combined
    # d 不該出現在 tie warning（它不在 top tier）
    # 注意：d 可能出現在其他 INFO log 中，這裡只看 WARNING


@pytest.mark.asyncio
async def test_single_bid_no_tie_warning(caplog):
    """只有一個 agent 出價 → 沒有第二名 → 不該 tie warning。"""
    bus = IntentBus([_StubAgent("only", lambda c: _bid("only", 0.9))])
    with caplog.at_level(logging.WARNING, logger="cogs.voice_controller.intent_bus"):
        await bus.dispatch(_ctx())

    tie_warnings = [
        r for r in caplog.records
        if r.levelno >= logging.WARNING
        and ("tie" in r.message.lower() or "collision" in r.message.lower() or "同分" in r.message)
    ]
    assert not tie_warnings
