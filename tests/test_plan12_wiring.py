"""Plan 12 god-class 接線（T3 sub-2a）— flag + mixer 實例化 + ensure-playing。

flag=off：mixer None、_ensure_mixer_playing no-op（舊路徑零改變）。
flag=on：cog 持 LocalMixingAudioSource、ensure 在 idle vc 上 play 一個 MixerPlaybackAdapter。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from local_mixing_source import LocalMixingAudioSource, MixerPlaybackAdapter


def _make_cog(plan12: bool, monkeypatch):
    if plan12:
        monkeypatch.setenv("PLAN12_LOCAL_MIX", "true")
    else:
        monkeypatch.delenv("PLAN12_LOCAL_MIX", raising=False)
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.tts_engine = MagicMock()
    bot.tts_engine.get_estimated_duration.return_value = 2.0
    with patch("discord_voice_engine.faster_whisper", None, create=True):
        from discord_voice_engine import DiscordVoiceEngine
        bot.engine = DiscordVoiceEngine(bot)
    with patch("discord.ext.tasks.loop", lambda *a, **kw: lambda f: f), \
         patch("cogs.voice_controller.DepartureStats", MagicMock), \
         patch("cogs.voice_controller.ConsentManager", MagicMock):
        from cogs.voice_controller import VoiceController
        return VoiceController(bot)


def _idle_vc():
    vc = MagicMock()
    vc.is_connected.return_value = True
    vc.is_playing.return_value = False
    return vc





def test_flag_on_builds_mixer_and_ensure_plays(monkeypatch):
    cog = _make_cog(plan12=True, monkeypatch=monkeypatch)
    assert cog._plan12 is True
    assert isinstance(cog._mixer, LocalMixingAudioSource)
    vc = _idle_vc()
    assert cog._ensure_mixer_playing(vc) is True
    assert vc.play.call_count == 1
    assert isinstance(vc.play.call_args.args[0], MixerPlaybackAdapter)


def test_flag_on_ensure_idempotent_when_playing(monkeypatch):
    cog = _make_cog(plan12=True, monkeypatch=monkeypatch)
    vc = _idle_vc()
    vc.is_playing.return_value = True
    assert cog._ensure_mixer_playing(vc) is False
    assert not vc.play.called


# ── T4：狀態欄位委派 mixer ─────────────────────────────────────────────────────

def test_flag_on_state_fields_delegate_to_mixer(monkeypatch):
    import numpy as np
    cog = _make_cog(plan12=True, monkeypatch=monkeypatch)
    assert cog.is_playing_audio is False          # mixer idle
    assert cog.tts_queue_duration == 0.0
    cog._mixer.push_tts(np.zeros(48000 * 2, dtype=np.float32))  # 1s
    assert cog.is_playing_audio is True            # 20+ reader 自然看到
    assert cog.tts_queue_duration == pytest.approx(1.0, abs=0.01)





# ── 2c：play_tts flag=on → render + push mixer ────────────────────────────────

@pytest.mark.asyncio
async def test_play_tts_flag_on_pushes_to_mixer(monkeypatch):
    import numpy as np
    cog = _make_cog(plan12=True, monkeypatch=monkeypatch)
    cog.game_mode = False
    cog._tts_protected = True   # 繞過 silence gate
    cog.stream_mode = False
    cog._tts_interrupted = False
    vc = _idle_vc()
    cog.bot.voice_clients = [vc]

    # streaming render：mock 成「逐幀 push 進 mixer」（真的接 edge-tts+ffmpeg 不適合單測）
    async def _fake_stream(text, **kw):
        cog._mixer.push_tts(np.full(960 * 2, 0.3, dtype=np.float32))
        return 1
    cog._stream_tts_to_mixer = _fake_stream
    await cog.play_tts("哈囉馬文")
    assert not cog._mixer.is_idle()      # TTS 已 push 進 mixer
    assert vc.play.called                # ensure_mixer_playing 啟動 adapter
