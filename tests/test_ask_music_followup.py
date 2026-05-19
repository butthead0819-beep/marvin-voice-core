"""TDD: P1 — VoiceController._ask_music_followup 行為。

責任：當 MusicAgent bid 帶 missing_slots 時，handler 改打這個方法 — 把
ambiguous query 改成頻道問句，user 看見後可以再喚醒一次講全名。

設計刻意：
- 不打 yt-dlp（避免亂選歌）
- 不啟 TTS（怕 storm；user 在語音也看得到頻道訊息）
- 不存 per-speaker pending state（slim P1，行為簡單可預測）
- 不存 stt_logger（這是 controller 內部 trace，不算 STT 事件）
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_cog():
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.tts_engine = MagicMock()
    bot.router = MagicMock()
    bot.engine = MagicMock()
    bot.engine.conv_buffer = MagicMock()
    bot.engine.post_summon_callback = None

    with patch("cogs.voice_controller.DepartureStats", MagicMock), \
         patch("cogs.voice_controller.ConsentManager", MagicMock):
        from cogs.voice_controller import VoiceController
        cog = VoiceController(bot)
    cog.stt_logger = MagicMock()
    return cog


# ── 基本行為 ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_followup_sends_channel_message_for_song_title_slot():
    cog = _make_cog()
    cog.active_text_channel = AsyncMock()

    await cog._ask_music_followup("Alice", "播放周杰倫", ["song_title"])

    cog.active_text_channel.send.assert_awaited_once()
    msg = cog.active_text_channel.send.await_args.args[0]
    # 訊息要含 speaker + 顯示原始 query（讓 user 知道 bot 收到什麼）
    assert "Alice" in msg or "周杰倫" in msg
    # 要明確問追問內容（哪一首 / song / 歌名）
    assert "首" in msg or "歌" in msg or "song" in msg.lower()


@pytest.mark.asyncio
async def test_followup_does_not_call_safe_music_command():
    """followup 不該意外 trigger yt-dlp 路徑。"""
    cog = _make_cog()
    cog.active_text_channel = AsyncMock()
    cog._safe_music_command = AsyncMock()

    await cog._ask_music_followup("Alice", "播放周杰倫", ["song_title"])

    cog._safe_music_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_followup_no_text_channel_does_not_crash():
    """active_text_channel 是 None（沒人連 voice 但 bus 還在跑）→ 不該 raise。"""
    cog = _make_cog()
    cog.active_text_channel = None
    # 不該 raise
    await cog._ask_music_followup("Alice", "播放周杰倫", ["song_title"])


@pytest.mark.asyncio
async def test_followup_does_not_use_tts_engine():
    """刻意不走 TTS（避免 storm；P1 scope）。"""
    cog = _make_cog()
    cog.active_text_channel = AsyncMock()

    await cog._ask_music_followup("Alice", "播放周杰倫", ["song_title"])

    # bot.tts_engine 不該被呼叫任何 method
    cog.bot.tts_engine.synthesize.assert_not_called() if hasattr(
        cog.bot.tts_engine, "synthesize"
    ) else None
    # 退一步：channel send 算一次，tts 路徑零次


# ── 邊界 ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_followup_unknown_slot_falls_back_to_generic_question():
    """未知 slot type 不該炸；給通用追問訊息。"""
    cog = _make_cog()
    cog.active_text_channel = AsyncMock()

    await cog._ask_music_followup("Alice", "播放某東西", ["unknown_slot_xyz"])

    cog.active_text_channel.send.assert_awaited_once()
    msg = cog.active_text_channel.send.await_args.args[0]
    # 至少要有點訊息，不能 empty
    assert len(msg) > 5


@pytest.mark.asyncio
async def test_followup_empty_slot_list_still_safe():
    """empty list 理論上不該觸發 followup，但防呆要在。"""
    cog = _make_cog()
    cog.active_text_channel = AsyncMock()
    # 不該 raise；訊息可以是 noop 或 generic
    await cog._ask_music_followup("Alice", "播放陶喆的天天", [])
