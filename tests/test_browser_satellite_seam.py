"""
tests/test_browser_satellite_seam.py
TDD：純軟體 satellite 輸出接縫（ConnectionMixin.start_browser_satellite_listening）。

與 start_satellite_listening 平行、但無 Pi/wyoming 橋、無 mic sink：輸出注入
BrowserSpeakerOutput，輸入唯一來源是 POST /audio。Pi 路徑不受影響。
"""
from unittest.mock import MagicMock

from cogs.voice_controller_connection import ConnectionMixin
from marvin_voice_core.playback_device import LocalSpeakerDevice
from marvin_voice_core.browser_speaker_output import BrowserSpeakerOutput


def _make_fake_self():
    fake = MagicMock()
    fake.bot.engine.start = MagicMock()
    fake.set_local_speaker.side_effect = lambda device: setattr(fake, "_local_speaker", device)
    return fake


def test_browser_sets_local_mode_true():
    fake = _make_fake_self()
    ConnectionMixin.start_browser_satellite_listening(fake, BrowserSpeakerOutput())
    assert fake._local_mode is True


def test_browser_calls_engine_start():
    fake = _make_fake_self()
    ConnectionMixin.start_browser_satellite_listening(fake, BrowserSpeakerOutput())
    fake.bot.engine.start.assert_called_once()


def test_browser_speaker_output_is_browser_speaker_output():
    fake = _make_fake_self()
    out = BrowserSpeakerOutput()
    ConnectionMixin.start_browser_satellite_listening(fake, out)
    assert isinstance(fake._local_speaker, LocalSpeakerDevice)
    assert fake._local_speaker._output is out


def test_browser_does_not_create_wyoming_bridge_or_mic_sink():
    """純軟體模式不連 Pi：不設 _satellite_bridge、不排重連 task。"""
    fake = _make_fake_self()
    ConnectionMixin.start_browser_satellite_listening(fake, BrowserSpeakerOutput())
    fake.bot.loop.create_task.assert_not_called()   # 無重連迴圈（無 Pi）


def test_browser_consent_allows_any_speaker():
    fake = _make_fake_self()
    ConnectionMixin.start_browser_satellite_listening(fake, BrowserSpeakerOutput())
    assert fake.consent.is_consented("Alice") is True


def test_browser_relaxes_late_skip_threshold():
    fake = _make_fake_self()
    ConnectionMixin.start_browser_satellite_listening(fake, BrowserSpeakerOutput())
    assert fake._LATE_RESPONSE_SKIP_SEC == 120.0


def test_browser_default_persistent_true_avoids_pump_exhaustion_race():
    """persistent=False 時，_ensure_mixer_playing 先 arm 泵、_stream_tts_to_mixer 才推
    frame 進 mixer；若 TTS 首塊延遲超過 mixer 的 on-demand idle grace（預設 1s），泵會判
    定「播完」提前關閉、close() 對空 _current no-op，整段回覆遺失且無任何錯誤（2026-07-23
    ESP32 puck 實測：/reply 觸發 TTS 但 seq 不動，log 顯示泵幾乎瞬間 arm→exhausted）。
    persistent=True（比照車載 StreamSpeakerOutput）泵常駐不因 idle 提前退出，靠
    BrowserSpeakerOutput.write() 自己的靜音 hangover 偵測（300ms）分段，不受 mixer
    on-demand 退出時序影響。"""
    fake = _make_fake_self()
    ConnectionMixin.start_browser_satellite_listening(fake, BrowserSpeakerOutput())
    assert fake._local_speaker._persistent is True
