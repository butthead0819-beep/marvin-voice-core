import asyncio
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from cogs.voice_controller import VoiceController
from tts_speak_policy import SpeakKind


@pytest.fixture
def dummy_vc_setup():
    cog = MagicMock(spec=VoiceController)
    cog.bot = MagicMock()
    cog.bot.user = MagicMock()
    cog.bot.user.id = 99999
    cog.consent = MagicMock()
    cog.consent.has_seen_notice.return_value = True
    cog._nudges = MagicMock()
    cog.greeting_cooldown = {}
    cog.stream_mode = False
    cog.stt_logger = MagicMock()
    cog.departure_stats = MagicMock()
    cog.departure_stats.record_departure = AsyncMock()
    cog.bot.router = MagicMock()
    cog.bot.router.generate_player_greeting = AsyncMock(return_value="測試點名台詞")
    cog._maybe_speak_join_callback = AsyncMock(return_value=False)
    cog.speak = AsyncMock()
    cog._send_mood_sticker = AsyncMock()
    cog.handle_dismiss = AsyncMock()
    cog.active_text_channel = MagicMock()
    cog.active_text_channel.send = AsyncMock()

    # Mixer mock
    cog._mixer = MagicMock()
    cog._mixer._tts_gain = 0.1
    # 模擬佇列無負載
    cog._mixer._tts_load_samples.return_value = 0

    channel = MagicMock()
    member = MagicMock()
    member.id = 12345
    member.display_name = "測試玩家"
    member.guild = MagicMock()
    channel.members = [member]

    voice_client = MagicMock()
    voice_client.guild = member.guild
    voice_client.channel = channel
    voice_client.is_connected.return_value = True
    cog.bot.voice_clients = [voice_client]

    return cog, member, channel, voice_client


@pytest.mark.asyncio
async def test_delayed_player_greeting_waits_5_seconds(dummy_vc_setup):
    """測試進場打招呼會延後 5 秒才 speak。"""
    cog, member, channel, voice_client = dummy_vc_setup
    
    # 綁定真實的 _delayed_player_greeting 方法
    cog._delayed_player_greeting = VoiceController._delayed_player_greeting.__get__(cog)

    sleep_calls = []
    _real_sleep = asyncio.sleep

    async def fake_sleep(sec):
        sleep_calls.append(sec)

    with patch("asyncio.sleep", side_effect=fake_sleep):
        await cog._delayed_player_greeting(member, channel, delay_sec=5.0)

    # 驗證有等待 5.0 秒
    assert 5.0 in sleep_calls
    # 驗證 speak 被呼叫
    cog.speak.assert_awaited_once_with("測試點名台詞", proactive=True, kind=SpeakKind.JOIN_GREETING)


@pytest.mark.asyncio
async def test_delayed_player_greeting_cancels_if_member_left(dummy_vc_setup):
    """測試若玩家在 5 秒內離開頻道，取消打招呼。"""
    cog, member, channel, voice_client = dummy_vc_setup
    cog._delayed_player_greeting = VoiceController._delayed_player_greeting.__get__(cog)

    async def fake_sleep(sec):
        # 模擬玩家在 5 秒等待期間離開了頻道
        channel.members = []

    with patch("asyncio.sleep", side_effect=fake_sleep):
        await cog._delayed_player_greeting(member, channel, delay_sec=5.0)

    # 玩家已離開，不可發言
    cog.speak.assert_not_called()
    cog.active_text_channel.send.assert_not_called()


@pytest.mark.asyncio
async def test_delayed_player_greeting_adjusts_volume_to_0_5_then_restores_0_1(dummy_vc_setup):
    """測試打招呼時音量設為 0.5，播完恢復為 0.1。"""
    cog, member, channel, voice_client = dummy_vc_setup
    cog._delayed_player_greeting = VoiceController._delayed_player_greeting.__get__(cog)

    gains_during_speak = []

    async def fake_speak(*args, **kwargs):
        # 記錄 speak 執行時當下的 _tts_gain
        gains_during_speak.append(cog._mixer._tts_gain)

    cog.speak = AsyncMock(side_effect=fake_speak)

    with patch("asyncio.sleep", AsyncMock()):
        await cog._delayed_player_greeting(member, channel, delay_sec=0.0)

    # 驗證 speak 當下音量為 0.5
    assert gains_during_speak == [0.5]
    # 驗證結束後音量恢復為 0.1
    assert cog._mixer._tts_gain == 0.1


@pytest.mark.asyncio
async def test_delayed_player_greeting_restores_volume_on_error(dummy_vc_setup):
    """測試若播放打招呼拋出例外，finally 仍確保音量恢復為 0.1。"""
    cog, member, channel, voice_client = dummy_vc_setup
    cog._delayed_player_greeting = VoiceController._delayed_player_greeting.__get__(cog)

    cog.speak = AsyncMock(side_effect=RuntimeError("語音播放失敗"))

    with patch("asyncio.sleep", AsyncMock()):
        with pytest.raises(RuntimeError):
            await cog._delayed_player_greeting(member, channel, delay_sec=0.0)

    # 確保例外後依然恢復 0.1
    assert cog._mixer._tts_gain == 0.1

