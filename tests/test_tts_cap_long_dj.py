"""9/30：DJ 串場改成不截斷後，口白可能超過 30s（實測原稿 173 字 ≈ 41s）。
mixer TTS 層預設上限 30s 會把整段拒收、無聲消失——預設放寬到 60s。"""
from __future__ import annotations

import logging
from unittest.mock import MagicMock

import numpy as np
import pytest

from local_mixing_source import LocalMixingAudioSource, _SAMPLES_PER_SEC


def test_default_mixer_accepts_45s_dj():
    mix = LocalMixingAudioSource()
    assert mix.push_tts(np.zeros(int(45 * _SAMPLES_PER_SEC), dtype=np.float32)) is True


def test_default_mixer_still_caps_backlog():
    mix = LocalMixingAudioSource()
    assert mix.push_tts(np.zeros(int(61 * _SAMPLES_PER_SEC), dtype=np.float32)) is False


@pytest.mark.asyncio
async def test_play_dj_on_tts_layer_warns_when_push_rejected(caplog, tmp_path):
    from cogs.voice_controller_playback import PlaybackMixin

    class _Cog:
        play_dj_on_tts_layer = PlaybackMixin.play_dj_on_tts_layer

    cog = _Cog()
    f = tmp_path / "dj.mp3"
    f.write_bytes(b"x")

    async def _to_f32(**_kw):
        return np.zeros(4800, dtype=np.float32)

    cog._ffmpeg_to_f32 = _to_f32
    cog._ensure_mixer_playing = MagicMock()
    cog._resolve_playback_device = MagicMock()
    cog._mixer = MagicMock()
    cog._mixer.push_tts = MagicMock(return_value=False)
    with caplog.at_level(logging.WARNING):
        ok = await cog.play_dj_on_tts_layer(str(f), text="很長的口白")
    assert ok is False
    assert any("拒收" in r.getMessage() for r in caplog.records)
