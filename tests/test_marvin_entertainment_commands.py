"""Tests for Marvin's entertainment slash commands:
- /marvin_imitate
- /marvin_news
- /marvin_standup
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


pytestmark = pytest.mark.asyncio


def _make_vc():
    from cogs.voice_controller import VoiceController

    vc = VoiceController.__new__(VoiceController)
    vc.play_tts = AsyncMock()
    vc.play_dual_dialogue = AsyncMock()
    vc.get_online_members = MagicMock(return_value=["Alice", "Bob"])
    vc._tts_protected = False
    vc._tts_interrupted = True
    vc.bot = MagicMock()
    vc.bot.router = MagicMock()
    vc.bot.router.memory = MagicMock()
    vc.bot.router._call_llm = AsyncMock(return_value="mocked response")
    
    return vc


def _make_interaction(author_name="Alice"):
    interaction = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    interaction.user.display_name = author_name
    return interaction


async def test_marvin_imitate_happy_path():
    """當 speech_dna 存在時，應成功呼叫 LLM 並且進行播放。"""
    from cogs.voice_controller import VoiceController
    vc = _make_vc()
    interaction = _make_interaction()
    
    target_member = MagicMock()
    target_member.display_name = "Bob"
    
    mock_dna = {
        "style_summary": "講話很慢",
        "quirks": ["常嘆氣"],
        "fillers": ["那個"]
    }
    
    vc.bot.router.memory.get_speech_dna.return_value = mock_dna
    vc.bot.router._call_llm.return_value = "「那個... 唉。」... 呵，這就是你，無聊的人類。"
    
    await VoiceController.marvin_imitate.callback(vc, interaction, target=target_member)
    
    vc.bot.router.memory.get_speech_dna.assert_called_once_with("Bob")
    vc.bot.router._call_llm.assert_called_once()
    
    # 確保發送訊息並且播放模仿內容
    interaction.followup.send.assert_called_once()
    vc.play_tts.assert_called_once_with("「那個... 唉。」... 呵，這就是你，無聊的人類。", already_in_channel=True, protected=True)


async def test_marvin_imitate_fallback_empty_dna():
    """當 speech_dna 為空或缺少欄位時，應播放 fallback 提示音。"""
    from cogs.voice_controller import VoiceController
    vc = _make_vc()
    interaction = _make_interaction()
    
    target_member = MagicMock()
    target_member.display_name = "Charlie"
    
    # dna 為空
    vc.bot.router.memory.get_speech_dna.return_value = {}
    
    await VoiceController.marvin_imitate.callback(vc, interaction, target=target_member)
    
    vc.bot.router.memory.get_speech_dna.assert_called_once_with("Charlie")
    vc.bot.router._call_llm.assert_not_called()
    
    # 確保播放 fallback 語句
    expected_fallback = "我對 `Charlie` 這卑微的人類毫無頭緒。看來你對我不夠敞開心房，多跟我講點話讓我收集 DNA 吧。"
    interaction.followup.send.assert_called_once_with(f"👁️ {expected_fallback}")
    vc.play_tts.assert_called_once_with(expected_fallback, already_in_channel=True, protected=True)


async def test_marvin_news_happy_path():
    """當有個人新聞時，應呼叫雙口漫才生成與播放。"""
    from cogs.voice_controller import VoiceController
    vc = _make_vc()
    interaction = _make_interaction()
    
    # Mock pop_news 返回新聞
    vc.bot.router.memory.pop_news.side_effect = lambda name: "Bob 最近買了新車" if name == "Bob" else None
    vc.get_online_members.return_value = ["Alice", "Bob"]
    
    segments = [
        {"voice": "marvin", "text": "聽說 Bob 買了新玩具，真是虛無。"},
        {"voice": "marmo", "text": "馬文！人家是買實用的車！"}
    ]
    
    with patch("services.dialogue_generation.generate_dual_dialogue", AsyncMock(return_value=segments)) as mock_gen, \
         patch("services.dialogue_generation.make_gemini_dual_dialogue_llm_fn", MagicMock()):
        await VoiceController.marvin_news.callback(vc, interaction, target=None)
        
        mock_gen.assert_called_once()
        _, kwargs = mock_gen.call_args
        assert kwargs.get("content_text") == "Bob 最近買了新車"
        
        # 確保彈出了新聞
        assert vc.bot.router.memory.pop_news.called
        
        # 確保在 Discord 送出對白且呼叫 play_dual_dialogue
        assert interaction.followup.send.call_count >= 2
        vc.play_dual_dialogue.assert_called_once_with(segments, interject=True)


async def test_marvin_news_fallback_empty_news():
    """當沒有任何新聞時，應使用冷場 fallback 新聞播報。"""
    from cogs.voice_controller import VoiceController
    vc = _make_vc()
    interaction = _make_interaction()
    
    # 所有人無新聞
    vc.bot.router.memory.pop_news.return_value = None
    vc.get_online_members.return_value = ["Alice", "Bob"]
    
    segments = [
        {"voice": "marvin", "text": "今天世界依然很無聊。"},
        {"voice": "marmo", "text": "馬文，你天天都在無聊！"}
    ]
    
    with patch("services.dialogue_generation.generate_dual_dialogue", AsyncMock(return_value=segments)) as mock_gen, \
         patch("services.dialogue_generation.make_gemini_dual_dialogue_llm_fn", MagicMock()):
        await VoiceController.marvin_news.callback(vc, interaction, target=None)
        
        mock_gen.assert_called_once()
        _, kwargs = mock_gen.call_args
        assert "無趣" in kwargs.get("content_text") or "無謂的掙扎" in kwargs.get("content_text")
        
        vc.play_dual_dialogue.assert_called_once_with(segments, interject=True)


async def test_marvin_standup_happy_path():
    """馬文脫口秀應在有主題或隨機主題時正常呼叫 LLM 播放。"""
    from cogs.voice_controller import VoiceController
    vc = _make_vc()
    interaction = _make_interaction()
    
    vc.bot.router._call_llm.return_value = "人生就像一場無聊的遊戲..."
    
    await VoiceController.marvin_standup.callback(vc, interaction, topic="無聊人生")
    
    # 驗證 LLM 調用
    vc.bot.router._call_llm.assert_called_once()
    system_prompt_arg = vc.bot.router._call_llm.call_args[0][0]
    assert "無聊人生" in system_prompt_arg
    
    # 驗證 Discord 發送與 play_tts
    interaction.followup.send.assert_called_with("🎤 **馬文的個人脫口秀：無聊人生**\n「人生就像一場無聊的遊戲...」")
    vc.play_tts.assert_called_once_with("人生就像一場無聊的遊戲...", already_in_channel=True, protected=True)
