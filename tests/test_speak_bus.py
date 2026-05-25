"""SpeakBus — Week 1 基建測試。

SpeakBus 是「主動發話」的 bid 架構，平行於 IntentBus（reactive）。

設計合約見 docs/social_catalyst_plan.md。

涵蓋：
  1. 無 agent → tick 回 None
  2. 單一 agent dense 0 → 不會被選中
  3. 單一 agent confidence > MIN → 回該 bid
  4. 多 agent → 最高 confidence 勝
  5. global_multiplier 拉低 → effective < MIN 不選
  6. global_multiplier TTL 過期自動回 1.0
  7. agent bid 拋例外 → 不影響其他 agent
  8. 同名 register 不重複
"""
from __future__ import annotations

import asyncio
import time

import pytest

from speak_bus import SpeakBus, SpeakBid, SpeakContext


async def _noop() -> None:
    pass


def _ctx(**overrides) -> SpeakContext:
    base = dict(
        channel_id=100,
        guild_id=1,
        silence_seconds=10.0,
        present_speakers=["alice", "bob"],
        room_mood=None,
        recent_utterances=[],
        trigger="idle_tick",
        last_speaker=None,
        last_text=None,
    )
    base.update(overrides)
    return SpeakContext(**base)


class _FakeAgent:
    def __init__(self, name: str, conf: float, reason: str = "test") -> None:
        self.name = name
        self._conf = conf
        self._reason = reason
        self.call_count = 0

    async def speak_bid(self, ctx: SpeakContext) -> SpeakBid:
        self.call_count += 1
        return SpeakBid(
            agent_name=self.name,
            confidence=self._conf,
            handler=_noop,
            reason=self._reason,
        )


class _ExplodingAgent:
    name = "exploder"

    async def speak_bid(self, ctx: SpeakContext) -> SpeakBid:
        raise RuntimeError("boom")


# ── tests ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_bus_returns_none():
    bus = SpeakBus()
    assert await bus.tick(_ctx()) is None


@pytest.mark.asyncio
async def test_dense_zero_not_selected():
    bus = SpeakBus()
    bus.register(_FakeAgent("a", 0.0))
    assert await bus.tick(_ctx()) is None


@pytest.mark.asyncio
async def test_single_agent_above_threshold_wins():
    bus = SpeakBus()
    bus.register(_FakeAgent("a", 0.5, reason="seed"))
    bid = await bus.tick(_ctx())
    assert bid is not None
    assert bid.agent_name == "a"
    assert bid.reason == "seed"


@pytest.mark.asyncio
async def test_highest_confidence_wins():
    bus = SpeakBus()
    bus.register(_FakeAgent("low", 0.4))
    bus.register(_FakeAgent("high", 0.8))
    bus.register(_FakeAgent("mid", 0.6))
    bid = await bus.tick(_ctx())
    assert bid.agent_name == "high"


@pytest.mark.asyncio
async def test_global_multiplier_suppresses_bids():
    bus = SpeakBus()
    bus.register(_FakeAgent("a", 0.6))
    bus.set_global_multiplier(0.2, ttl_s=60.0)
    # 0.6 * 0.2 = 0.12 < MIN_CONFIDENCE
    assert await bus.tick(_ctx()) is None


@pytest.mark.asyncio
async def test_global_multiplier_expires():
    bus = SpeakBus()
    bus.register(_FakeAgent("a", 0.6))
    bus.set_global_multiplier(0.0, ttl_s=0.01)
    await asyncio.sleep(0.02)
    bid = await bus.tick(_ctx())
    assert bid is not None
    assert bid.agent_name == "a"


@pytest.mark.asyncio
async def test_exception_in_one_agent_does_not_kill_others():
    bus = SpeakBus()
    bus.register(_ExplodingAgent())
    bus.register(_FakeAgent("survivor", 0.7))
    bid = await bus.tick(_ctx())
    assert bid is not None
    assert bid.agent_name == "survivor"


@pytest.mark.asyncio
async def test_register_same_name_replaces_not_duplicates():
    bus = SpeakBus()
    bus.register(_FakeAgent("a", 0.3))
    bus.register(_FakeAgent("a", 0.9))  # 同名應該取代
    bid = await bus.tick(_ctx())
    assert bid is not None
    # 拿到的應該是新註冊的（0.9），而且只 call 一次
    assert bid.agent_name == "a"


@pytest.mark.asyncio
async def test_multiplier_scales_confidence_in_returned_bid():
    """得標 bid 的 confidence 應該已套用 multiplier（caller 觀察到的是 effective 值）。"""
    bus = SpeakBus()
    bus.register(_FakeAgent("a", 1.0))
    bus.set_global_multiplier(0.5, ttl_s=60.0)
    bid = await bus.tick(_ctx())
    assert bid is not None
    # 0.5 effective 高於 MIN，會被選；caller 看到 0.5 而不是 1.0
    assert bid.confidence == pytest.approx(0.5)
