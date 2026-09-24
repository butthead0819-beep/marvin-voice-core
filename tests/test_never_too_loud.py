"""絕不超大音量（2026-09-24 使用者：寧願悶或小聲也絕對不能超大音量輸出）。

源頭：音樂未量測/量測失敗用保守增益、放大看真峰值、DJ Mix 口白統一響度。
後防線：mixer 輸出限幅（audio_mixing.limit_frame）。
"""
from __future__ import annotations

import shutil
import subprocess
import wave
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

import audio_mixing as am
import loudness_norm as ln
from local_mixing_source import FRAME_SAMPLES, LocalMixingAudioSource


def _frame(peak: float) -> np.ndarray:
    x = np.zeros(FRAME_SAMPLES, dtype=np.float32)
    x[::7] = peak
    x[3::11] = -peak
    return x


# ── limit_frame ──────────────────────────────────────────────────────────────

def test_limit_frame_caps_loud_frame():
    out, g, hit = am.limit_frame(_frame(2.0), 1.0)
    assert float(np.max(np.abs(out))) <= am.LIMIT_CEILING + 1e-6
    assert g == pytest.approx(am.LIMIT_CEILING / 2.0)
    assert hit is True


def test_limit_frame_passthrough_when_under_ceiling():
    fr = _frame(0.5)
    out, g, hit = am.limit_frame(fr, 1.0)
    assert np.array_equal(out, fr)
    assert g == 1.0
    assert hit is False


def test_limit_frame_releases_slowly_back_to_unity():
    _, g, _ = am.limit_frame(_frame(2.0), 1.0)
    prev = g
    for _ in range(200):
        _, g, _ = am.limit_frame(_frame(0.1), prev)
        assert g - prev <= am.LIMIT_RELEASE_PER_FRAME + 1e-6
        prev = g
    assert g == pytest.approx(1.0)


def test_limit_frame_ramp_starts_from_prev_gain():
    fr = np.full(FRAME_SAMPLES, 0.4, dtype=np.float32)
    out, g, _ = am.limit_frame(fr, 0.5)
    assert out[0] == pytest.approx(0.4 * 0.5)
    assert out[1] == pytest.approx(0.4 * 0.5)
    assert g == pytest.approx(0.5 + am.LIMIT_RELEASE_PER_FRAME)


class _LoudMusic:
    def read(self):
        return np.full(FRAME_SAMPLES, 0.99, dtype=np.float32).tobytes()


def test_mixer_output_never_exceeds_ceiling():
    mix = LocalMixingAudioSource(seed=0)
    mix.set_volume(4.0, immediate=True)
    mix.set_music_source(_LoudMusic())
    mix.push_tts(np.full(FRAME_SAMPLES * 20, 0.9, dtype=np.float32))
    peak = 0
    for _ in range(100):
        s16 = np.frombuffer(mix.read(), dtype=np.int16)
        peak = max(peak, int(np.max(np.abs(s16.astype(np.int32)))))
    assert peak <= int(am.LIMIT_CEILING * 32768) + 2
    assert mix._limit_hits > 0


# ── loudness_norm ────────────────────────────────────────────────────────────

def test_unmeasured_gain_is_conservative():
    assert ln.compute_loudness_gain(None) == ln.UNMEASURED_GAIN
    assert ln.UNMEASURED_GAIN < 1.0


def test_gain_capped_by_true_peak():
    assert ln.compute_loudness_gain(-30.0, peak_dbfs=-3.0) == pytest.approx(
        ln.PEAK_CEILING / 10 ** (-3.0 / 20))


def test_gain_peak_cap_can_go_below_min_gain():
    assert ln.compute_loudness_gain(-14.0, peak_dbfs=12.0) < ln.MIN_GAIN


def test_gain_without_peak_unchanged():
    assert ln.compute_loudness_gain(-8.0) == pytest.approx(10 ** (-6 / 20))


_EBUR_STDERR = """
[Parsed_ebur128_0 @ 0x1] t: 0.1  TARGET:-23 LUFS    M: -30.1 S:-120.7     I: -30.1 LUFS       LRA:   0.0 LU  TPK:  -2.5  -2.6 dBFS
[Parsed_ebur128_0 @ 0x1] Summary:

  Integrated loudness:
    I:         -12.3 LUFS
    Threshold: -22.5 LUFS

  Loudness range:
    LRA:         4.1 LU

  True peak:
    Peak:       -1.2 dBFS
"""


def test_parse_true_peak_from_summary():
    assert ln.parse_ebur128_true_peak(_EBUR_STDERR) == pytest.approx(-1.2)


