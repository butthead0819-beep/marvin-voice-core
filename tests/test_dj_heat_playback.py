"""TDD: 播出前一刻依聊天熱度切換 DJ 口白（cogs/music_cog_tail_dj.py::_maybe_play_dj_interjection）。

2026-09-30 使用者定：熱聊時素材最多但沒人在聽 DJ，播出前一刻若判定熱聊 → 改唸
短版（只報歌名），沒有短版就這輪不講；不熱則照舊播原本預渲染的口白。
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_cog():
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.tts_engine = MagicMock()
    bot.tts_engine.generate_audio = AsyncMock(return_value="/tmp/short_dj.opus")
    bot.tts_engine.get_estimated_duration = MagicMock(return_value=3.0)
    bot.router = MagicMock()
    bot.router.generate_dynamic_system_msg = AsyncMock(return_value="唉...")
    bot.engine = MagicMock()
    bot.engine.conv_buffer = MagicMock()
    bot.engine.conv_buffer.get_last_n_utterances = MagicMock(return_value=[])
    bot.engine.conv_buffer.get_history = MagicMock(return_value=[])
    bot.engine.post_summon_callback = None
    bot.music_memory = MagicMock()
    bot.music_memory._key = MagicMock(return_value="key")
    bot.music_memory._data = {"songs": {}}
    bot.music_memory.time_slot = MagicMock(return_value="深夜")

    from cogs.music_cog import MusicCog
    cog = MusicCog(bot)
    return cog


def _make_vc(members):
    vc = MagicMock()
    vc.play_tts = AsyncMock()
    vc.play_local_file = AsyncMock()
    vc.play_dj_on_tts_layer = AsyncMock(return_value=True)
    vc._tts_protected = False
    vc._intimate_mode = False
    vc.get_online_members = MagicMock(return_value=list(members))
    return vc


def _hot_entries(now, n=5):
    return [
        {"timestamp": now - 3 * (i + 1), "speaker": ("大肚" if i % 2 == 0 else "狗與露"),
         "text": f"聊天{i}"}
        for i in range(n)
    ]


@pytest.mark.asyncio
async def test_hot_channel_speaks_short_text_via_fresh_tts(tmp_path):
    """熱聊 → generate_audio 用 short_text 現生成，play_dj_on_tts_layer 收到 short_text（非原口白）。"""
    cog = _make_cog()
    short_audio = tmp_path / "short_dj.opus"
    short_audio.write_bytes(b"x")
    cog.bot.tts_engine.generate_audio = AsyncMock(return_value=str(short_audio))
    now = time.time()
    cog.bot.engine.conv_buffer.get_history = MagicMock(return_value=_hot_entries(now))
    vc = _make_vc(["大肚", "狗與露"])
    cog._vc = MagicMock(return_value=vc)

    dj = {"text": "這是原本的長口白，講了很多故事", "audio_path": "/tmp/original_dj.opus",
          "short_text": "下一首，周杰倫的夜曲，大肚 點的"}
    await cog._maybe_play_dj_interjection(dj)

    cog.bot.tts_engine.generate_audio.assert_awaited_once_with(dj["short_text"], emotion="normal")
    vc.play_dj_on_tts_layer.assert_awaited_once()
    played_path, played_kwargs = vc.play_dj_on_tts_layer.call_args.args, vc.play_dj_on_tts_layer.call_args.kwargs
    assert played_path[0] == str(short_audio)
    assert played_kwargs.get("text") == dj["short_text"]


@pytest.mark.asyncio
async def test_not_hot_speaks_original_text_and_audio(tmp_path):
    """不熱 → 照舊播原本預渲染的 audio_path 與原口白文字。"""
    cog = _make_cog()
    vc = _make_vc(["大肚"])  # 只 1 人在線，不算熱
    cog._vc = MagicMock(return_value=vc)
    original = tmp_path / "original_dj.opus"
    original.write_bytes(b"x")

    dj = {"text": "這是原本的長口白", "audio_path": str(original),
          "short_text": "下一首，周杰倫的夜曲"}
    await cog._maybe_play_dj_interjection(dj)

    cog.bot.tts_engine.generate_audio.assert_not_awaited()
    vc.play_dj_on_tts_layer.assert_awaited_once_with(str(original), text=dj["text"])


@pytest.mark.asyncio
async def test_hot_without_short_text_plays_nothing():
    """熱聊但 dj meta 沒有 short_text（例如舊快取）→ 什麼都不播。"""
    cog = _make_cog()
    now = time.time()
    cog.bot.engine.conv_buffer.get_history = MagicMock(return_value=_hot_entries(now))
    vc = _make_vc(["大肚", "狗與露"])
    cog._vc = MagicMock(return_value=vc)

    dj = {"text": "這是原本的長口白", "audio_path": "/tmp/original_dj.opus", "short_text": ""}
    await cog._maybe_play_dj_interjection(dj)

    vc.play_dj_on_tts_layer.assert_not_awaited()
    vc.play_tts.assert_not_awaited()
    cog.bot.tts_engine.generate_audio.assert_not_awaited()


@pytest.mark.asyncio
async def test_hot_channel_snapshots_topic_bank():
    """熱聊時順便把話題存進話題庫，之後 take() 拿得到。"""
    cog = _make_cog()
    now = time.time()
    entries = _hot_entries(now)
    cog.bot.engine.conv_buffer.get_history = MagicMock(return_value=entries)
    vc = _make_vc(["大肚", "狗與露"])
    cog._vc = MagicMock(return_value=vc)

    dj = {"text": "原口白", "audio_path": "/tmp/original_dj.opus", "short_text": "下一首，夜曲"}
    await cog._maybe_play_dj_interjection(dj)

    lines = cog._dj_heat_bank().take(now)
    assert lines, "熱聊時應該把話題存進話題庫"
    assert any("聊天0" in line for line in lines)
