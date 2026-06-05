"""Tests for /marvin_say — 讓馬文用他的聲音念出使用者打的字。

行為契約：
  1. 把文字交給 play_tts（Marvin 預設聲音）
  2. protected：呼叫 play_tts 當下 _tts_protected 必須為 True
     （繞過靜默閘 / queue-drop guard，確保整句念完不被砍）
  3. 呼叫前清掉 _tts_interrupted（避免被前一次中斷旗標吞掉）
  4. play_tts 結束後還原 _tts_protected 原值（不 clobber 既有保護播放）
  5. 把文字貼回頻道
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_vc():
    """繞過 VoiceController 全建構，只搭測 marvin_say 需要的 attr。"""
    from cogs.voice_controller import VoiceController

    vc = VoiceController.__new__(VoiceController)
    vc.play_tts = AsyncMock()
    vc._tts_protected = False
    vc._tts_interrupted = True  # 預設髒，驗證指令會清掉
    return vc


def _make_interaction():
    interaction = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


@pytest.mark.asyncio
async def test_marvin_say_forwards_text_to_play_tts():
    from cogs.voice_controller import VoiceController
    vc = _make_vc()
    interaction = _make_interaction()

    await VoiceController.marvin_say.callback(vc, interaction, text="哈囉宇宙")

    vc.play_tts.assert_called_once()
    args, _ = vc.play_tts.call_args
    assert args[0] == "哈囉宇宙"


@pytest.mark.asyncio
async def test_marvin_say_is_protected_during_playback():
    """呼叫 play_tts 的當下 _tts_protected 必須為 True（繞過靜默閘）。"""
    from cogs.voice_controller import VoiceController
    vc = _make_vc()
    interaction = _make_interaction()

    seen = {}

    async def _capture(*a, **k):
        seen["protected_flag"] = vc._tts_protected
        seen["protected_kwarg"] = k.get("protected")

    vc.play_tts.side_effect = _capture

    await VoiceController.marvin_say.callback(vc, interaction, text="念這句")

    assert seen["protected_flag"] is True
    assert seen["protected_kwarg"] is True


@pytest.mark.asyncio
async def test_marvin_say_uses_macos_say_male_voice():
    """念字走 macOS say 男聲（force_macos=True），不走 edge-tts。"""
    from cogs.voice_controller import VoiceController
    vc = _make_vc()
    interaction = _make_interaction()

    await VoiceController.marvin_say.callback(vc, interaction, text="念這句")

    _, kwargs = vc.play_tts.call_args
    assert kwargs.get("force_macos") is True


@pytest.mark.asyncio
async def test_marvin_say_clears_interrupt_flag():
    from cogs.voice_controller import VoiceController
    vc = _make_vc()
    interaction = _make_interaction()

    seen = {}

    async def _capture(*a, **k):
        seen["interrupted"] = vc._tts_interrupted

    vc.play_tts.side_effect = _capture

    await VoiceController.marvin_say.callback(vc, interaction, text="x")

    assert seen["interrupted"] is False


@pytest.mark.asyncio
async def test_marvin_say_restores_protected_flag_after():
    """念完還原 _tts_protected 原值，不 clobber 既有保護播放。"""
    from cogs.voice_controller import VoiceController
    vc = _make_vc()
    vc._tts_protected = True  # 模擬指令觸發時已有保護播放在進行
    interaction = _make_interaction()

    await VoiceController.marvin_say.callback(vc, interaction, text="x")

    assert vc._tts_protected is True


@pytest.mark.asyncio
async def test_marvin_say_restores_protected_even_on_error():
    from cogs.voice_controller import VoiceController
    vc = _make_vc()
    interaction = _make_interaction()
    vc.play_tts.side_effect = RuntimeError("tts boom")

    with pytest.raises(RuntimeError):
        await VoiceController.marvin_say.callback(vc, interaction, text="x")

    assert vc._tts_protected is False


@pytest.mark.asyncio
async def test_marvin_say_posts_text_to_channel():
    from cogs.voice_controller import VoiceController
    vc = _make_vc()
    interaction = _make_interaction()

    await VoiceController.marvin_say.callback(vc, interaction, text="貼這句")

    interaction.response.defer.assert_awaited_once()
    interaction.followup.send.assert_awaited_once()
    sent = interaction.followup.send.call_args.args[0]
    assert "貼這句" in sent