def test_parse_true_peak_missing_returns_none():
    assert ln.parse_ebur128_true_peak("Summary:\n  I: -14.0 LUFS\n") is None
    assert ln.parse_ebur128_true_peak("") is None


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="需要 ffmpeg")
def test_parse_true_peak_real_ffmpeg(tmp_path):
    sr = 48000
    t = np.arange(sr * 2) / sr
    x = (0.5 * np.sin(2 * np.pi * 1000 * t) * 32767).astype(np.int16)
    p = tmp_path / "sine.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(x.tobytes())
    r = subprocess.run(["ffmpeg", "-nostats", "-i", str(p), "-af", "ebur128=peak=true",
                        "-f", "null", "-"], capture_output=True, text=True)
    assert ln.parse_ebur128_true_peak(r.stderr) == pytest.approx(-6.0, abs=0.5)


# ── _measure_norm_gain_bg 用峰值上限 ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_measure_uses_true_peak_and_caps_gain():
    from cogs.music_cog import MusicCog
    cog = MusicCog(bot=MagicMock())
    stderr = b"Summary:\n  Integrated loudness:\n    I:  -30.0 LUFS\n  True peak:\n    Peak:  -3.0 dBFS\n"
    with patch("asyncio.create_subprocess_exec") as mock_exec, \
         patch("loudness_norm.sample_positions", return_value=[10.0]):
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"", stderr))
        mock_exec.return_value = proc
        await cog._measure_norm_gain_bg("https://t/song", duration=200.0,
                                        highlight_start_s=0.0, info={"duration": 200.0})
    args = mock_exec.call_args.args
    assert "ebur128=peak=true" in args
    assert cog._stream_norm_gain["https://t/song"] == pytest.approx(
        ln.PEAK_CEILING / 10 ** (-3.0 / 20))


# ── DJ Mix filter_complex ─────────────────────────────────────────────────────

def test_music_cog_dj_mix_uses_shared_filter():
    import inspect
    import cogs.music_cog as mc
    assert mc.dj_mix_filter_complex is am.dj_mix_filter_complex
    assert "_DJ_INTERJECTION_VOLUME" not in inspect.getsource(mc)


def test_dj_mix_filter_uses_uniform_tts_loudness():
    from intent_agents.volume_agent import calculate_tts_gain
    fc = am.dj_mix_filter_complex(0.35, calculate_tts_gain(0.35))
    assert am.TTS_LOUDNESS_AF in fc
    assert f"volume={calculate_tts_gain(0.35):.3f}[dj_q]" in fc
    assert "volume=0.300" not in fc
    assert "loudnorm=I=-14:TP=-1.5:LRA=11,volume=0.350[music]" in fc


# ── 播放迴圈：沒量好的歌用保守增益 ─────────────────────────────────────────────

class _VolMixer:
    def __init__(self):
        self._seq = [True, False]
        self.volumes = []

    def set_music_source(self, s):
        pass

    def has_music(self):
        return self._seq.pop(0) if self._seq else False

    def clear_music(self):
        pass

    def set_volume(self, v):
        self.volumes.append(v)


class _Dev:
    def is_connected(self):
        return True


class _Exhausted:
    def read(self):
        return b""

    def cleanup(self):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("norm_gain, expected", [({}, ln.UNMEASURED_GAIN), ({"u": 1.5}, 1.5)])
async def test_play_loop_uses_unmeasured_gain_until_measured(norm_gain, expected):
    from types import SimpleNamespace
    from cogs.voice_controller_playback import PlaybackMixin
    mixer = _VolMixer()
    fake = SimpleNamespace(_mixer=mixer, _ensure_mixer_playing=lambda d: None,
                           _stream_norm_gain=norm_gain, _current_stream_url="u",
                           stream_volume=0.5)
    await PlaybackMixin._mixer_play_music(fake, _Dev(), _Exhausted(),
                                          still_active=lambda: True, volume_attr="stream_volume")
    assert mixer.volumes and mixer.volumes[0] == pytest.approx(0.5 * expected)


@pytest.mark.asyncio
async def test_play_stream_song_dj_mix_uses_tts_gain_for_narration(tmp_path, monkeypatch):
    """use_mix 分支實際組出的 ffmpeg 選項：口白過統一濾鏡、音量 = calculate_tts_gain(stream_volume)。"""
    import discord
    from cogs.music_cog import MusicCog
    from intent_agents.volume_agent import calculate_tts_gain
    dj = tmp_path / "dj.mp3"
    dj.write_bytes(b"x")
    cog = MusicCog(bot=MagicMock())
    cog.stream_volume = 0.35
    vc = MagicMock()
    vc._resolve_playback_device.return_value = MagicMock()
    vc._mixer_play_music = AsyncMock()
    cog._vc = lambda: vc
    captured = {}

    def fake_ffmpeg(url, before_options=None, options=None):
        captured["options"] = options
        return MagicMock()

    monkeypatch.setattr(discord, "FFmpegPCMAudio", fake_ffmpeg)
    await cog.play_stream_song("https://t/song", "t", dj_audio_path=str(dj))
    opts = captured["options"]
    assert am.TTS_LOUDNESS_AF in opts
    assert f"volume={calculate_tts_gain(0.35):.3f}[dj_q]" in opts
