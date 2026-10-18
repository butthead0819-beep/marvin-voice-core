"""端到端：日記氣氛行 → DJ 口白 context 內化 + 講破退模板。

路徑：_fetch_dj_interjection_raw → dj_mood_hint.latest_mood/build_mood_hint/
mood_hint_ctx_lines 注入 ctx → LLM 產出後 leaks_mood 檢查，講破就退模板（同
claims_request/_is_qualified_dj_script 那條不合格判斷鏈）。

2026-10-18 使用者定：氣氛只能內化成說話方式，不准講出來。
"""
from __future__ import annotations

import datetime as _dt
import tempfile
from unittest.mock import AsyncMock, MagicMock

import pytest

from dj_mood_hint import MOOD_HINT_HEADER


def _no_quick(monkeypatch):
    """排除 quick（本地模板、不呼叫 LLM），其餘照預設權重——只在乎有走 LLM 路徑的測試用。"""
    import dj_topic_selector
    w = dict(dj_topic_selector.MODE_WEIGHTS)
    w["quick"] = 0.0
    monkeypatch.setattr(dj_topic_selector, "MODE_WEIGHTS", w)


def _make_cog(online_members=None, life_entries=None, llm_text="DJ text"):
    """回傳 MusicCog，_load_summary_entries 注入 life_entries（避免讀真實檔案）。"""
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.tts_engine = MagicMock()
    bot.tts_engine.generate_audio = AsyncMock(return_value="/tmp/dj.opus")
    bot.tts_engine.get_estimated_duration = MagicMock(return_value=3.0)
    bot.router = MagicMock()
    bot.router.generate_dynamic_system_msg = AsyncMock(return_value=llm_text)
    bot.engine = MagicMock()
    bot.engine.conv_buffer = MagicMock()
    bot.engine.conv_buffer.get_last_n_utterances = MagicMock(return_value=[])
    bot.engine.post_summon_callback = None
    bot.music_memory = MagicMock()
    bot.music_memory._key = MagicMock(return_value="k")
    bot.music_memory._data = {"songs": {}}
    bot.music_memory.time_slot = MagicMock(return_value="深夜")

    from cogs.music_cog import MusicCog
    from dj_topic_selector import TopicCooldownStore
    cog = MusicCog(bot)
    cog._dj_topic_cooldown_store = TopicCooldownStore(tempfile.mktemp(suffix=".json"))

    fake_vc = MagicMock()
    fake_vc.get_online_members = MagicMock(return_value=online_members or [])
    cog._vc = MagicMock(return_value=fake_vc)

    cog._load_summary_entries = MagicMock(return_value=life_entries or [])

    return cog


def _mood_entry(mood, speakers, minutes_ago=0.0):
    from chat_summary_parser import DiaryEntry
    ts_str = (_dt.datetime.now() - _dt.timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M:%S")
    return DiaryEntry(ts_str=ts_str, core="聊排球賽", speakers=list(speakers), mood=mood)


def _info(requester="大肚"):
    return {"title": "夜曲", "uploader": "周杰倫", "requested_by": requester,
            "url": "https://example/x"}


# ── 1. 氣氛新鮮 → 注入 context ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fresh_mood_injected_into_context(monkeypatch):
    _no_quick(monkeypatch)
    entry = _mood_entry("熱絡｜showay 不爽隊友講不聽", ["showay"], minutes_ago=2)
    cog = _make_cog(online_members=["showay"], life_entries=[entry])

    await cog._fetch_dj_interjection_raw(_info())
    ctx = cog.bot.router.generate_dynamic_system_msg.call_args
    ctx_str = ctx.kwargs.get("context", "") if ctx else ""
    assert MOOD_HINT_HEADER in ctx_str
    assert "熱絡｜showay 不爽隊友講不聽" in ctx_str


# ── 2. 氣氛過期 → 不注入 ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stale_mood_not_injected(monkeypatch):
    _no_quick(monkeypatch)
    entry = _mood_entry("熱絡｜showay 不爽隊友講不聽", ["showay"], minutes_ago=40)
    cog = _make_cog(online_members=["showay"], life_entries=[entry])

    await cog._fetch_dj_interjection_raw(_info())
    ctx = cog.bot.router.generate_dynamic_system_msg.call_args
    ctx_str = ctx.kwargs.get("context", "") if ctx else ""
    assert MOOD_HINT_HEADER not in ctx_str


# ── 3. 講破 → 退模板 ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_leaked_mood_falls_back_to_template(monkeypatch):
    _no_quick(monkeypatch)
    entry = _mood_entry("熱絡｜showay 不爽隊友講不聽", ["showay"], minutes_ago=2)
    leak_text = "showay 遇到隊友講不聽很不爽，下一首給大家"
    cog = _make_cog(online_members=["showay"], life_entries=[entry], llm_text=leak_text)

    result = await cog._fetch_dj_interjection_raw(_info(requester="Marvin推薦"))
    assert result["text"] != leak_text


@pytest.mark.asyncio
async def test_unrelated_qualified_text_not_blocked(monkeypatch):
    """對照組：LLM 回不相干的合格句 → 不觸發講破檢查，照常使用。"""
    _no_quick(monkeypatch)
    entry = _mood_entry("熱絡｜showay 不爽隊友講不聽", ["showay"], minutes_ago=2)
    safe_text = "今天大家心情都很不錯，繼續聽下一首歌曲吧"
    cog = _make_cog(online_members=["showay"], life_entries=[entry], llm_text=safe_text)

    result = await cog._fetch_dj_interjection_raw(_info(requester="Marvin推薦"))
    assert result["text"] == safe_text


# ── 4. _load_summary_entries 丟例外 → fail-open ──────────────────────────────

@pytest.mark.asyncio
async def test_load_summary_entries_exception_fail_open(monkeypatch):
    _no_quick(monkeypatch)
    cog = _make_cog(online_members=["showay"])
    cog._load_summary_entries = MagicMock(side_effect=RuntimeError("boom"))

    result = await cog._fetch_dj_interjection_raw(_info())
    assert result is not None
    assert result["text"]
