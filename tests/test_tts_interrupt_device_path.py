"""
tests/test_tts_interrupt_device_path.py

TDD (T2a)：handle_raw_speech_start 的 TTS 打斷 block 必須走
_resolve_playback_device() 而非 discord.utils.get(voice_clients)。

四個案例：
(a) is_playing_audio=True, _tts_protected=False → 呼叫 _resolve_playback_device；
    device.stop() 被呼叫，clear_tts 與 _tts_interrupted=True 執行。
(b) Discord byte-equivalence：_resolve_playback_device 回 DiscordPlaybackDevice(vc)，
    vc.stop_playing() 被呼叫（NOT vc.stop）。
(c) device=None → 不 crash，clear_tts 與 _tts_interrupted=True 仍執行。
(d) _tts_protected=True → 打斷 block 完全跳過（guard 不變）。

函式在打斷 block 後碰到 voice_client 後段 → bot.voice_clients=[] 讓它回 None，
再靠 conv_buffer.get_conversation_temperature MagicMock >= 1.5 的 truthy 比較早退。
"""
from __future__ import annotations

from unittest.mock import MagicMock

from cogs.voice_controller import VoiceController
from marvin_voice_core.playback_device import DiscordPlaybackDevice


def _make_fake_self(*, is_playing_audio: bool = True, tts_protected: bool = False):
    """建 MagicMock fake self，含 handle_raw_speech_start 所需的所有屬性。"""
    fake = MagicMock()
    fake.bot.cogs.get.return_value = None
    # 讓 discord.utils.get(self.bot.voice_clients) 回 None，不污染斷言
    fake.bot.voice_clients = []
    # bool 直接設，不走 is_playing_audio property（unbound call，self=fake）
    fake.is_playing_audio = is_playing_audio
    fake._tts_protected = tts_protected
    fake._plan12 = True
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


# ── (a) 打斷 block 呼叫 _resolve_playback_device，device.stop() 與 clear_tts 執行 ───

def test_interrupt_calls_resolve_playback_device_stop():
    fake = _make_fake_self()
    device = MagicMock()
    device.is_playing.return_value = True
    fake._resolve_playback_device.return_value = device

    VoiceController.handle_raw_speech_start(fake, "Alice")

    fake._resolve_playback_device.assert_called_once()
    device.is_playing.assert_called_once()
    device.stop.assert_called_once()
    fake._mixer.clear_tts.assert_called_once()
    assert fake._tts_interrupted is True


# ── (b) Discord byte-equivalence：stop() → vc.stop_playing()，vc.stop 不呼叫 ──────

def test_interrupt_discord_byte_equiv_routes_to_vc_stop_playing():
    fake = _make_fake_self()
    mock_vc = MagicMock()
    mock_vc.is_playing.return_value = True
    # 真正的 DiscordPlaybackDevice：stop() → vc.stop_playing()
    fake._resolve_playback_device.return_value = DiscordPlaybackDevice(mock_vc)

    VoiceController.handle_raw_speech_start(fake, "Alice")

    mock_vc.stop_playing.assert_called_once()
    mock_vc.stop.assert_not_called()


# ── (c) device=None → 不 crash，clear_tts + _tts_interrupted 仍執行 ────────────────

def test_interrupt_device_none_no_crash():
    fake = _make_fake_self()
    fake._resolve_playback_device.return_value = None

    VoiceController.handle_raw_speech_start(fake, "Alice")

    fake._resolve_playback_device.assert_called_once()
    fake._mixer.clear_tts.assert_called_once()
    assert fake._tts_interrupted is True


# ── (d) _tts_protected=True → 打斷 block 完全跳過（guard 不變）────────────────────

def test_interrupt_skipped_when_tts_protected():
    fake = _make_fake_self(tts_protected=True)

    VoiceController.handle_raw_speech_start(fake, "Alice")

    fake._resolve_playback_device.assert_not_called()
    fake._mixer.clear_tts.assert_not_called()
    # _tts_interrupted 未被設成 True（仍是 MagicMock 預設值，不是 bool True）
    assert fake._tts_interrupted is not True
