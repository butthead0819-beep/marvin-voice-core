"""TDD: DJ 口白角色改成「老朋友」＋素材分三槽（生活／品味／歌詞），歌詞從真實
歌詞挑一句（9/30 使用者定）。

不複製被測邏輯驗算自己——只呼叫真的 pick_chorus_line / _fetch_dj_interjection_raw
/ _fetch_song_meta / build_dj_interjection_prompt。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from dj_lyric_pick import pick_chorus_line
from tests.test_dj_story_context import _info, _make_cog, _only, _ctx_str


# ── pick_chorus_line（純函式） ──────────────────────────────────────────────

def test_pick_chorus_line_picks_most_repeated():
    lyrics = "第一句歌詞在這裡\n副歌就是這一句啦\n第二段不一樣的\n副歌就是這一句啦"
    assert pick_chorus_line(lyrics) == "副歌就是這一句啦"


def test_pick_chorus_line_no_repeat_returns_none():
    lyrics = "第一句歌詞在這裡\n第二段不一樣的\n第三段也不一樣"
    assert pick_chorus_line(lyrics) is None


def test_pick_chorus_line_none_or_empty_returns_none():
    assert pick_chorus_line(None) is None
    assert pick_chorus_line("") is None


def test_pick_chorus_line_excludes_credit_lines():
    lyrics = "作詞：某某某\n作詞：某某某\n只有一次的歌詞句"
    assert pick_chorus_line(lyrics) is None


def test_pick_chorus_line_excludes_too_short_lines():
    lyrics = "啦啦啦\n啦啦啦"
    assert pick_chorus_line(lyrics) is None


def test_pick_chorus_line_ties_take_earliest():
    lyrics = "甲乙丙丁戊己\n庚辛壬癸子丑\n甲乙丙丁戊己\n庚辛壬癸子丑"
    assert pick_chorus_line(lyrics) == "甲乙丙丁戊己"


# ── _fetch_dj_interjection_raw：歌詞槽 ──────────────────────────────────────

def _done_task(result):
    """建一個已完成、可被 asyncio.shield 包住的 awaitable。"""
    fut = asyncio.get_running_loop().create_future()
    fut.set_result(result)
    return fut


@pytest.mark.asyncio
async def test_lyric_slot_uses_real_lyrics_when_task_completes(tmp_path, monkeypatch):
    _only(monkeypatch, "atmosphere")
    cog = _make_cog(tmp_path=tmp_path)
    cog._life_cores = MagicMock(return_value=[])
    lyrics_text = "第一句歌詞在這裡\n副歌就是這一句啦\n第二段不一樣的\n副歌就是這一句啦"
    lyrics_task = _done_task(lyrics_text)

    await cog._fetch_dj_interjection_raw(_info(), lyrics_task=lyrics_task)
    ctx = _ctx_str(cog)
    assert "【你想跟他分享這首的原因】歌詞：『副歌就是這一句啦』" in ctx


@pytest.mark.asyncio
async def test_lyric_slot_skipped_and_task_not_cancelled_on_timeout(tmp_path, monkeypatch):
    import cogs.music_cog_dj_lyrics as dj_lyrics_mod
    monkeypatch.setattr(dj_lyrics_mod, "_DJ_LYRICS_WAIT_S", 0.01)
    _only(monkeypatch, "atmosphere")
    cog = _make_cog(tmp_path=tmp_path)
    cog._life_cores = MagicMock(return_value=[])
    lyrics_task = asyncio.get_running_loop().create_future()  # 永不完成

    result = await cog._fetch_dj_interjection_raw(_info(), lyrics_task=lyrics_task)
    assert isinstance(result, dict)
    ctx = _ctx_str(cog)
    assert "分享這首的原因" not in ctx
    assert lyrics_task.cancelled() is False


@pytest.mark.asyncio
async def test_taste_slot_labeled_when_play_count_high(tmp_path, monkeypatch):
    _only(monkeypatch, "atmosphere")
    cog = _make_cog(tmp_path=tmp_path)
    cog._life_cores = MagicMock(return_value=[])
    cog.bot.music_memory._data = {
        "songs": {
            "song_key_xyz": {
                "requesters": {"大肚": 5},
                "reactions": {},
            }
        }
    }
    await cog._fetch_dj_interjection_raw(_info(requester="大肚"))
    ctx = _ctx_str(cog)
    assert "【你懂他的音樂品味】" in ctx


@pytest.mark.asyncio
async def test_life_slot_labeled_for_conversation_mode(tmp_path, monkeypatch):
    _only(monkeypatch, "conversation")
    cog = _make_cog(tmp_path=tmp_path)
    cog._life_cores = MagicMock(return_value=[])
    cog.bot.engine.conv_buffer.get_last_n_utterances = MagicMock(
        return_value=[{"speaker": "狗與露", "text": "今天天氣真好"}]
    )
    await cog._fetch_dj_interjection_raw(_info(requester="大肚"))
    ctx = _ctx_str(cog)
    assert "【你熟悉他的生活】頻道近期對話" in ctx


@pytest.mark.asyncio
async def test_quick_mode_skips_all_three_slots(tmp_path, monkeypatch):
    import dj_narration_orchestrator
    spy = MagicMock(side_effect=dj_narration_orchestrator.pick_song_material)
    monkeypatch.setattr(dj_narration_orchestrator, "pick_song_material", spy)
    _only(monkeypatch, "quick")
    cog = _make_cog(tmp_path=tmp_path)
    cog._life_cores = MagicMock(return_value=[])

    result = await cog._fetch_dj_interjection_raw(_info())
    assert isinstance(result, dict)
    spy.assert_not_called()


# ── build_dj_interjection_prompt ────────────────────────────────────────────

def test_prompt_mentions_old_friend_and_lyric_slot():
    from dj_prompt_builder import build_dj_interjection_prompt
    prompt = build_dj_interjection_prompt("x")
    assert "老朋友" in prompt
    assert "【你想跟他分享這首的原因】" in prompt


# ── _fetch_song_meta：歌詞只抓一次、lyrics_task 傳給 DJ ─────────────────────

def _make_music_cog():
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.music_memory = MagicMock()
    bot.music_memory.all_songs = MagicMock(return_value={})

    from cogs.music_cog import MusicCog
    return MusicCog(bot)


@pytest.mark.asyncio
async def test_fetch_song_meta_fetches_lyrics_once_and_passes_task_to_dj():
    cog = _make_music_cog()
    cog._fetch_lyrics_raw = AsyncMock(return_value="歌詞內容")
    cog._fetch_comment_raw = AsyncMock(return_value=None)
    cog._fetch_lyrics_synced = AsyncMock(return_value=None)

    seen = {}

    async def _fake_dj(info, lyrics_task=None):
        seen["lyrics_task"] = lyrics_task
        return {"text": "DJ台詞", "audio_path": None}

    cog._fetch_dj_interjection_raw = _fake_dj

    info = {"title": "七里香", "url": "x"}
    await cog._fetch_song_meta(info)

    cog._fetch_lyrics_raw.assert_called_once()
    assert seen["lyrics_task"] is not None
