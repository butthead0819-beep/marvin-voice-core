"""
tests/test_tts_interrupt_device_path.py

TDD (T2a)：handle_raw_speech_start 的 TTS 打斷 block 必須走
_resolve_playback_device() 而非 discord.utils.get(voice_clients)。

⚠️ 10/22 改為「只有對談窗 target 能打斷」（session barge-in）：handle_raw_speech_start
不再同步切 TTS，只在 speaker 是 session.target 時起一個 watcher task（_barge_in_watch
持續講話過 BARGE_CONFIRM_S 才真的切）。本檔 fake 沒有設 _wake_session → 一律不建
watcher，device/_resolve_playback_device 完全不會被呼叫。device-routing
（_resolve_playback_device / DiscordPlaybackDevice byte-equivalence）的斷言已搬到
_barge_in_watch 本身，見 tests/test_barge_in_wiring.py 的
test_barge_in_watch_cuts_then_plays_ack。

(d) _tts_protected=True → 打斷 block 完全跳過，這點不變。

函式在打斷 block 後碰到 voice_client 後段 → bot.voice_clients=[] 讓它回 None，
再靠 conv_buffer.get_conversation_temperature MagicMock >= 1.5 的 truthy 比較早退。
"""
from __future__ import annotations

from unittest.mock import MagicMock

from cogs.voice_controller import VoiceController
from marvin_voice_core.playback_device import DiscordPlaybackDevice


def _make_fake_self(*, is_playing_audio: bool = True, tts_protected: bool = False,
                    plan12: bool = False):
    """建 MagicMock fake self，含 handle_raw_speech_start 所需的所有屬性。"""
    fake = MagicMock()
    fake.bot.cogs.get.return_value = None
    # 讓 discord.utils.get(self.bot.voice_clients) 回 None，不污染斷言
    fake.bot.voice_clients = []
    # bool 直接設，不走 is_playing_audio property（unbound call，self=fake）
    fake.is_playing_audio = is_playing_audio
    fake._tts_protected = tts_protected
    fake._plan12 = plan12
    fake._mixer = MagicMock()
    fake._current_tts_text = "未說完的話"
    fake._current_tts_in_channel = True  # 跳過 asyncio.create_task
    fake.stt_logger = MagicMock()
    # silence_duration = time.time() - last_marvin_speech_time 必須是 float
    fake.last_marvin_speech_time = 0.0
    # user_states 走真 dict，避免 MagicMock __contains__ 奇異行為
    fake.user_states = {}
    # conv_buffer 溫度回 float（MagicMock >= float 在 Python 3.13 拋 TypeError）
    fake.bot.engine.conv_buffer.get_conversation_temperature.return_value = 2.0
    return fake


# ── (a)/(b)/(c) 10/22：無 session target → 不建 watcher，device 完全不被碰 ──────

def test_interrupt_skipped_when_no_wake_session_target():
    fake = _make_fake_self()
    device = MagicMock()
    device.is_playing.return_value = True
    fake._resolve_playback_device.return_value = device

    VoiceController.handle_raw_speech_start(fake, "Alice")

    fake._resolve_playback_device.assert_not_called()
    assert fake._tts_interrupted is not True


def test_interrupt_discord_byte_equiv_skipped_when_no_wake_session_target():
    fake = _make_fake_self()
    mock_vc = MagicMock()
    mock_vc.is_playing.return_value = True
    fake._resolve_playback_device.return_value = DiscordPlaybackDevice(mock_vc)

    VoiceController.handle_raw_speech_start(fake, "Alice")

    mock_vc.stop_playing.assert_not_called()
    mock_vc.stop.assert_not_called()


def test_interrupt_device_none_no_crash_when_no_wake_session_target():
    fake = _make_fake_self(plan12=True)
    fake._resolve_playback_device.return_value = None

    VoiceController.handle_raw_speech_start(fake, "Alice")

    fake._resolve_playback_device.assert_not_called()
    fake._mixer.clear_tts.assert_not_called()
    assert fake._tts_interrupted is not True


# ── (d) _tts_protected=True → 打斷 block 完全跳過（guard 不變）────────────────────

def test_interrupt_skipped_when_tts_protected():
    fake = _make_fake_self(tts_protected=True)

    VoiceController.handle_raw_speech_start(fake, "Alice")

    fake._resolve_playback_device.assert_not_called()
    fake._mixer.clear_tts.assert_not_called()
    # _tts_interrupted 未被設成 True（仍是 MagicMock 預設值，不是 bool True）
    assert fake._tts_interrupted is not True
