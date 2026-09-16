"""TDD：IntentBus core — IntentContext / Bid / IntentBus dispatch 行為。"""
from __future__ import annotations

import logging
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from intent_bus import IntentBus, IntentContext, Bid, RescuePayload


def _ctx(query="今天天氣怎樣", wake_intent=None, game_mode=False, is_owner=False, stream_active=False):
    return IntentContext(
        speaker="Alice",
        raw_text=query,
        query=query,
        original_raw=None,
        wake_intent=wake_intent,
        stream_active=stream_active,
        game_mode=game_mode,
        is_owner=is_owner,
        now=100.0,
    )


class _StubAgent:
    """Configurable test agent. bid_fn returns Bid|None when called."""
    def __init__(self, name, bid_fn=None):
        self.name = name
        self._bid_fn = bid_fn
        self.calls = 0
    def bid(self, ctx):
        self.calls += 1
        return self._bid_fn(ctx) if self._bid_fn else None


def _bid(name, confidence, handler=None):
    return Bid(name=name, confidence=confidence,
               handler=handler or AsyncMock(),
               reason=f"test:{name}")


# ── Empty / no-bid cases ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dispatch_returns_none_when_no_agents():
    bus = IntentBus([])
    result = await bus.dispatch(_ctx())
    assert result is None


@pytest.mark.asyncio
async def test_dispatch_returns_none_when_all_agents_pass():
    bus = IntentBus([_StubAgent("a", lambda c: None),
                     _StubAgent("b", lambda c: None)])
    result = await bus.dispatch(_ctx())
    assert result is None


# ── Single winner ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dispatch_calls_winner_handler():
    handler = AsyncMock()
    bus = IntentBus([_StubAgent("a", lambda c: _bid("a", 0.9, handler))])
    winner = await bus.dispatch(_ctx())
    assert winner is not None
    assert winner.name == "a"
    handler.assert_awaited_once()


# ── Max wins ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dispatch_picks_highest_confidence():
    h_low = AsyncMock()
    h_high = AsyncMock()
    bus = IntentBus([
        _StubAgent("low",  lambda c: _bid("low",  0.40, h_low)),
        _StubAgent("high", lambda c: _bid("high", 0.95, h_high)),
        _StubAgent("mid",  lambda c: _bid("mid",  0.70, AsyncMock())),
    ])
    winner = await bus.dispatch(_ctx())
    assert winner.name == "high"
    h_high.assert_awaited_once()
    h_low.assert_not_awaited()


# ── Min confidence threshold ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dispatch_returns_none_when_max_below_threshold():
    handler = AsyncMock()
    bus = IntentBus([_StubAgent("weak", lambda c: _bid("weak", 0.20, handler))])
    result = await bus.dispatch(_ctx())
    assert result is None
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_min_confidence_is_inclusive_at_threshold():
    """confidence == MIN_CONFIDENCE 應該 OK 算贏。"""
    bus = IntentBus.__new__(IntentBus)  # bypass __init__
    bus.agents = []
    bus.MIN_CONFIDENCE = 0.30
    bus.logger = logging.getLogger("cogs.voice_controller.intent_bus")

    handler = AsyncMock()
    bus.agents = [_StubAgent("edge", lambda c: _bid("edge", 0.30, handler))]
    winner = await bus.dispatch(_ctx())
    assert winner is not None
    handler.assert_awaited_once()


# ── Agent exception isolation ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dispatch_isolates_agent_exception():
    """一個 agent 的 bid() 炸了，其他 agents 還是要正常 bid。"""
    def _broken(ctx):
        raise RuntimeError("agent crashed")

    h_ok = AsyncMock()
    bus = IntentBus([
        _StubAgent("broken", _broken),
        _StubAgent("ok", lambda c: _bid("ok", 0.5, h_ok)),
    ])
    winner = await bus.dispatch(_ctx())
    assert winner is not None
    assert winner.name == "ok"
    h_ok.assert_awaited_once()


# ── Handler exception propagates ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_dispatch_handler_exception_propagates():
    """handler() 炸了不該被吞 — caller 要看得到。"""
    async def _bad_handler():
        raise RuntimeError("handler bug")

    bus = IntentBus([_StubAgent("x", lambda c: _bid("x", 0.9, _bad_handler))])
    with pytest.raises(RuntimeError, match="handler bug"):
        await bus.dispatch(_ctx())


# ── Observability ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dispatch_logs_bid_summary(caplog):
    bus = IntentBus([
        _StubAgent("a", lambda c: _bid("a", 0.95)),
        _StubAgent("b", lambda c: _bid("b", 0.40)),
        _StubAgent("c", lambda c: None),
    ])
    with caplog.at_level(logging.INFO, logger="cogs.voice_controller.intent_bus"):
        await bus.dispatch(_ctx())
    # 應該至少有一條 log 同時含 winner + bids
    relevant = [r for r in caplog.records if "IntentBus" in r.message]
    assert relevant, "expected at least one [IntentBus] log line"
    combined = " ".join(r.message for r in relevant)
    assert "a=0.95" in combined or "a:0.95" in combined or "a 0.95" in combined
    assert "b=0.40" in combined or "b:0.40" in combined or "b 0.40" in combined
    assert "winner" in combined.lower() or "won" in combined.lower()


