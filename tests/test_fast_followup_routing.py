"""10/20 fast-followup routing：連續對談窗內跳過 AudioRescue + gap classifier 預設關閉。

背景：連續對談一句話在進 Marvin 主 LLM 前要串行等 AudioRescue（~2.9s）和 gap
classifier（~1.7s）。使用者定案：
1. in_wake_session（WakeSession 已開窗且是 target 的句子）→ IntentBus._maybe_rescue 早退。
2. gap classifier 改預設關閉，MARVIN_GAP_CLASSIFIER=1 才開。

A 組測 IntentBus 層（仿 test_intent_bus_audio_rescue_wiring.py 的 stub 寫法）。
B 組測 VoiceController 層（沿用 test_intent_gate_silenced_log.py 的 _make_vc harness）。
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from intent_bus import Bid, IntentBus, IntentContext

from tests.test_intent_gate_silenced_log import _make_vc

pytestmark = pytest.mark.asyncio


def _ctx(in_wake_session: bool, query="隨便聊聊"):
    return IntentContext(
        speaker="Alice", raw_text=query, query=query, original_raw=query,
        wake_intent=0.9, stream_active=False, game_mode=False,
        is_owner=False, now=0.0, mode="normal",
        in_wake_session=in_wake_session,
    )


class _StubAgent:
    """沒人出價——仿 test_intent_bus_audio_rescue_wiring.py 的 _StubAgent。"""
    name = "noop"

    def bid(self, ctx):
        return Bid(name=self.name, confidence=0.0, handler=AsyncMock(), reason="no_match")


class _StubRescue:
    name = "LLMRescue"

    def __init__(self):
        self.calls: list[IntentContext] = []

    async def synthesize(self, ctx):
        self.calls.append(ctx)
        return None


# --- A: IntentBus 層 ---

async def test_in_wake_session_skips_rescue():
    rescue = _StubRescue()
    bus = IntentBus([_StubAgent()], llm_rescue_agent=rescue)

    winner = await bus.dispatch(_ctx(in_wake_session=True))

    assert winner is None
    assert rescue.calls == []


async def test_not_in_wake_session_still_calls_rescue():
    rescue = _StubRescue()
    bus = IntentBus([_StubAgent()], llm_rescue_agent=rescue)

    winner = await bus.dispatch(_ctx(in_wake_session=False))

    assert winner is None
    assert len(rescue.calls) == 1


# --- B: VoiceController 層 ---

async def test_controller_passes_in_wake_session_true_to_bus_ctx(monkeypatch):
    vc, _captured = _make_vc(monkeypatch, query_intent_signal=True, query_text="隨便聊聊")
    vc._wake_session = MagicMock(target="狗與露", opened=True)

    seen_ctx = {}
    async def _dispatch(ctx):
        seen_ctx["ctx"] = ctx
        return None
    vc._intent_bus.dispatch = _dispatch

    await vc._process_queued_query("狗與露", time.time(), wake_intent=None)

    assert seen_ctx["ctx"].in_wake_session is True


async def test_controller_passes_in_wake_session_false_when_no_session(monkeypatch):
    vc, _captured = _make_vc(monkeypatch, query_intent_signal=True, query_text="隨便聊聊")
    assert not hasattr(vc, "_wake_session")

    seen_ctx = {}
    async def _dispatch(ctx):
        seen_ctx["ctx"] = ctx
        return None
    vc._intent_bus.dispatch = _dispatch

    await vc._process_queued_query("狗與露", time.time(), wake_intent=None)

    assert seen_ctx["ctx"].in_wake_session is False


async def test_gap_classifier_not_called_without_env(monkeypatch):
    import cogs.voice_controller as vcmod

    monkeypatch.delenv("MARVIN_GAP_CLASSIFIER", raising=False)
    vc, _captured = _make_vc(monkeypatch, query_intent_signal=False, query_text="暫停播放")
    vc._shared_tier_router = MagicMock()
    vc._gap_logger = MagicMock()
    vc._stream_response = AsyncMock()

    make_classifier_mock = MagicMock()
    monkeypatch.setattr(vcmod, "make_groq_gap_classifier", make_classifier_mock)
    handle_gap_mock = AsyncMock()
    monkeypatch.setattr(vcmod, "handle_intent_gap", handle_gap_mock)

    await vc._process_queued_query("狗與露", time.time(), wake_intent=None)

    make_classifier_mock.assert_not_called()
    handle_gap_mock.assert_not_awaited()


async def test_gap_classifier_called_with_env(monkeypatch):
    import cogs.voice_controller as vcmod
    from intent_gap import IntentGapRecord

    monkeypatch.setenv("MARVIN_GAP_CLASSIFIER", "1")
    vc, _captured = _make_vc(monkeypatch, query_intent_signal=False, query_text="暫停播放")
    vc._shared_tier_router = MagicMock()
    vc._gap_logger = MagicMock()
    vc._stream_response = AsyncMock()

    monkeypatch.setattr(vcmod, "make_groq_gap_classifier", lambda router: MagicMock())
    fake_rec = IntentGapRecord(
        utterance_id="u1", ts=time.time(), speaker="狗與露", mode="normal",
        raw_query="暫停播放", cleaned_query="暫停播放", intent_type="playback_control_pause",
        slots={}, nearest_agent="playback_control", nearest_distance=0.0,
        ack_text=None, acknowledged=False,
    )
    handle_gap_mock = AsyncMock(return_value=fake_rec)
    monkeypatch.setattr(vcmod, "handle_intent_gap", handle_gap_mock)

    await vc._process_queued_query("狗與露", time.time(), wake_intent=None)

    handle_gap_mock.assert_awaited_once()
