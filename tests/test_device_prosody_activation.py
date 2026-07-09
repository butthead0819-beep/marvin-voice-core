"""tests/test_device_prosody_activation.py

TDD：device 韻律活化 (T6b-1)。先紅後綠。

驗三件事：
(A) LocalMicSink._process_chunk 每幀都呼叫 meta_analyzer.add_rms(user_id, rms)；
    meta_analyzer=None 時 no-op 不崩。
(B) VoiceMetaAnalyzer.calculate_prosody 回傳包含 mean_rms 鍵；
    無採樣時仍回 {}（不加 mean_rms）。
(C) start_local_listening 把 sink.meta_analyzer 接上 engine.meta_analyzer；
    start_satellite_listening 把 bridge.sink.meta_analyzer 接上 engine.meta_analyzer。
"""
from __future__ import annotations

import statistics
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

# ── 常數（對齊 test_local_mic_sink.py）──────────────────────────────────────────
FRAME_BYTES = 3840  # 20ms 48kHz stereo int16
SPEECH_VALUE = 8000  # RMS ≈ 8000，遠超任何靜態門檻


def _speech_frame() -> bytes:
    return (np.ones(FRAME_BYTES // 2, dtype=np.int16) * SPEECH_VALUE).tobytes()


def _silence_frame() -> bytes:
    return bytes(FRAME_BYTES)


# ════════════════════════════════════════════════════════════════════════════════
# (A) LocalMicSink._process_chunk 韻律採樣
# ════════════════════════════════════════════════════════════════════════════════

def test_process_chunk_feeds_add_rms_when_meta_analyzer_wired():
    """每幀 _process_chunk 對有掛 meta_analyzer 呼叫 add_rms(user_id, rms) 恰一次。"""
    from marvin_voice_core.local_mic_sink import LocalMicSink
    from marvin_voice_core.audio_utils import calculate_rms

    mock_meta = MagicMock()
    sink = LocalMicSink(MagicMock(), loop=None)
    sink.meta_analyzer = mock_meta

    frame = _speech_frame()
    expected_rms = calculate_rms(frame)
    sink._process_chunk(frame, 0.0)

    mock_meta.add_rms.assert_called_once_with(sink._user_id, expected_rms)


def test_process_chunk_no_crash_when_meta_analyzer_none():
    """meta_analyzer=None（預設）時 _process_chunk 不崩、add_rms 永不呼叫。"""
    from marvin_voice_core.local_mic_sink import LocalMicSink

    sink = LocalMicSink(MagicMock(), loop=None)
    assert sink.meta_analyzer is None
    # 不應拋出 AttributeError
    sink._process_chunk(_speech_frame(), 0.0)


def test_process_chunk_feeds_rms_for_silence_frame():
    """add_rms 在每幀（含靜默幀）呼叫——閾值判斷之前採樣。"""
    from marvin_voice_core.local_mic_sink import LocalMicSink
    from marvin_voice_core.audio_utils import calculate_rms

    mock_meta = MagicMock()
    sink = LocalMicSink(MagicMock(), loop=None)
    sink.meta_analyzer = mock_meta

    frame = _silence_frame()
    expected_rms = calculate_rms(frame)
    sink._process_chunk(frame, 0.0)

    mock_meta.add_rms.assert_called_once_with(sink._user_id, expected_rms)


# ════════════════════════════════════════════════════════════════════════════════
# (B) VoiceMetaAnalyzer.calculate_prosody 回傳 mean_rms
# ════════════════════════════════════════════════════════════════════════════════

def test_calculate_prosody_returns_mean_rms_when_samples_present():
    """採樣後 calculate_prosody 含 mean_rms = round(mean(samples), 2)。"""
    from voice_meta_analyzer import VoiceMetaAnalyzer

    uid = "local"
    samples = [100.0, 200.0, 300.0]
    analyzer = VoiceMetaAnalyzer()
    for r in samples:
        analyzer.add_rms(uid, r)

    result = analyzer.calculate_prosody(uid, text="hi", physical_duration=1.0)

    expected_mean_rms = round(statistics.mean(samples), 2)
    assert "mean_rms" in result
    assert result["mean_rms"] == expected_mean_rms


def test_calculate_prosody_existing_keys_unchanged():
    """mean_rms 純加法——wps/char_count/energy_variance/physical_duration/sample_count 不變。"""
    from voice_meta_analyzer import VoiceMetaAnalyzer

    uid = "local"
    analyzer = VoiceMetaAnalyzer()
    for r in [100.0, 200.0, 300.0]:
        analyzer.add_rms(uid, r)

    result = analyzer.calculate_prosody(uid, text="hi", physical_duration=1.0)

    for key in ("wps", "char_count", "energy_variance", "physical_duration", "sample_count"):
        assert key in result, f"existing key '{key}' missing"


def test_calculate_prosody_returns_empty_dict_when_user_has_no_samples():
    """無採樣的 user_id → calculate_prosody 回 {}（mean_rms 不出現）。"""
    from voice_meta_analyzer import VoiceMetaAnalyzer

    analyzer = VoiceMetaAnalyzer()
    result = analyzer.calculate_prosody("nonexistent", text="hi", physical_duration=1.0)
    assert result == {}


# ════════════════════════════════════════════════════════════════════════════════
# (C) 接線：start_local_listening / start_satellite_listening
# ════════════════════════════════════════════════════════════════════════════════

def _make_fake_self():
    """造 VoiceController mock self（對齊 test_local_input_seam.py 風格）。"""
    fake = MagicMock()
    fake.bot.engine.process_audio_slice = AsyncMock()
    fake.bot.engine.start = MagicMock()
    fake.bot.loop = MagicMock()
    fake.set_local_speaker.side_effect = lambda device: setattr(fake, "_local_speaker", device)
    return fake


def test_start_local_listening_wires_meta_analyzer_to_sink():
    """start_local_listening 後 sink.meta_analyzer is engine.meta_analyzer。"""
    from marvin_voice_core.local_mic_sink import LocalMicSink
    from cogs.voice_controller_connection import ConnectionMixin

    fake = _make_fake_self()
    ConnectionMixin.start_local_listening(fake)

    sink = fake.bot.engine.sink
    assert isinstance(sink, LocalMicSink)
    assert sink.meta_analyzer is fake.bot.engine.meta_analyzer


def test_start_satellite_listening_wires_meta_analyzer_to_bridge_sink():
    """start_satellite_listening 後 bridge.sink.meta_analyzer is engine.meta_analyzer。"""
    from marvin_voice_core.local_mic_sink import LocalMicSink
    from cogs.voice_controller_connection import ConnectionMixin

    fake = _make_fake_self()
    ConnectionMixin.start_satellite_listening(fake)

    bridge = fake._satellite_bridge
    assert isinstance(bridge.sink, LocalMicSink)
    assert bridge.sink.meta_analyzer is fake.bot.engine.meta_analyzer
