"""TDD — Marvin 每句話記一筆 metric=speech（核心規範 G1 + 補回 G2 interruption via n_speaking）。

見任務檔：quality_metrics.speech_requested + PlaybackMixin._record_speech。
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from quality_metrics import speech_requested
from tts_speak_policy import SpeakKind


def _make_cog():
    """照抄 tests/test_playback_tts_path.py 的 _make_cog()。"""
    bot = MagicMock()
    bot.guilds = []
    bot.cogs.get.return_value = None
    bot.tts_engine = MagicMock()
    bot.tts_engine.get_estimated_duration.return_value = 0.01

    vc = MagicMock()
    vc.is_connected.return_value = True
    bot.voice_clients = [vc]

    with patch("discord_voice_engine.faster_whisper", None, create=True):
        from discord_voice_engine import DiscordVoiceEngine
        engine = DiscordVoiceEngine(bot)
    bot.engine = engine

    with patch("discord.ext.tasks.loop", lambda *a, **kw: lambda f: f), \
         patch("cogs.voice_controller.DepartureStats", MagicMock), \
         patch("cogs.voice_controller.ConsentManager", MagicMock):
        from cogs.voice_controller import VoiceController
        cog = VoiceController(bot)

    cog.active_text_channel = AsyncMock()
    cog.active_text_channel.send = AsyncMock()
    cog.game_mode = False
    cog._tts_protected = False
    cog._tts_interrupted = False
    cog._tts_flush_requested = False
    cog.stream_mode = False
    cog.radio_mode = False
    cog.is_playing_audio = False
    cog.tts_queue_duration = 0.0

    cog._mixer = MagicMock()
    cog._mixer.tts_load_seconds.return_value = 0.0
    cog._stream_tts_to_mixer = AsyncMock(return_value=10)
    cog._ensure_mixer_playing = MagicMock(return_value=True)
    cog._wait_for_user_silence = AsyncMock(return_value=True)

    return cog, vc


def _fake_record_metric(monkeypatch, calls):
    def fake(metric, **kwargs):
        calls.append((metric, kwargs))
    monkeypatch.setattr("cogs.voice_controller_playback.record_metric", fake)


# ── speech_requested ───────────────────────────────────────────────────────

def test_speech_requested_true_for_requested_kinds():
    assert speech_requested("wake_reply") is True
    assert speech_requested("ack") is True
    assert speech_requested("song_ack") is True


def test_speech_requested_false_for_proactive_kinds():
    assert speech_requested("dj_narration") is False
    assert speech_requested("proactive_topic") is False
    assert speech_requested("proactive_manzai") is False
    assert speech_requested("不存在的kind") is False


# ── play_tts ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_play_tts_records_one_speech_metric(monkeypatch):
    calls = []
    _fake_record_metric(monkeypatch, calls)
    cog, _ = _make_cog()
    device = MagicMock()

    with patch.object(cog, "_resolve_playback_device", return_value=device), \
         patch.object(cog, "_tts_suppressed", AsyncMock(return_value=False)):
        cog._stream_tts_to_mixer = AsyncMock(return_value=50)
        await cog.play_tts("測試文字", kind=SpeakKind.WAKE_REPLY)

    speech_calls = [c for c in calls if c[0] == "speech"]
    assert len(speech_calls) == 1
    _, kwargs = speech_calls[0]
    assert kwargs["kind"] == "wake_reply"
    assert kwargs["dur_s"] == pytest.approx(1.0)
    assert kwargs["verdict"] == "play"
    assert kwargs["requested"] is True
    assert kwargs["kind_inferred"] is False
    assert kwargs["chars"] == len("測試文字")


@pytest.mark.asyncio
async def test_play_tts_ctx_captured_before_speaking(monkeypatch):
    """ctx 在開口前拍：_stream_tts_to_mixer 期間把 hot_chat 改成 True，紀錄的仍是開口前的 False。"""
    calls = []
    _fake_record_metric(monkeypatch, calls)
    cog, _ = _make_cog()
    device = MagicMock()
    from room_mood_state import RoomMoodState
    state = RoomMoodState(channel_id=0, hot_chat=False)
    cog._room_mood_store._states[0] = state

    async def _fake_stream(*a, **kw):
        state.hot_chat = True
        return 50

    with patch.object(cog, "_resolve_playback_device", return_value=device), \
         patch.object(cog, "_tts_suppressed", AsyncMock(return_value=False)):
        cog._stream_tts_to_mixer = _fake_stream
        await cog.play_tts("測試文字", kind=SpeakKind.WAKE_REPLY)

    speech_calls = [c for c in calls if c[0] == "speech"]
    assert len(speech_calls) == 1
    assert speech_calls[0][1]["hot_chat"] is False


@pytest.mark.asyncio
async def test_play_tts_without_kind_marks_inferred(monkeypatch):
    calls = []
    _fake_record_metric(monkeypatch, calls)
    cog, _ = _make_cog()
    device = MagicMock()

    with patch.object(cog, "_resolve_playback_device", return_value=device), \
         patch.object(cog, "_tts_suppressed", AsyncMock(return_value=False)):
        cog._stream_tts_to_mixer = AsyncMock(return_value=50)
        await cog.play_tts("測試文字")  # 沒傳 kind

    speech_calls = [c for c in calls if c[0] == "speech"]
    assert len(speech_calls) == 1
    assert speech_calls[0][1]["kind_inferred"] is True


@pytest.mark.asyncio
async def test_play_tts_zero_pushed_records_nothing(monkeypatch):
    calls = []
    _fake_record_metric(monkeypatch, calls)
    cog, _ = _make_cog()
    device = MagicMock()

    with patch.object(cog, "_resolve_playback_device", return_value=device), \
         patch.object(cog, "_tts_suppressed", AsyncMock(return_value=False)):
        cog._stream_tts_to_mixer = AsyncMock(return_value=0)
        await cog.play_tts("測試文字", kind=SpeakKind.WAKE_REPLY)

    assert [c for c in calls if c[0] == "speech"] == []


@pytest.mark.asyncio
async def test_tts_suppressed_drop_records_one_speech_metric(monkeypatch):
    """PROACTIVE_TOPIC + stream_mode=True → tts_speak_policy.POLICY 的 on_stream=DROP。"""
    calls = []
    _fake_record_metric(monkeypatch, calls)
    cog, _ = _make_cog()
    cog.stream_mode = True

    suppressed = await cog._tts_suppressed(
        text="冷場找話題", silent_during_stream=False, already_in_channel=True,
        bypass_stream_mute=False, kind=SpeakKind.PROACTIVE_TOPIC,
    )

    assert suppressed is True
    speech_calls = [c for c in calls if c[0] == "speech"]
    assert len(speech_calls) == 1
    _, kwargs = speech_calls[0]
    assert kwargs["verdict"] == "drop"
    assert kwargs["dur_s"] == 0.0
    assert kwargs["kind"] == "proactive_topic"


# ── play_dj_on_tts_layer ─────────────────────────────────────────────────

def _make_f32_seconds(n_seconds: float):
    import numpy as np
    return np.zeros(int(96000 * n_seconds), dtype="float32")


@pytest.mark.asyncio
async def test_play_dj_on_tts_layer_records_dj_narration_by_default(monkeypatch, tmp_path):
    calls = []
    _fake_record_metric(monkeypatch, calls)
    cog, _ = _make_cog()
    device = MagicMock()
    path = str(tmp_path / "dj.wav")
    with open(path, "wb"):
        pass

    with patch.object(cog, "_resolve_playback_device", return_value=device), \
         patch.object(cog, "_ffmpeg_to_f32", AsyncMock(return_value=_make_f32_seconds(1.0))):
        cog._mixer.push_tts = MagicMock(return_value=True)
        ok = await cog.play_dj_on_tts_layer(path, text="口白文字")

    assert ok is True
    speech_calls = [c for c in calls if c[0] == "speech"]
    assert len(speech_calls) == 1
    _, kwargs = speech_calls[0]
    assert kwargs["kind"] == "dj_narration"
    assert kwargs["src"] == "dj"
    assert kwargs["dur_s"] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_play_dj_on_tts_layer_records_custom_speech_kind(monkeypatch, tmp_path):
    calls = []
    _fake_record_metric(monkeypatch, calls)
    cog, _ = _make_cog()
    device = MagicMock()
    path = str(tmp_path / "dj.wav")
    with open(path, "wb"):
        pass

    with patch.object(cog, "_resolve_playback_device", return_value=device), \
         patch.object(cog, "_ffmpeg_to_f32", AsyncMock(return_value=_make_f32_seconds(1.0))):
        cog._mixer.push_tts = MagicMock(return_value=True)
        ok = await cog.play_dj_on_tts_layer(path, text="幫你點了", speech_kind="song_ack")

    assert ok is True
    speech_calls = [c for c in calls if c[0] == "speech"]
    assert len(speech_calls) == 1
    _, kwargs = speech_calls[0]
    assert kwargs["kind"] == "song_ack"
    assert kwargs["requested"] is True


@pytest.mark.asyncio
async def test_play_dj_on_tts_layer_sfx_records_nothing(monkeypatch, tmp_path):
    calls = []
    _fake_record_metric(monkeypatch, calls)
    cog, _ = _make_cog()
    device = MagicMock()
    path = str(tmp_path / "sfx.wav")
    with open(path, "wb"):
        pass

    with patch.object(cog, "_resolve_playback_device", return_value=device), \
         patch.object(cog, "_ffmpeg_to_f32", AsyncMock(return_value=_make_f32_seconds(1.0))), \
         patch("audio_mixing.peak_normalize_f32", side_effect=lambda f32, target_peak: f32):
        cog._mixer.push_tts = MagicMock(return_value=True)
        ok = await cog.play_dj_on_tts_layer(path, peak=0.1)

    assert ok is True
    assert [c for c in calls if c[0] == "speech"] == []


# ── _push_ack_tts ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_push_ack_tts_records_ack_on_success(monkeypatch):
    calls = []
    _fake_record_metric(monkeypatch, calls)
    cog, _ = _make_cog()
    cog._mixer.push_tts = MagicMock(return_value=True)

    import numpy as np
    f32 = np.zeros(96000, dtype="float32")
    with patch("ack_templates.text_for_file", return_value="好喔"):
        ok = cog._push_ack_tts(f32, "ack_ok.mp3")

    assert ok is True
    speech_calls = [c for c in calls if c[0] == "speech"]
    assert len(speech_calls) == 1
    assert speech_calls[0][1]["kind"] == "ack"


@pytest.mark.asyncio
async def test_push_ack_tts_records_nothing_on_failure(monkeypatch):
    calls = []
    _fake_record_metric(monkeypatch, calls)
    cog, _ = _make_cog()
    cog._mixer.push_tts = MagicMock(return_value=False)

    import numpy as np
    f32 = np.zeros(96000, dtype="float32")
    ok = cog._push_ack_tts(f32, "ack_fail.mp3")

    assert ok is False
    assert [c for c in calls if c[0] == "speech"] == []


# ── 容錯 ─────────────────────────────────────────────────────────────────

def test_record_speech_never_raises_when_record_metric_throws(monkeypatch):
    cog, _ = _make_cog()

    def boom(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr("cogs.voice_controller_playback.record_metric", boom)
    cog._record_speech("wake_reply", src="tts", dur_s=1.0, chars=3)  # 不應 raise


def test_speech_context_no_sink_returns_zero_speaking():
    cog, _ = _make_cog()
    cog.bot.engine = MagicMock()
    del cog.bot.engine.get_active_sink  # 模擬沒有這個方法

    hot, n = cog._speech_context()

    assert n == 0
