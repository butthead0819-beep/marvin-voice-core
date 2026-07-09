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


# ── (T2b) 橋內部 LocalMicSink 的 on_speech_start_callback 綁 engine handler ─────────

def test_start_satellite_wires_onset_callback_to_engine_handler():
    fake = _make_fake_self()
    ConnectionMixin.start_satellite_listening(fake)
    bridge = fake._satellite_bridge
    assert isinstance(bridge, WyomingSatelliteBridge)
    assert bridge.sink.on_speech_start_callback is fake.bot.engine._handle_raw_speech_start
