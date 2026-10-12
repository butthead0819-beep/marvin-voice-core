"""統一輸出增益架構（內容層 / 混音層 / 出口層）回歸測試。

驗證重點（見任務設計）：
  1. 車機 tap 固定走出口層 car_volume=1.0，不受 Discord 出口音量（set_volume）影響。
  2. DJ Mix 情境回歸：音樂內容增益（set_music_gain）維持 1.0 時，車機拿到的是
     滿幅音樂，不會被 Discord 使用者音量（例如預設 0.10）錯誤反推成一成大小。
  3. TTS 跟音樂一樣走出口音量：Discord 端 TTS 音量 == 車機音量 × Discord 出口音量。
  4. 限幅器在出口層之前、作用在參考準位上，車機那份一樣受保護。
  5. set_music_gain 逐幀平滑漸變，不瞬跳。
  6. paused / on-demand idle 時車機 tap 跟 Discord 輸出同步（silence / b""）。
  7. _mixer_play_music 的 100ms sync loop：norm_gain 開關決定 set_music_gain 吃 ng 還是
     固定 1.0；volume_attr=None（intro / 本地檔）完全不碰 mixer 增益。
  8. DJ Mix 呼叫端：play_stream_song 的 use_mix 分支傳 volume_attr="stream_volume",
     norm_gain=False，filter_complex 的音樂段不再烤使用者音量（固定 1.000）。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

import audio_mixing as am
from local_mixing_source import FRAME_BYTES_S16, FRAME_SAMPLES, LocalMixingAudioSource


def _rms_s16(frame: bytes) -> float:
    arr = np.frombuffer(frame, dtype=np.int16).astype(np.float64) / 32768.0
    return float(np.sqrt(np.mean(arr ** 2)))


class _ConstMusic:
    """常數振幅立體聲 f32 音樂層 fake（跟既有 _FakeMusic 同慣例：全幀同一個值）。"""

    def __init__(self, value: float, frames: int = 200):
        self._value = value
        self._left = frames

    def read(self) -> bytes:
        if self._left <= 0:
            return b""
        self._left -= 1
        return np.full(FRAME_SAMPLES, self._value, dtype=np.float32).tobytes()


class _FakeTap:
    def __init__(self):
        self.frames: list[bytes] = []

    def write(self, frame: bytes) -> None:
        self.frames.append(frame)


# ── 1. 車機不受 Discord 音量影響 ────────────────────────────────────────────

def test_car_tap_unaffected_by_discord_volume():
    results = {}
    for discord_vol in (0.10, 0.5, 1.0):
        tap = _FakeTap()
        mix = LocalMixingAudioSource(seed=1)
        mix.set_tap(tap)
        mix.set_volume(discord_vol, immediate=True)
        mix.set_music_source(_ConstMusic(0.3, frames=50))
        discord_frames = [mix.read() for _ in range(20)]
        tap_rms = float(np.mean([_rms_s16(f) for f in tap.frames[-10:]]))
        discord_rms = float(np.mean([_rms_s16(f) for f in discord_frames[-10:]]))
        results[discord_vol] = (tap_rms, discord_rms)

    tap_values = [v[0] for v in results.values()]
    assert max(tap_values) - min(tap_values) <= 0.01 * max(tap_values)
    for discord_vol, (tap_rms, discord_rms) in results.items():
        assert discord_rms == pytest.approx(tap_rms * discord_vol, rel=0.02)


# ── 2. DJ Mix 情境回歸：music_gain=1.0 時車機拿到滿幅、不被 Discord 音量反推 ──

def test_dj_mix_regression_music_gain_unity_ignores_discord_volume():
    tap = _FakeTap()
    mix = LocalMixingAudioSource(seed=2)
    mix.set_tap(tap)
    mix.set_music_gain(1.0, immediate=True)
    mix.set_volume(0.10, immediate=True)  # Discord 使用者音量預設值
    mix.set_music_source(_ConstMusic(0.3, frames=50))
    for _ in range(20):
        mix.read()
    tap_rms = float(np.mean([_rms_s16(f) for f in tap.frames[-10:]]))
    assert tap_rms == pytest.approx(0.3, rel=0.03)  # 不是被烤成 10%


# ── 3. TTS 也走出口音量，不再各自補償 ───────────────────────────────────────

def test_tts_follows_exit_volume_not_separately_boosted():
    tap = _FakeTap()
    mix = LocalMixingAudioSource(seed=3)
    mix.set_tap(tap)
    mix.set_volume(0.10, immediate=True)
    mix.push_tts(np.full(FRAME_SAMPLES * 20, 0.4, dtype=np.float32))
    discord_frames = [mix.read() for _ in range(20)]
    tap_rms = float(np.mean([_rms_s16(f) for f in tap.frames[:15]]))
    discord_rms = float(np.mean([_rms_s16(f) for f in discord_frames[:15]]))
    assert tap_rms == pytest.approx(0.4, rel=0.03)  # tts_gain 預設 1.0
    assert discord_rms == pytest.approx(tap_rms * 0.10, rel=0.05)


# ── 4. 車機也受限幅器保護 ────────────────────────────────────────────────────

def test_car_tap_has_limiter_protection():
    tap = _FakeTap()
    mix = LocalMixingAudioSource(seed=4)
    mix.set_tap(tap)
    mix.set_volume(1.0, immediate=True)
    mix.set_music_source(_ConstMusic(0.89, frames=10))
    mix.push_tts(np.full(FRAME_SAMPLES * 5, 0.79, dtype=np.float32))
    mix.read()
    assert tap.frames, "tap 應該收到幀"
    s16 = np.frombuffer(tap.frames[-1], dtype=np.int16)
    peak = int(np.max(np.abs(s16.astype(np.int32))))
    assert peak <= int(am.LIMIT_CEILING * 32767) + 3


# ── 5. music_gain ramp 無跳階 ────────────────────────────────────────────────

def test_music_gain_ramps_without_jump():
    mix = LocalMixingAudioSource(seed=5)
    mix.set_music_source(_ConstMusic(0.3, frames=500))
    mix.set_music_gain(0.4)
    for _ in range(20):
        mix.read()
    assert mix._music_gain_cur == pytest.approx(0.4, abs=0.01)

    mix.set_music_gain(2.0)
    prev = mix._music_gain_cur
    max_step = 0.0
    for _ in range(60):
        mix.read()
        max_step = max(max_step, abs(mix._music_gain_cur - prev))
        prev = mix._music_gain_cur
    assert max_step <= 0.04 + 1e-6


# ── 6. paused / idle：車機跟 Discord 一致 ───────────────────────────────────

def test_paused_tap_receives_silence():
    tap = _FakeTap()
    mix = LocalMixingAudioSource(seed=6)
    mix.set_tap(tap)
    mix.set_music_source(_ConstMusic(0.3, frames=500))
    mix.read()  # 先出聲確認 tap 走正常內容路徑
    mix.set_paused(True)
    mix.read()
    assert tap.frames[-1] == b"\x00" * FRAME_BYTES_S16


def test_on_demand_idle_tap_gets_empty_after_grace():
    tap = _FakeTap()
    mix = LocalMixingAudioSource(seed=7, on_demand=True, idle_grace_s=0.02)
    mix.set_tap(tap)
    for _ in range(5):
        mix.read()
    assert tap.frames[-1] == b""


# ── 7. _mixer_play_music 的 100ms sync loop ─────────────────────────────────

class _RecordingMixer:
    def __init__(self):
        self._seq = [True, False]
        self.music_gain_calls: list[float] = []
        self.volume_calls: list[float] = []
        self.events: list[tuple] = []

    def set_music_source(self, s, fade_in_s=0.0):
        self.events.append(("source",))

    def has_music(self):
        return self._seq.pop(0) if self._seq else False

    def clear_music(self):
        pass

    def set_music_gain(self, g, immediate=False):
        self.music_gain_calls.append(g)
        self.events.append(("gain", g, immediate))

    def set_volume(self, v, immediate=False):
        self.volume_calls.append(v)


class _Dev:
    def is_connected(self):
        return True


class _Exhausted:
    def read(self):
        return b""

    def cleanup(self):
        pass


@pytest.mark.asyncio
async def test_mixer_play_music_norm_gain_true_sets_measured_gain():
    from cogs.voice_controller_playback import PlaybackMixin
    mixer = _RecordingMixer()
    fake = SimpleNamespace(_mixer=mixer, _ensure_mixer_playing=lambda d: None,
                           _stream_norm_gain={"u": 0.6}, _current_stream_url="u",
                           stream_volume=0.7)
    await PlaybackMixin._mixer_play_music(
        fake, _Dev(), _Exhausted(), still_active=lambda: True,
        volume_attr="stream_volume", norm_gain=True)
    assert mixer.music_gain_calls and mixer.music_gain_calls[0] == pytest.approx(0.6)
    assert mixer.volume_calls and mixer.volume_calls[0] == pytest.approx(0.7)


@pytest.mark.asyncio
async def test_mixer_play_music_norm_gain_false_uses_unity():
    from cogs.voice_controller_playback import PlaybackMixin
    mixer = _RecordingMixer()
    fake = SimpleNamespace(_mixer=mixer, _ensure_mixer_playing=lambda d: None,
                           _stream_norm_gain={"u": 0.6}, _current_stream_url="u",
                           stream_volume=0.7)
    await PlaybackMixin._mixer_play_music(
        fake, _Dev(), _Exhausted(), still_active=lambda: True,
        volume_attr="stream_volume", norm_gain=False)
    assert mixer.music_gain_calls and mixer.music_gain_calls[0] == pytest.approx(1.0)
    assert mixer.volume_calls and mixer.volume_calls[0] == pytest.approx(0.7)


@pytest.mark.asyncio
async def test_mixer_play_music_volume_attr_none_does_not_touch_mixer_gain():
    from cogs.voice_controller_playback import PlaybackMixin
    mixer = _RecordingMixer()
    fake = SimpleNamespace(_mixer=mixer, _ensure_mixer_playing=lambda d: None,
                           _stream_norm_gain={}, _current_stream_url=None)
    await PlaybackMixin._mixer_play_music(
        fake, _Dev(), _Exhausted(), still_active=lambda: True, volume_attr=None)
    assert mixer.music_gain_calls == []
    assert mixer.volume_calls == []


# ── 8. DJ Mix 呼叫端：play_stream_song use_mix 分支 ─────────────────────────

@pytest.mark.asyncio
async def test_play_stream_song_dj_mix_passes_norm_gain_false_and_unity_volume(tmp_path, monkeypatch):
    import discord
    from cogs.music_cog import MusicCog
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
    assert "volume=1.000[dj_q]" in opts
    assert "loudnorm=I=-14:TP=-1.5:LRA=11,volume=1.000[music]" in opts
    call_kwargs = vc._mixer_play_music.call_args.kwargs
    assert call_kwargs["volume_attr"] == "stream_volume"
    assert call_kwargs["norm_gain"] is False


@pytest.mark.asyncio
async def test_mixer_play_music_sets_new_song_gain_immediately_before_source():
    """換歌時 music gain 不准從上一首的值慢慢 ramp（ng 2.28→0.4 以 0.04/幀要 ~1s，
    新歌開頭會偏大聲）→ 掛上音源前就 immediate 設到新歌的增益。"""
    from cogs.voice_controller_playback import PlaybackMixin
    mixer = _RecordingMixer()
    fake = SimpleNamespace(_mixer=mixer, _ensure_mixer_playing=lambda d: None,
                           _stream_norm_gain={}, _current_stream_url="u",
                           stream_volume=0.7)
    await PlaybackMixin._mixer_play_music(
        fake, _Dev(), _Exhausted(), still_active=lambda: True,
        volume_attr="stream_volume", norm_gain=True)
    src_idx = mixer.events.index(("source",))
    assert ("gain", 0.4, True) in mixer.events[:src_idx]


def test_set_volume_clamped_to_unity_never_amplifies_past_limiter():
    """出口音量在限幅器之後 → 只准衰減，>1 夾到 1.0（絕不超大音量）。"""
    mix = LocalMixingAudioSource(seed=8)
    mix.set_volume(4.0, immediate=True)
    assert mix._volume_cur == 1.0
    mix.set_volume(-1.0, immediate=True)
    assert mix._volume_cur == 0.0
