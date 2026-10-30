"""10/20 fast-followup routing：連續對談窗內跳過 AudioRescue。

背景：連續對談一句話在進 Marvin 主 LLM 前要串行等 AudioRescue（~2.9s）。使用者定案：
1. in_wake_session（WakeSession 已開窗且是 target 的句子）→ IntentBus._maybe_rescue 早退。
2. （gap classifier 已於意圖漏斗步驟 2 第二批刪除）

A 組測 IntentBus 層（仿 test_intent_bus_audio_rescue_wiring.py 的 stub 寫法）。
B 組測 VoiceController 層（沿用 test_stream_response_characterization.py 的 _make_vc harness）。
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from intent_bus import Bid, IntentBus, IntentContext

from tests.test_stream_response_characterization import _make_vc

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
    vc, _ph, _ch = _make_vc(monkeypatch, [])
    vc.bot.engine.conv_buffer.get_harvest.return_value = "隨便聊聊"
    vc._wake_session = MagicMock(target="狗與露", opened=True)

    seen_ctx = {}
    async def _dispatch(ctx):
        seen_ctx["ctx"] = ctx
        return None
    vc._intent_bus.dispatch = _dispatch

    await vc._process_queued_query("狗與露", time.time(), wake_intent=None)

    assert seen_ctx["ctx"].in_wake_session is True


async def test_controller_passes_in_wake_session_false_when_no_session(monkeypatch):
    vc, _ph, _ch = _make_vc(monkeypatch, [])
    vc.bot.engine.conv_buffer.get_harvest.return_value = "隨便聊聊"
    assert not hasattr(vc, "_wake_session")

    seen_ctx = {}
    async def _dispatch(ctx):
        seen_ctx["ctx"] = ctx
        return None
    vc._intent_bus.dispatch = _dispatch

    await vc._process_queued_query("狗與露", time.time(), wake_intent=None)

    assert seen_ctx["ctx"].in_wake_session is False




async def test_speculative_prefetch_threshold_relaxed_to_four_chars():
    """驗證 4~5 字的 _speculative_query（如 '收到請迴答'）會觸發預熱 LLM。"""
    from tests.test_followup_listening import _make_cog_for_stt
    cog, wf = _make_cog_for_stt()
    cog.bot.router._speculative_response = AsyncMock(return_value="ok")
    wf.multi_channel_decide.return_value = (
        True, 0.95,
        {"voice": 0.95, "task": 0.0, "info": 0.0, "control": 0.0,
         "threshold": 0.35, "total": 0.95},
    )

    await cog.handle_stt_result(
        "TestUser", "馬文收到請迴答", time.time(), b"fake_audio",
        bypass_etd=True,
    )

    assert "TestUser" in cog.bot.router._pending_prefetch
    cog.bot.router._speculative_response.assert_called_once()
    assert cog.bot.router._speculative_response.call_args[0][1] == "收到請迴答"

