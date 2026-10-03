"""TDD: DJ atmosphere 的「現況」素材（dj_situation）。

純函式測試直接 import dj_situation 的真函式；整合測試走 _fetch_dj_interjection_raw。
"""
from __future__ import annotations

import datetime
import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

import dj_situation
import dj_topic_selector
from dj_situation import (
    format_duration_zh,
    is_working_hours,
    load_join_times,
    situation_facts,
)


def _ts(*args) -> float:
    return datetime.datetime(*args).timestamp()


def _write(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write((r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)) + "\n")


def _ev(name, event, ts):
    return {"user_name": name, "event": event, "ts": ts}


# ── is_working_hours ──

def test_is_working_hours_boundaries():
    assert is_working_hours(datetime.datetime(2026, 10, 5, 8, 0)) is True
    assert is_working_hours(datetime.datetime(2026, 10, 5, 7, 59)) is False
    assert is_working_hours(datetime.datetime(2026, 10, 9, 16, 59)) is True
    assert is_working_hours(datetime.datetime(2026, 10, 9, 17, 0)) is False
    assert is_working_hours(datetime.datetime(2026, 10, 10, 10, 0)) is False


# ── format_duration_zh ──

@pytest.mark.parametrize("seconds,expected", [
    (None, None),
    (1799, None),
    (1800, "30 分鐘"),
    (3599, "50 分鐘"),
    (3600, "1 小時"),
    (7199, "1 小時"),
    (86399, "23 小時"),
    (86400, "1 天"),
])
def test_format_duration_zh(seconds, expected):
    assert format_duration_zh(seconds) == expected


# ── load_join_times ──

def test_load_join_times_order_and_leave(tmp_path):
    p = tmp_path / "presence.jsonl"
    _write(p, [
        _ev("C", "join", 50),
        _ev("A", "join", 100),
        _ev("B", "join", 200),
        _ev("A", "leave", 300),
        _ev("A", "join", 400),
    ])
    assert load_join_times(str(p), since_ts=80) == {"A": 400.0, "B": 200.0}


def test_load_join_times_left_member_dropped(tmp_path):
    p = tmp_path / "presence.jsonl"
    _write(p, [_ev("D", "join", 150), _ev("B", "join", 200), _ev("D", "leave", 250)])
    assert load_join_times(str(p), since_ts=0) == {"B": 200.0}


def test_load_join_times_skips_bad_lines(tmp_path):
    p = tmp_path / "presence.jsonl"
    _write(p, [
        _ev("A", "join", 100),
        "{not json",
        {"user_name": "X", "event": "join"},
        "[1, 2]",
        _ev("B", "join", 200),
    ])
    assert load_join_times(str(p), since_ts=0) == {"A": 100.0, "B": 200.0}


def test_load_join_times_missing_file(tmp_path):
    assert load_join_times(str(tmp_path / "nope.jsonl"), since_ts=0) == {}


def test_load_join_times_default_path_read_at_call_time(tmp_path, monkeypatch):
    p = tmp_path / "presence.jsonl"
    _write(p, [_ev("A", "join", 100)])
    monkeypatch.setattr(dj_situation, "PRESENCE_LOG_PATH", str(p))
    assert load_join_times(since_ts=0) == {"A": 100.0}


def test_load_join_times_tail_truncation(tmp_path, monkeypatch):
    p = tmp_path / "presence.jsonl"
    rows = [
        _ev("OLD", "join", 1000),
        _ev("CUT", "join", 2000),
        _ev("LATE1", "join", 3000),
        _ev("LATE2", "join", 4000),
    ]
    _write(p, rows)
    sizes = [len((json.dumps(r, ensure_ascii=False) + "\n").encode("utf-8")) for r in rows]
    # 切點落在第二行（CUT）中間：尾巴 = 後兩行完整 + CUT 行的後半
    tail = sizes[2] + sizes[3] + sizes[1] // 2
    monkeypatch.setattr(dj_situation, "_TAIL_BYTES", tail)
    assert p.stat().st_size > tail
    # OLD 在檔尾之外沒被讀到；CUT 被切半丟掉
    assert load_join_times(str(p), since_ts=0) == {"LATE1": 3000.0, "LATE2": 4000.0}


# ── situation_facts ──

