"""Tests for /marvin_manzai — 立刻讓馬文與 Marmo 進行雙人漫才表演。
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


pytestmark = pytest.mark.asyncio


def _make_vc():
    from cogs.voice_controller import VoiceController

    vc = VoiceController.__new__(VoiceController)
    vc.play_dual_dialogue = AsyncMock()
    vc._tts_protected = False
    vc._tts_interrupted = True
    vc.bot = MagicMock()
    vc.bot.router = MagicMock()
    
    # Mock conv_buffer
    vc.bot.engine = MagicMock()
    vc.bot.engine.conv_buffer = MagicMock()
    vc.bot.engine.conv_buffer.history = []
    
    return vc


def _make_interaction():
    interaction = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


async def test_marvin_manzai_uses_explicit_topic():
    """帶入明確 topic 參數時，應以其做為漫才內容。"""
    from cogs.voice_controller import VoiceController
    vc = _make_vc()
    interaction = _make_interaction()
    
    segments = [{"voice": "marvin", "text": "話說那天的雨..."}, {"voice": "marmo", "text": "你還敢提雨？"}]
    
    with patch("services.dialogue_generation.generate_dual_dialogue", AsyncMock(return_value=segments)) as mock_gen, \
         patch("services.dialogue_generation.make_gemini_dual_dialogue_llm_fn", MagicMock()):
        await VoiceController.marvin_manzai.callback(vc, interaction, topic="外面正在下雨")
        
        mock_gen.assert_called_once()
        args, kwargs = mock_gen.call_args
        assert kwargs.get("content_text") == "外面正在下雨"
        
    vc.play_dual_dialogue.assert_called_once_with(segments, interject=True)


async def test_marvin_manzai_extracts_recent_utterances_when_no_topic():
    """未給 topic 時，從 conv_buffer.history 提取最後 5 筆對白做為主題。"""
    from cogs.voice_controller import VoiceController
    vc = _make_vc()
    interaction = _make_interaction()
    
    # 填充 6 筆對話，預期只取最後 5 筆
    vc.bot.engine.conv_buffer.history = [
        {"speaker": "Alice", "text": "第0句"},
        {"speaker": "Bob", "text": "第1句"},
        {"speaker": "Alice", "text": "第2句"},
        {"speaker": "Bob", "text": "第3句"},
        {"speaker": "Alice", "text": "第4句"},
        {"speaker": "Bob", "text": "第5句"},
    ]
    
    with patch("services.dialogue_generation.generate_dual_dialogue", AsyncMock(return_value=[])) as mock_gen, \
         patch("services.dialogue_generation.make_gemini_dual_dialogue_llm_fn", MagicMock()):
        await VoiceController.marvin_manzai.callback(vc, interaction, topic=None)
        
        mock_gen.assert_called_once()
        _, kwargs = mock_gen.call_args
        expected = "Bob: 第1句\nAlice: 第2句\nBob: 第3句\nAlice: 第4句\nBob: 第5句"
        assert kwargs.get("content_text") == expected


async def test_marvin_manzai_uses_fallback_when_history_empty():
    """對話歷史為空且未給 topic，使用預設冷場台詞。"""
    from cogs.voice_controller import VoiceController
    vc = _make_vc()
    interaction = _make_interaction()
    
    vc.bot.engine.conv_buffer.history = []
    
    with patch("services.dialogue_generation.generate_dual_dialogue", AsyncMock(return_value=[])) as mock_gen, \
         patch("services.dialogue_generation.make_gemini_dual_dialogue_llm_fn", MagicMock()):
        await VoiceController.marvin_manzai.callback(vc, interaction, topic=None)
        
        mock_gen.assert_called_once()
        _, kwargs = mock_gen.call_args
        assert "冷場" in kwargs.get("content_text") or "安安靜靜" in kwargs.get("content_text")


async def test_marvin_manzai_playback_is_protected():
    """播放漫才時，_tts_protected 應暫時拉為 True，事後還原。"""
    from cogs.voice_controller import VoiceController
    vc = _make_vc()
    interaction = _make_interaction()
    
    segments = [{"voice": "marvin", "text": "x"}]
    
    seen = {}
    async def _capture(*a, **k):
        seen["protected"] = vc._tts_protected
        seen["interrupted"] = vc._tts_interrupted
        
    vc.play_dual_dialogue.side_effect = _capture
    
    with patch("services.dialogue_generation.generate_dual_dialogue", AsyncMock(return_value=segments)), \
         patch("services.dialogue_generation.make_gemini_dual_dialogue_llm_fn", MagicMock()):
        await VoiceController.marvin_manzai.callback(vc, interaction, topic="x")
        
    assert seen["protected"] is True
    assert seen["interrupted"] is False
    assert vc._tts_protected is False  # 播放完還原為 False
