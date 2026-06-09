"""TDD: Plan 12 模式下 skip_if_busy ack 應仍播出

問題：nemoclaw ack category 設 skip_if_busy=True。
Plan 12 模式 is_playing_audio 永遠 True（mixer 播音樂）→ ack 永遠被跳過。
修法：Plan 12 路徑繞過 skip_if_busy / wait_if_busy guard（ack 走 push_tts overlay）。
"""
from __future__ import annotations
import asyncio
import numpy as np
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _make_cog(plan12: bool, is_playing_audio: bool = True):
    """最精簡的 VoiceController stub。"""
    from cogs.voice_controller import VoiceController

    # 最小 bot stub
    bot = MagicMock()
    bot.cogs.get.return_value = None

    cog = VoiceController.__new__(VoiceController)
    cog.bot = bot
    cog._plan12 = plan12
    cog._voice_client_override = None
    cog._speaker_lang = {}
    cog._storm_active = False

    # 偽造 connected voice client（Plan 12：vc.is_playing() = True）
    _vc = MagicMock()
    _vc.is_connected.return_value = True
    _vc.is_playing.return_value = True
    bot.voice_clients = [_vc]

    # mixer
    _mixer = MagicMock()
    _mixer.is_playing_audio = is_playing_audio
    _mixer.tts_load_seconds.return_value = 0.0
    cog._mixer = _mixer

    # _ffmpeg_to_f32 → 回傳有大小的 float32 array
    cog._ffmpeg_to_f32 = AsyncMock(return_value=np.zeros(100, dtype=np.float32))
    cog._ensure_mixer_playing = MagicMock()

    # 其他 gate：全部放行
    cog._active_ack_allowed = MagicMock(return_value=True)
    cog.playback_lock = asyncio.Lock()

    return cog, _mixer


@pytest.mark.asyncio
async def test_nemoclaw_ack_plays_in_plan12_when_busy():
    """Plan 12 + is_playing_audio=True → nemoclaw ack 仍應 push_tts。"""
    cog, mixer = _make_cog(plan12=True, is_playing_audio=True)

    with patch("glob.glob", return_value=["assets/acks/fake_nemo.mp3"]):
        await cog._play_ack("nemoclaw", speaker="狗與露")

    mixer.push_tts.assert_called_once()


@pytest.mark.asyncio
async def test_nemoclaw_ack_skips_in_non_plan12_when_busy():
    """非 Plan 12 + is_playing_audio=True → skip_if_busy 仍生效（維持原行為）。"""
    cog, mixer = _make_cog(plan12=False, is_playing_audio=True)

    with patch("glob.glob", return_value=["assets/acks/fake_nemo.mp3"]):
        await cog._play_ack("nemoclaw", speaker="狗與露")

    mixer.push_tts.assert_not_called()


@pytest.mark.asyncio
async def test_nemoclaw_ack_plays_in_plan12_idle():
    """Plan 12 + is_playing_audio=False（閒置）→ 也能播。"""
    cog, mixer = _make_cog(plan12=True, is_playing_audio=False)

    with patch("glob.glob", return_value=["assets/acks/fake_nemo.mp3"]):
        await cog._play_ack("nemoclaw", speaker="狗與露")

    mixer.push_tts.assert_called_once()
