"""TDD: WakeShortcut 直派失敗要有回應，不能靜默。

問題（2026-07-23 實戰）：WakeShortcut 直派 asyncio.create_task(dispatch(...))
是 fire-and-forget，dispatch() 回 None（沒人 above threshold，例如 music
agent 被 repetitive_hallucination guard 擋下）時結果被丟掉——使用者已經
聽到 wake 反應（VAD/duck），指令卻悄悄消失，體感像 bot 沒聽到、連環喊
連環失敗都查不出原因。main bus 路徑（handle_stt_result 主流程）早就有
「沒接到→反問/回應」的處理（_ask_music_followup），WakeShortcut 這條
快路徑漏了同一層防護。

dispatch_with_feedback 補這一層：dispatch 有 winner→原樣放行；沒有→
貼頻道告知「沒處理成功」，不用 TTS（避免 storm，跟 _ask_music_followup
同精神）。純函式（傳 intent_bus/channel 進來，不掛 VoiceController），
不佔 [[project_voice_controller_decomposition]] 的 method 棘輪額度。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from wake_shortcut import dispatch_with_feedback


@pytest.mark.asyncio
async def test_no_feedback_when_dispatch_finds_a_winner():
    intent_bus = MagicMock()
    intent_bus.dispatch = AsyncMock(return_value=MagicMock(name="music_winner"))
    channel = AsyncMock()

    await dispatch_with_feedback(intent_bus, channel, "Alice", "馬文放晴天", MagicMock())

    channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_apologizes_to_channel_when_dispatch_has_no_winner():
    intent_bus = MagicMock()
    intent_bus.dispatch = AsyncMock(return_value=None)
    channel = AsyncMock()

    await dispatch_with_feedback(intent_bus, channel, "Alice", "馬文放晴天", MagicMock())

    channel.send.assert_awaited_once()
    msg = channel.send.await_args.args[0]
    assert "Alice" in msg


@pytest.mark.asyncio
async def test_no_channel_does_not_crash():
    intent_bus = MagicMock()
    intent_bus.dispatch = AsyncMock(return_value=None)

    await dispatch_with_feedback(intent_bus, None, "Alice", "馬文放晴天", MagicMock())


@pytest.mark.asyncio
async def test_channel_send_failure_does_not_crash():
    intent_bus = MagicMock()
    intent_bus.dispatch = AsyncMock(return_value=None)
    channel = AsyncMock()
    channel.send.side_effect = Exception("discord down")

    await dispatch_with_feedback(intent_bus, channel, "Alice", "馬文放晴天", MagicMock())
