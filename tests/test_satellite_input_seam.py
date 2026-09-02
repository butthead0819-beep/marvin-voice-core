"""
tests/test_satellite_input_seam.py

TDD：衛星模式輸入接縫（ConnectionMixin.start_satellite_listening）。先紅後綠。
mirror tests/test_local_input_seam.py，差異＝mic 來源是 WyomingSatelliteBridge、喇叭
輸出注入 WyomingSpeakerOutput、喚醒 Detection → duck。Discord 路徑不受影響（零硬體）。

驗：
(a) _local_mode = True（衛星共用 local 輸出接縫）
(b) engine.sink 是橋內部的 LocalMicSink（Sentinel 心跳監控同型）
(c) 橋 callback 綁 engine.process_audio_slice
(d) 重連迴圈以 loop.create_task 非阻塞排程
(e) consent 換 always-allow stub
(f) engine.start() 被呼叫
(g) _local_speaker 是 LocalSpeakerDevice、輸出注入 WyomingSpeakerOutput
(h) 喚醒 hook 接到 _on_satellite_wake；_on_satellite_wake 觸發 mixer.duck_for_wake
"""
from __future__ import annotations

from unittest.mock import MagicMock

from cogs.voice_controller_connection import ConnectionMixin
from marvin_voice_core.local_mic_sink import LocalMicSink
from marvin_voice_core.playback_device import LocalSpeakerDevice
from marvin_voice_core.wyoming_bridge import WyomingSatelliteBridge
from marvin_voice_core.wyoming_speaker_output import WyomingSpeakerOutput


def _make_fake_self():
    fake = MagicMock()
    fake.bot.engine.process_audio_slice = MagicMock()
    fake.bot.engine.start = MagicMock()
    fake.bot.loop = MagicMock()
    fake.set_local_speaker.side_effect = lambda device: setattr(fake, "_local_speaker", device)
    return fake


# ── (a) _local_mode = True ────────────────────────────────────────────────────

def test_start_satellite_sets_local_mode_true():
    fake = _make_fake_self()
    ConnectionMixin.start_satellite_listening(fake)
    assert fake._local_mode is True


# ── (b) engine.sink 是橋內部 LocalMicSink ────────────────────────────────────

def test_start_satellite_engine_sink_is_bridge_local_mic_sink():
    fake = _make_fake_self()
    ConnectionMixin.start_satellite_listening(fake)
    assert isinstance(fake.bot.engine.sink, LocalMicSink)
    assert fake.bot.engine.sink is fake._satellite_bridge.sink


# ── (c) 橋 callback 綁 process_audio_slice ────────────────────────────────────

def test_start_satellite_bridge_callback_is_process_audio_slice():
    fake = _make_fake_self()
    ConnectionMixin.start_satellite_listening(fake)
    bridge = fake._satellite_bridge
    assert isinstance(bridge, WyomingSatelliteBridge)
    assert bridge.sink.on_speech_cut_callback is fake.bot.engine.process_audio_slice


# ── (d) 重連迴圈以 loop.create_task 非阻塞排程 ───────────────────────────────

def test_start_satellite_schedules_reconnect_loop_via_create_task():
    fake = _make_fake_self()
    ConnectionMixin.start_satellite_listening(fake)
    fake.bot.loop.create_task.assert_called_once()


def test_start_satellite_mic_bridge_gate_off_skips_reconnect_loop(monkeypatch):
    """MARVIN_SATELLITE_MIC_BRIDGE=0 → 不排重連迴圈（Pi wyoming 退役時不洗 log）。
    其餘接線（local_mode / speaker output / consent）照舊完成。"""
    monkeypatch.setenv("MARVIN_SATELLITE_MIC_BRIDGE", "0")
    fake = _make_fake_self()
    ConnectionMixin.start_satellite_listening(fake)
    fake.bot.loop.create_task.assert_not_called()
    assert fake._local_mode is True
    assert isinstance(fake._local_speaker, LocalSpeakerDevice)


def test_start_satellite_mic_bridge_gate_default_on(monkeypatch):
    monkeypatch.delenv("MARVIN_SATELLITE_MIC_BRIDGE", raising=False)
    fake = _make_fake_self()
    ConnectionMixin.start_satellite_listening(fake)
    fake.bot.loop.create_task.assert_called_once()


# ── (e) consent always-allow stub ────────────────────────────────────────────

