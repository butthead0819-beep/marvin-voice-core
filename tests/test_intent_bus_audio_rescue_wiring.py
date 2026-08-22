"""IntentBus ↔ AudioRescueAgent wiring tests（Audio Rescue v2）。

比照 test_intent_bus_llm_rescue_wiring.py 風格：no winner → 呼叫注入的 rescue
agent.synthesize()。差別在於 dispatch_source == "llm_rescue_audio" 時，bus 不
重跑 self.dispatch()（regex 本來就沒中，重跑無意義），而是呼叫
_execute_resolved_intent() 直接對應到 stub agent 的 resolve_intent()。
"""
from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from intent_bus import Bid, IntentBus, IntentContext


def _ctx(query="小聲一點啦煩死了", depth=0):
    return IntentContext(
        speaker="Alice", raw_text=query, query=query, original_raw=query,
        wake_intent=0.9, stream_active=False, game_mode=False,
        is_owner=False, now=0.0, mode="normal", depth=depth,
        audio_wav_bytes=b"fake-wav",
    )


class _StubAgent:
    """跟 test_intent_bus_llm_rescue_wiring.py 同款，但額外支援 resolve_intent()。"""
    def __init__(self, name, bid_fn=None, resolve_fn=None):
        self.name = name
        self._bid_fn = bid_fn or (lambda ctx: Bid(name=name, confidence=0.0, handler=AsyncMock(), reason="no_match"))
        self._resolve_fn = resolve_fn
        self.resolve_calls: list[tuple] = []

    def bid(self, ctx):
        return self._bid_fn(ctx)

    def resolve_intent(self, intent_name, slots, ctx):
        self.resolve_calls.append((intent_name, slots, ctx))
        if self._resolve_fn is None:
            return None
        return self._resolve_fn(intent_name, slots, ctx)


class _StubRescue:
    name = "AudioRescue"

    def __init__(self, result):
        self._result = result
        self.calls: list[IntentContext] = []

    async def synthesize(self, ctx):
        self.calls.append(ctx)
        if isinstance(self._result, BaseException):
            raise self._result
        return self._result


@pytest.mark.asyncio
async def test_audio_rescue_executes_resolved_intent_not_redispatch():
    handler = AsyncMock()
    volume_agent = _StubAgent(
        "volume",
        resolve_fn=lambda name, slots, ctx: Bid(name="volume", confidence=0.9, handler=handler, reason="audio_rescue:x"),
    )
    rescued_ctx = replace(
        _ctx(), depth=1, dispatch_source="llm_rescue_audio",
        resolved_agent="volume", resolved_intent="volume_down", resolved_slots={},
    )
    rescue = _StubRescue(result=rescued_ctx)
    bus = IntentBus([volume_agent], llm_rescue_agent=rescue)

    winner = await bus.dispatch(_ctx())

    assert winner is not None
    assert winner.name == "volume"
    handler.assert_awaited_once()
    assert volume_agent.resolve_calls == [("volume_down", {}, rescued_ctx)]


@pytest.mark.asyncio
async def test_audio_rescue_agent_not_found_returns_none_without_crash():
    rescued_ctx = replace(
        _ctx(), depth=1, dispatch_source="llm_rescue_audio",
        resolved_agent="does_not_exist", resolved_intent="x", resolved_slots={},
    )
    rescue = _StubRescue(result=rescued_ctx)
    bus = IntentBus([_StubAgent("volume")], llm_rescue_agent=rescue)

    winner = await bus.dispatch(_ctx())

    assert winner is None


@pytest.mark.asyncio
async def test_audio_rescue_resolve_intent_returning_none_is_clean():
    volume_agent = _StubAgent("volume", resolve_fn=lambda *a: None)
    rescued_ctx = replace(
        _ctx(), depth=1, dispatch_source="llm_rescue_audio",
        resolved_agent="volume", resolved_intent="volume_down", resolved_slots={},
    )
    rescue = _StubRescue(result=rescued_ctx)
    bus = IntentBus([volume_agent], llm_rescue_agent=rescue)

    winner = await bus.dispatch(_ctx())

    assert winner is None


@pytest.mark.asyncio
async def test_audio_rescue_shadow_mode_does_not_execute():
    handler = AsyncMock()
    volume_agent = _StubAgent(
        "volume",
        resolve_fn=lambda name, slots, ctx: Bid(name="volume", confidence=0.9, handler=handler, reason="x"),
    )
    rescued_ctx = replace(
        _ctx(), depth=1, dispatch_source="llm_rescue_audio",
        resolved_agent="volume", resolved_intent="volume_down", resolved_slots={},
    )
    rescue = _StubRescue(result=rescued_ctx)
    bus = IntentBus([volume_agent], llm_rescue_agent=rescue, rescue_shadow_mode=True)

    winner = await bus.dispatch(_ctx())

    assert winner is None
    handler.assert_not_awaited()
    assert volume_agent.resolve_calls == []


@pytest.mark.asyncio
async def test_audio_rescue_depth_guard_skips_rescue():
    rescue = _StubRescue(result=None)
    bus = IntentBus([_StubAgent("volume")], llm_rescue_agent=rescue)

    await bus.dispatch(_ctx(depth=1))

    assert rescue.calls == []


@pytest.mark.asyncio
async def test_emit_rescue_outcome_includes_audio_fields():
    handler = AsyncMock()
    volume_agent = _StubAgent(
        "volume",
        resolve_fn=lambda name, slots, ctx: Bid(name="volume", confidence=0.9, handler=handler, reason="x"),
    )
    rescued_ctx = replace(
        _ctx(), depth=1, dispatch_source="llm_rescue_audio",
        resolved_agent="volume", resolved_intent="volume_down", resolved_slots={"foo": "bar"},
    )
    rescue = _StubRescue(result=rescued_ctx)
    records: list[dict] = []
    bus = IntentBus(
        [volume_agent], llm_rescue_agent=rescue, rescue_outcome_sink=records.append,
    )

    await bus.dispatch(_ctx())

    assert len(records) == 1
    rec = records[0]
    assert rec["dispatch_mode"] == "llm_rescue_audio"
    assert rec["resolved_agent"] == "volume"
    assert rec["resolved_intent"] == "volume_down"
    assert rec["resolved_slots"] == {"foo": "bar"}
    assert rec["audio_bytes_len"] == len(b"fake-wav")
