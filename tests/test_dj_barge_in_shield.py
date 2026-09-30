"""9/30：DJ 口白被「有人開口就清 TTS 層」的 barge-in 整段清掉（14:17、14:22 實測，無 log）。
_protected_tts_window 只包住非阻塞的 push，推完保護就結束。改成講話類 TTS 推上 TTS 層後，
整段播完前都擋 barge-in：有人開口只走 note_player_speech 的 duck（80%→10%），不清掉。"""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import numpy as np
import pytest

from cogs.voice_controller import VoiceController
from cogs.voice_controller_playback import PlaybackMixin, barge_in_shielded
from tests.test_opus_encoder_race import _barge_fake


def test_barge_in_shielded_pure():
    now = 1000.0
    assert barge_in_shielded(1005.0, now) is True
    assert barge_in_shielded(999.0, now) is False
    assert barge_in_shielded(0.0, now) is False
    assert barge_in_shielded(MagicMock(), now) is False
    assert barge_in_shielded(None, now) is False


class _Cog:
    play_dj_on_tts_layer = PlaybackMixin.play_dj_on_tts_layer


def _playback_cog(tmp_path, push_ok=True, load_s=12.0):
    cog = _Cog()
    f = tmp_path / "dj.mp3"
    f.write_bytes(b"x")

    async def _to_f32(**_kw):
        return np.zeros(4800, dtype=np.float32)

    cog._ffmpeg_to_f32 = _to_f32
    cog._ensure_mixer_playing = MagicMock()
    cog._resolve_playback_device = MagicMock()
    cog._mixer = MagicMock()
    cog._mixer.push_tts = MagicMock(return_value=push_ok)
    cog._mixer.tts_load_seconds = MagicMock(return_value=load_s)
    return cog, str(f)


@pytest.mark.asyncio
async def test_speech_push_sets_shield_until_tts_layer_drains(tmp_path):
    cog, path = _playback_cog(tmp_path, load_s=12.0)
    t0 = time.time()
    assert await cog.play_dj_on_tts_layer(path, text="很長的口白") is True
    assert t0 + 11.5 <= cog._tts_barge_shield_until <= time.time() + 12.5


@pytest.mark.asyncio
async def test_sfx_push_does_not_set_shield(tmp_path):
    cog, path = _playback_cog(tmp_path)
    await cog.play_dj_on_tts_layer(path, peak=0.1)
    assert not hasattr(cog, "_tts_barge_shield_until")


@pytest.mark.asyncio
async def test_rejected_push_does_not_set_shield(tmp_path):
    cog, path = _playback_cog(tmp_path, push_ok=False)
    await cog.play_dj_on_tts_layer(path, text="口白")
    assert not hasattr(cog, "_tts_barge_shield_until")


def test_speech_start_during_shield_does_not_clear_tts():
    fake = _barge_fake()
    fake._tts_barge_shield_until = time.time() + 10.0
    fake._tts_interrupted = False
    VoiceController.handle_raw_speech_start(fake, "狗與露")
    fake._mixer.clear_tts.assert_not_called()
    assert fake._tts_interrupted is False
    fake._mixer.note_player_speech.assert_called_once()  # 仍走 duck


def test_speech_start_after_shield_expires_clears_tts():
    fake = _barge_fake()
    fake._tts_barge_shield_until = time.time() - 1.0
    fake._resolve_playback_device.return_value = MagicMock()
    VoiceController.handle_raw_speech_start(fake, "狗與露")
    fake._mixer.clear_tts.assert_called_once()