# ── Bid ordering: stable when tied ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_dispatch_with_tie_picks_first_registered():
    """同分時取第一個註冊的（穩定排序），方便 debug。"""
    h1, h2 = AsyncMock(), AsyncMock()
    bus = IntentBus([
        _StubAgent("first",  lambda c: _bid("first",  0.80, h1)),
        _StubAgent("second", lambda c: _bid("second", 0.80, h2)),
    ])
    winner = await bus.dispatch(_ctx())
    assert winner.name == "first"
    h1.assert_awaited_once()
    h2.assert_not_awaited()


# ── IntentContext immutability ─────────────────────────────────────────────

def test_intent_context_is_frozen():
    """IntentContext 是 frozen dataclass — agent 不能誤改 state。"""
    ctx = _ctx()
    with pytest.raises((AttributeError, Exception)):  # FrozenInstanceError
        ctx.query = "tampered"  # type: ignore


# ── RescuePayload 相容層（Phase A）───────────────────────────────────────────

def test_rescue_defaults_to_payload_with_matching_defaults():
    """舊式建構（不傳任何救援 flat 欄位）時，ctx.rescue 不會是 None，
    且欄位值等於 flat 欄位的預設值 —— 全部欄位 optional 不會炸。"""
    ctx = _ctx()
    assert ctx.rescue is not None
    assert ctx.rescue == RescuePayload(
        dispatch_source=ctx.dispatch_source,
        pragmatic_signal=ctx.pragmatic_signal,
        pragmatic_target=ctx.pragmatic_target,
        payload=ctx.payload,
        audio_wav_bytes=ctx.audio_wav_bytes,
        prev_turn_audio_wav_bytes=ctx.prev_turn_audio_wav_bytes,
        resolved_agent=ctx.resolved_agent,
        resolved_intent=ctx.resolved_intent,
        resolved_slots=ctx.resolved_slots,
        low_confidence_wake=ctx.low_confidence_wake,
    )


def test_rescue_mirrors_flat_fields_when_constructed_old_style():
    """既有 30 個 agent 呼叫端只傳 flat 救援欄位、不傳 rescue —— ctx.rescue
    要能正確反映出那些 flat 欄位的值，讓新程式碼可以改讀 ctx.rescue.xxx。"""
    ctx = IntentContext(
        speaker="Alice",
        raw_text="播放五月天",
        query="播放五月天",
        original_raw="播放五月天",
        wake_intent=0.9,
        stream_active=False,
        game_mode=False,
        is_owner=True,
        now=100.0,
        dispatch_source="llm_rescue_audio",
        pragmatic_signal="positive",
        pragmatic_target="current_song",
        payload={"job_id": "abc123"},
        audio_wav_bytes=b"\x00\x01",
        prev_turn_audio_wav_bytes=b"\x02\x03",
        resolved_agent="music_agent_v2",
        resolved_intent="play_song",
        resolved_slots={"song": "五月天"},
        low_confidence_wake=True,
    )
    assert ctx.rescue.dispatch_source == "llm_rescue_audio"
    assert ctx.rescue.pragmatic_signal == "positive"
    assert ctx.rescue.pragmatic_target == "current_song"
    assert ctx.rescue.payload == {"job_id": "abc123"}
    assert ctx.rescue.audio_wav_bytes == b"\x00\x01"
    assert ctx.rescue.prev_turn_audio_wav_bytes == b"\x02\x03"
    assert ctx.rescue.resolved_agent == "music_agent_v2"
    assert ctx.rescue.resolved_intent == "play_song"
    assert ctx.rescue.resolved_slots == {"song": "五月天"}
    assert ctx.rescue.low_confidence_wake is True
    # flat 欄位本身完全不受影響 —— 這次是新增平行路徑，不是取代。
    assert ctx.dispatch_source == "llm_rescue_audio"
    assert ctx.low_confidence_wake is True


def test_rescue_explicit_override_is_ignored():
    """呼叫端明確傳入 rescue 時，__post_init__ 仍然無條件從 flat 欄位重建，
    忽略傳入值。行為變更（原本是「尊重呼叫端傳入值」）：flat 欄位才是唯一
    事實來源，rescue 只能是唯讀鏡像，不接受呼叫端覆蓋——否則
    dataclasses.replace() 只改 flat 欄位時，若舊物件的 rescue 已非 None，
    __post_init__ 會誤判成「呼叫端明確傳入」而不重新同步，見
    test_rescue_stays_synced_after_replace_flat_field。"""
    explicit = RescuePayload(dispatch_source="marmo_inject")
    ctx = IntentContext(
        speaker="Alice",
        raw_text="test",
        query="test",
        original_raw=None,
        wake_intent=None,
        stream_active=False,
        game_mode=False,
        is_owner=False,
        now=100.0,
        rescue=explicit,
    )
    assert ctx.rescue is not explicit
    assert ctx.rescue.dispatch_source == "regex"  # 來自 flat 欄位預設值，非 explicit


def test_rescue_stays_synced_after_replace_flat_field():
    """回歸測試：dataclasses.replace() 只改 flat 欄位時，rescue 鏡像要跟上。

    修復前的 bug：__post_init__ 只在 self.rescue is None 時才重建；
    replace() 沒明確指定的 rescue 欄位會沿用舊物件當前的 rescue（非
    None），導致新物件 flat 欄位是新值、rescue.xxx 卻是舊值，永久不同步。
    """
    ctx = IntentContext(
        speaker="a",
        raw_text="x",
        query="x",
        original_raw=None,
        wake_intent=1.0,
        stream_active=False,
        game_mode=False,
        is_owner=False,
        now=0.0,
    )
    ctx2 = replace(ctx, resolved_agent="music")
    assert ctx2.resolved_agent == "music"
    assert ctx2.rescue.resolved_agent == "music"