def test_situation_facts_full():
    now = _ts(2026, 10, 5, 10, 0)
    facts = situation_facts(
        now,
        on_air_since=now - 7200,
        join_times={"大肚": now - 3 * 3600, "阿明": now - 600, "不在場": now - 7200},
        present_members={"大肚", "阿明"},
    )
    assert facts == [
        "現在是平日上班時間，大家可能正邊上班邊聽",
        "Marvin 電台這次已經連續開台 2 小時",
        "大肚 這次已經在頻道待了 3 小時",
    ]


def test_situation_facts_empty():
    now = _ts(2026, 10, 10, 22, 0)
    assert situation_facts(now, on_air_since=None, join_times={}, present_members=None) == []


# ── 整合：_fetch_dj_interjection_raw ──

def _only(monkeypatch, *modes):
    monkeypatch.setattr(dj_topic_selector, "MODE_WEIGHTS", {m: 1.0 for m in modes})


def _make_cog(tmp_path=None):
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.tts_engine = MagicMock()
    bot.tts_engine.generate_audio = AsyncMock(return_value="/tmp/dj_audio.opus")
    bot.tts_engine.get_estimated_duration = MagicMock(return_value=3.0)
    bot.router = MagicMock()
    bot.router.generate_dynamic_system_msg = AsyncMock(return_value="這首接得剛好")
    bot.engine = MagicMock()
    bot.engine.conv_buffer = MagicMock()
    bot.engine.conv_buffer.get_last_n_utterances = MagicMock(return_value=[])
    bot.engine.post_summon_callback = None
    bot.music_memory = MagicMock()
    bot.music_memory._key = MagicMock(return_value="song_key_xyz")
    bot.music_memory._data = {"songs": {}}
    bot.music_memory.time_slot = MagicMock(return_value="深夜")

    from cogs.music_cog import MusicCog
    cog = MusicCog(bot)
    cog._enable_dj_news_fetch = False
    if tmp_path is not None:
        from dj_topic_selector import TopicCooldownStore
        cog._dj_topic_cooldown_store = TopicCooldownStore(path=str(tmp_path / "dj_topic_cooldown.json"))
    return cog


def _info():
    return {
        "title": "周杰倫 - 夜曲",
        "uploader": "周杰倫",
        "requested_by": "大肚",
        "url": "https://example/x",
    }


def _ctx_str(cog):
    call = cog.bot.router.generate_dynamic_system_msg.call_args
    return call.kwargs.get("context", "") or (call.args[1] if len(call.args) > 1 else "")


def _setup(monkeypatch, tmp_path):
    import location_state
    _only(monkeypatch, "atmosphere")
    monkeypatch.setattr(location_state, "load_location_state", lambda *a, **kw: None)
    monkeypatch.setattr(dj_situation, "is_working_hours", lambda dt: False)
    monkeypatch.setattr(dj_situation, "PRESENCE_LOG_PATH", str(tmp_path / "none.jsonl"))
    cog = _make_cog(tmp_path)
    cog._life_cores = MagicMock(return_value=[])
    return cog


@pytest.mark.asyncio
async def test_atmosphere_includes_situation_fact(monkeypatch, tmp_path):
    cog = _setup(monkeypatch, tmp_path)
    cog.bot.last_restart_time = time.time() - 7200
    await cog._fetch_dj_interjection_raw(_info())
    assert "現況：Marvin 電台這次已經連續開台 2 小時" in _ctx_str(cog)


@pytest.mark.asyncio
async def test_atmosphere_without_restart_time_has_no_situation(monkeypatch, tmp_path):
    cog = _setup(monkeypatch, tmp_path)
    await cog._fetch_dj_interjection_raw(_info())
    ctx = _ctx_str(cog)
    assert "現況：" not in ctx
    assert "台中" in ctx


@pytest.mark.asyncio
async def test_atmosphere_situation_failure_degrades(monkeypatch, tmp_path):
    cog = _setup(monkeypatch, tmp_path)
    cog.bot.last_restart_time = time.time() - 7200

    def _boom(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(dj_situation, "situation_facts", _boom)
    await cog._fetch_dj_interjection_raw(_info())
    assert cog.bot.router.generate_dynamic_system_msg.called
    assert "現況：" not in _ctx_str(cog)