def test_start_satellite_consent_allows_any_speaker():
    fake = _make_fake_self()
    ConnectionMixin.start_satellite_listening(fake)
    assert fake.consent.is_consented("Alice") is True
    assert fake.consent.has_seen_notice("Bob") is True


# ── (f) engine.start() 被呼叫 ─────────────────────────────────────────────────

def test_start_satellite_calls_engine_start():
    fake = _make_fake_self()
    ConnectionMixin.start_satellite_listening(fake)
    fake.bot.engine.start.assert_called_once()


# ── (g) 喇叭輸出注入 WyomingSpeakerOutput ────────────────────────────────────

def test_start_satellite_speaker_output_is_wyoming():
    fake = _make_fake_self()
    ConnectionMixin.start_satellite_listening(fake)
    assert isinstance(fake._local_speaker, LocalSpeakerDevice)
    assert isinstance(fake._local_speaker._output, WyomingSpeakerOutput)


# ── extra_output：車 puck 跟家用喇叭共用同一顆 mixer，扇出兩路 ───────────────

def test_start_satellite_without_extra_output_stays_plain_wyoming():
    """沒給 extra_output（預設）→ 零行為改變，還是純 WyomingSpeakerOutput，不包 Tee。"""
    fake = _make_fake_self()
    ConnectionMixin.start_satellite_listening(fake)
    assert isinstance(fake._local_speaker._output, WyomingSpeakerOutput)


def test_start_satellite_with_extra_output_tees_both():
    from marvin_voice_core.tee_speaker_output import TeeSpeakerOutput

    fake = _make_fake_self()
    car_output = MagicMock()
    ConnectionMixin.start_satellite_listening(fake, extra_output=car_output)
    tee = fake._local_speaker._output
    assert isinstance(tee, TeeSpeakerOutput)
    assert isinstance(tee._outputs[0], WyomingSpeakerOutput)
    assert tee._outputs[1] is car_output


# ── (h) 喚醒 hook → duck ──────────────────────────────────────────────────────

def test_start_satellite_wires_detection_to_wake_hook():
    fake = _make_fake_self()
    ConnectionMixin.start_satellite_listening(fake)
    # 橋的 on_detection 就是 mixin 的 _on_satellite_wake（bound）
    assert fake._satellite_bridge._on_detection is fake._on_satellite_wake


def test_on_satellite_wake_ducks_music_when_mixer_present(monkeypatch):
    monkeypatch.setenv("MARVIN_WAKE_DUCK", "1")
    fake = MagicMock()
    ConnectionMixin._on_satellite_wake(fake, "mawen_v1")
    fake._mixer.duck_for_wake.assert_called_once()


def test_on_satellite_wake_respects_kill_switch(monkeypatch):
    monkeypatch.setenv("MARVIN_WAKE_DUCK", "0")
    fake = MagicMock()
    ConnectionMixin._on_satellite_wake(fake, "mawen_v1")
    fake._mixer.duck_for_wake.assert_not_called()


# ── tts_gain 預設 1.0 ─────────────────────────────────────────────────────────

def test_start_satellite_defaults_tts_gain_to_1_0(monkeypatch):
    """satellite 模式下音樂 1.0，Marvin TTS 也是 1.0。"""
    monkeypatch.delenv("MARVIN_TTS_GAIN", raising=False)
    fake = _make_fake_self()
    ConnectionMixin.start_satellite_listening(fake)
    assert fake._mixer._tts_gain == 1.0


def test_start_satellite_respects_env_tts_gain(monkeypatch):
    """MARVIN_TTS_GAIN 可覆蓋。"""
    monkeypatch.setenv("MARVIN_TTS_GAIN", "0.7")
    fake = _make_fake_self()
    ConnectionMixin.start_satellite_listening(fake)
    assert fake._mixer._tts_gain == 0.7



# ── (T2b) 橋內部 LocalMicSink 的 on_speech_start_callback 綁 engine handler ─────────

def test_start_satellite_wires_onset_callback_to_engine_handler():
    fake = _make_fake_self()
    ConnectionMixin.start_satellite_listening(fake)
    bridge = fake._satellite_bridge
    assert isinstance(bridge, WyomingSatelliteBridge)
    assert bridge.sink.on_speech_start_callback is fake.bot.engine._handle_raw_speech_start
