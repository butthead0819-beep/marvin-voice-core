"""TDD: meme_id 斷線 B 端到端。

路徑：
  ambient_diary prompt 新增 meme 標籤
  → DiaryEntry.meme_id 欄位
  → parser._extract_after_marker('meme')
  → dj_life_context.recent_life_cores 回 (core, meme_id) tuple（meme_id 非空時）
  → music_cog._life_cores 回 tuple list
  → select_topic 接收並用 meme_id 冷卻

fail-open 原則：log 沒有 meme 行 → meme_id="" → 回純字串（向後相容）。
"""
from __future__ import annotations

import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── 1. DiaryEntry 有 meme_id 欄位 ─────────────────────────────────────────────

def test_diary_entry_has_meme_id_field():
    from diary_comic.parser import DiaryEntry
    e = DiaryEntry(ts_str="2026-01-01 00:00:00", core="搬家")
    assert hasattr(e, "meme_id")
    assert e.meme_id == ""   # 預設空字串


# ── 2. parser 從 log 讀 meme 標籤 ─────────────────────────────────────────────

def test_parse_log_extracts_meme_id():
    """新格式 log 含 meme：標籤 → DiaryEntry.meme_id 被填入。"""
    from diary_comic.parser import parse_log
    log = (
        "[2026-07-29 20:00:00] --- 10分鐘對話總結 ---\n"
        "核心：大肚在準備搬家\n"
        "摘要：大肚說在打包\n"
        "顯著度：高\n"
        "meme：搬家\n"
    )
    entries = parse_log(log)
    assert len(entries) == 1
    assert entries[0].meme_id == "搬家"


def test_parse_log_meme_id_empty_when_absent():
    """舊格式 log 沒有 meme 行 → meme_id="" (fail-open)。"""
    from diary_comic.parser import parse_log
    log = (
        "[2026-07-29 20:00:00] --- 10分鐘對話總結 ---\n"
        "核心：大肚在準備搬家\n"
        "摘要：大肚說在打包\n"
        "顯著度：高\n"
    )
    entries = parse_log(log)
    assert len(entries) == 1
    assert entries[0].meme_id == ""


def test_parse_log_meme_id_bracket_format():
    """也吃舊式 【meme】 bracket 格式（_extract_after_marker 的能力）。"""
    from diary_comic.parser import parse_log
    log = (
        "[2026-07-29 20:00:00] --- 10分鐘對話總結 ---\n"
        "核心：今天宿醉很慘\n"
        "摘要：大肚昨晚喝太多\n"
        "顯著度：中\n"
        "【meme】：宿醉\n"
    )
    entries = parse_log(log)
    assert entries[0].meme_id == "宿醉"


# ── 3. recent_life_cores 回 tuple 當 meme_id 非空 ──────────────────────────────

def _ts_now() -> str:
    import datetime as _dt
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def test_recent_life_cores_returns_tuple_with_meme_id():
    """entry 有 meme_id → 回 (core, meme_id) tuple。"""
    from dj_life_context import recent_life_cores

    e = MagicMock()
    e.ts_str = _ts_now()
    e.core = "大肚在準備搬家"
    e.salience = "高"
    e.meme_id = "搬家"
    e.is_sensitive = False
    e.participants = None

    result = recent_life_cores([e], now=__import__("time").time())
    assert len(result) == 1
    item = result[0]
    # 有 meme_id → tuple
    assert isinstance(item, tuple), f"expected tuple, got {type(item)}: {item!r}"
    assert item[0].endswith("大肚在準備搬家")   # core（可能有【重點】前綴）
    assert item[1] == "搬家"


def test_recent_life_cores_returns_str_without_meme_id():
    """entry 沒有 meme_id（空字串）→ 回純 str（向後相容）。"""
    from dj_life_context import recent_life_cores

    e = MagicMock()
    e.ts_str = _ts_now()
    e.core = "今天天氣很好"
    e.salience = "中"
    e.meme_id = ""
    e.is_sensitive = False
    e.participants = None

    result = recent_life_cores([e], now=__import__("time").time())
    assert len(result) == 1
    assert isinstance(result[0], str)


# ── 4. music_cog._life_cores 回 tuple list（透過 recent_life_cores）────────────

def test_life_cores_passes_through_tuples():
    """_life_cores 回 LifeCore 列表（含 meme_id），供 select_mode 用。"""
    from cogs.music_cog import MusicCog
    from dj_life_context import LifeCore
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    cog = MusicCog(bot)

    e = MagicMock()
    e.ts_str = _ts_now()
    e.core = "大肚在搬家"
    e.salience = "中"
    e.meme_id = "搬家"
    e.is_sensitive = False
    e.participants = None

    result = cog._life_cores([e], __import__("time").time())
    assert len(result) == 1
    assert isinstance(result[0], LifeCore)
    assert result[0].meme_id == "搬家"


# ── 5. 端到端：meme_id 冷卻透過 _fetch_dj_interjection_raw ─────────────────────

@pytest.mark.asyncio
async def test_meme_id_cooldown_e2e_via_fetch_dj():
    """DJ 播報後，同 meme 的不同說法不再出現在 context。"""
    from cogs.music_cog import MusicCog
    from dj_topic_selector import TopicCooldownStore

    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.tts_engine = MagicMock()
    bot.tts_engine.generate_audio = AsyncMock(return_value="/tmp/dj.opus")
    bot.tts_engine.get_estimated_duration = MagicMock(return_value=3.0)
    bot.router = MagicMock()
    bot.router.generate_dynamic_system_msg = AsyncMock(return_value="DJ text")
    bot.engine = MagicMock()
    bot.engine.conv_buffer = MagicMock()
    bot.engine.conv_buffer.get_last_n_utterances = MagicMock(return_value=[])
    bot.engine.post_summon_callback = None
    bot.music_memory = MagicMock()
    bot.music_memory._key = MagicMock(return_value="k")
    bot.music_memory._data = {"songs": {}}
    bot.music_memory.time_slot = MagicMock(return_value="深夜")

    cog = MusicCog(bot)
    store = TopicCooldownStore(tempfile.mktemp(suffix=".json"))
    cog._dj_topic_cooldown_store = store

    fake_vc = MagicMock()
    fake_vc.get_online_members = MagicMock(return_value=["大肚"])
    cog._vc = MagicMock(return_value=fake_vc)

    # 第一首：life core 有 meme_id="搬家"，選中「大肚在準備搬家」
    cog._life_cores = MagicMock(return_value=[("大肚在準備搬家", "搬家")])
    await cog._fetch_dj_interjection_raw(
        {"title": "夜曲", "requested_by": "大肚", "url": "x"}
    )
    ctx1 = cog.bot.router.generate_dynamic_system_msg.call_args
    ctx1_str = ctx1.kwargs.get("context", "") if ctx1 else ""
    assert "大肚在準備搬家" in ctx1_str

    # 第二首：同 meme_id="搬家"，不同說法「大肚在打包」→ 應被冷卻跳過
    cog._life_cores = MagicMock(return_value=[("大肚在打包", "搬家")])
    cog.bot.router.generate_dynamic_system_msg.reset_mock()
    await cog._fetch_dj_interjection_raw(
        {"title": "稻香", "requested_by": "大肚", "url": "y"}
    )
    ctx2 = cog.bot.router.generate_dynamic_system_msg.call_args
    ctx2_str = ctx2.kwargs.get("context", "") if ctx2 else ""
    assert "大肚在打包" not in ctx2_str, "同 meme 第二說法應被 meme_id 冷卻跳過"
