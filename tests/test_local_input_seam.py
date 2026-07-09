"""
tests/test_local_input_seam.py

TDD：local 模式輸入接縫（ConnectionMixin.start_local_listening）。先紅後綠。

驗六件事：
(a) _local_mode = True
(b) _local_speaker 是 LocalSpeakerDevice
(c) LocalMicSink 以 bot.engine.process_audio_slice 為 callback、engine.sink 被賦值
(d) 麥克風擷取以 loop.create_task 非阻塞排程
(e) consent 換成 always-allow stub
(f) engine.start() 被呼叫（VAD watchdog 起來）
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from cogs.voice_controller_connection import ConnectionMixin
from marvin_voice_core.local_mic_sink import LocalMicSink
from marvin_voice_core.playback_device import LocalSpeakerDevice


def _make_fake_self():
    """造 VoiceController mock self，含 engine/loop。"""
    fake = MagicMock()
    fake.bot.engine.process_audio_slice = AsyncMock()
    fake.bot.engine.start = MagicMock()
    fake.bot.loop = MagicMock()
    # 讓 set_local_speaker 真的設 _local_speaker（side_effect 模擬真實行為）
    fake.set_local_speaker.side_effect = lambda device: setattr(fake, "_local_speaker", device)
    return fake


# ── (a) _local_mode 設為 True ─────────────────────────────────────────────────

def test_start_local_listening_sets_local_mode_true():
    fake = _make_fake_self()
    ConnectionMixin.start_local_listening(fake)
    assert fake._local_mode is True


# ── (b) _local_speaker 是 LocalSpeakerDevice ──────────────────────────────────

def test_start_local_listening_local_speaker_is_local_speaker_device():
    fake = _make_fake_self()
    ConnectionMixin.start_local_listening(fake)
    assert isinstance(fake._local_speaker, LocalSpeakerDevice)


# ── (c-1) engine.sink 被賦值為 LocalMicSink ───────────────────────────────────

def test_start_local_listening_assigns_local_mic_sink_to_engine_sink():
    fake = _make_fake_self()
    ConnectionMixin.start_local_listening(fake)
    assert isinstance(fake.bot.engine.sink, LocalMicSink)


# ── (c-2) LocalMicSink callback 綁 process_audio_slice ───────────────────────

def test_start_local_listening_sink_callback_is_process_audio_slice():
    fake = _make_fake_self()
    ConnectionMixin.start_local_listening(fake)
    sink = fake.bot.engine.sink
    assert isinstance(sink, LocalMicSink)
    assert sink.on_speech_cut_callback is fake.bot.engine.process_audio_slice


# ── (d) 麥克風以 loop.create_task 非阻塞排程 ─────────────────────────────────

def test_start_local_listening_schedules_sink_start_via_create_task():
    """start_local_listening 回傳後，loop.create_task 必須被呼叫過（非阻塞）。"""
    fake = _make_fake_self()
    ConnectionMixin.start_local_listening(fake)
    fake.bot.loop.create_task.assert_called_once()


# ── (e) consent 換成 always-allow stub ───────────────────────────────────────

def test_start_local_listening_consent_allows_any_speaker():
    fake = _make_fake_self()
    ConnectionMixin.start_local_listening(fake)
    assert fake.consent.is_consented("Alice") is True
    assert fake.consent.is_consented("Bob") is True


def test_start_local_listening_consent_has_seen_notice():
    fake = _make_fake_self()
    ConnectionMixin.start_local_listening(fake)
    assert fake.consent.has_seen_notice("Charlie") is True


# ── (f) engine.start() 被呼叫（VAD watchdog 起來）───────────────────────────

def test_start_local_listening_calls_engine_start():
    fake = _make_fake_self()
    ConnectionMixin.start_local_listening(fake)
    fake.bot.engine.start.assert_called_once()


# ── (T2b) LocalMicSink 的 on_speech_start_callback 綁 engine._handle_raw_speech_start ─

def test_start_local_listening_wires_onset_callback_to_engine_handler():
    fake = _make_fake_self()
    ConnectionMixin.start_local_listening(fake)
    sink = fake.bot.engine.sink
    assert isinstance(sink, LocalMicSink)
    assert sink.on_speech_start_callback is fake.bot.engine._handle_raw_speech_start
