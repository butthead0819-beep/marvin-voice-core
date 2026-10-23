"""TDD — 歌曲反應分析只在播放期間「至少 3 句人話」才打 LLM（省免費層額度）。

prompt 也改成逐行「名字：內容」格式，而非純文字拼接（原本 LLM 要憑純文字猜每位
成員的反應，容易掛錯人）。
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from cogs.music_cog_audio_meta import MusicAudioMetaMixin
from discord_voice_engine import ConversationBuffer


class _FakeCogWithConv(MusicAudioMetaMixin):
    def __init__(self, items: list[dict]):
        self.bot = MagicMock()
        self.bot.music_memory = MagicMock()
        self.bot.engine.conv_buffer.get_window_items = MagicMock(return_value=items)
        self.bot.router._call_llm = AsyncMock(return_value='{"reactions": {}}')


def _item(speaker: str, text: str, ts: float = 0.0) -> dict:
    return {"timestamp": ts, "speaker": speaker, "text": text}


@pytest.mark.asyncio
async def test_two_human_lines_skips_llm_call():
    """只有 2 句人話 → 不打 LLM。"""
    cog = _FakeCogWithConv([
        _item("阿明", "這首好好聽"),
        _item("小華", "對啊"),
    ])

    await cog._analyze_song_reactions({"title": "晴天"}, song_start_time=time.time(), lyrics="")

    cog.bot.router._call_llm.assert_not_called()


@pytest.mark.asyncio
async def test_three_human_lines_with_marmo_calls_llm_and_excludes_marmo():
    """3 句人話（另含 1 句 Marmo）→ 呼叫一次；prompt 含「名字：內容」格式，排除 Marmo。"""
    cog = _FakeCogWithConv([
        _item("阿明", "這首好好聽"),
        _item("小華", "對啊超讚"),
        _item("Marmo", "我也覺得不錯"),
        _item("大肚", "單曲循環吧"),
    ])

    await cog._analyze_song_reactions({"title": "晴天"}, song_start_time=time.time(), lyrics="")

    cog.bot.router._call_llm.assert_called_once()
    _, kwargs = cog.bot.router._call_llm.call_args
    user_prompt = kwargs["user_prompt"]
    assert "阿明：這首好好聽" in user_prompt
    assert "小華：對啊超讚" in user_prompt
    assert "大肚：單曲循環吧" in user_prompt
    assert "我也覺得不錯" not in user_prompt


def test_get_window_items_boundary_inclusive():
    """get_window_items(start, end)：剛好在 start/end 的條目要納入，之外要排除。"""
    buf = ConversationBuffer()
    buf.add_entry("A", "before", timestamp=100.0)
    buf.add_entry("B", "at_start", timestamp=110.0)
    buf.add_entry("C", "middle", timestamp=115.0)
    buf.add_entry("D", "at_end", timestamp=120.0)
    buf.add_entry("E", "after", timestamp=130.0)

    items = buf.get_window_items(110.0, 120.0)
    texts = [i["text"] for i in items]

    assert texts == ["at_start", "middle", "at_end"]
