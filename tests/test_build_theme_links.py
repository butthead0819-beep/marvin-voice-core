"""Unit tests for scripts/build_theme_links.py — 主題連結 MVP 離線批次。

全部用 tmp_path 的 sqlite3 連線，不碰真的 records/ data/ marvin.db。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

import pytest

import scripts.build_theme_links as build_theme_links
from scripts.build_theme_links import (
    compute_heard_by,
    init_tables,
    load_canon_names,
    load_presence_events,
    run_card_phase,
    run_match_phase,
    run_theme_phase,
)


async def _noop_sleep(_s):
    return None


@dataclass
class FakeRouter:
    """記錄每次呼叫；responses 依 caller 給固定回應（或 callable 動態算）。"""
    responses: dict = field(default_factory=dict)
    calls: list = field(default_factory=list)

    async def analyze(self, prompt, *, caller, system=None, json=True, **kw):
        self.calls.append({"caller": caller, "prompt": prompt, "system": system})
        resp = self.responses.get(caller)
        if callable(resp):
            return resp(prompt)
        return resp

    async def quick(self, prompt, *, caller, system=None, json=True, **kw):
        return await self.analyze(prompt, caller=caller, system=system, json=json, **kw)


@pytest.fixture
def con(tmp_path):
    c = sqlite3.connect(str(tmp_path / "marvin.db"))
    init_tables(c)
    c.execute("""
        CREATE TABLE transcripts (
            speaker TEXT, guild_id INTEGER, channel_id INTEGER, text TEXT, timestamp REAL
        )
    """)
    c.commit()
    yield c
    c.close()


@dataclass
class _FakeEntry:
    ts_str: str
    meme_id: str = ""


# ── 歌卡 ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_card_phase_builds_card_from_llm(con):
    songs = {
        "k1": {"artist": "陳綺貞", "title": "旅行的意義", "total_plays": 5,
               "webpage_url": "https://www.youtube.com/watch?v=AAAAAAAAAAA"},
    }
    router = FakeRouter(responses={
        "theme_link_card": json.dumps({
            "imagery": "海邊、離別", "deep_story": "寫給遠行的伴侶",
            "relation": "伴侶", "weight": "中", "familiarity": "高",
        }),
    })

    built = await run_card_phase(con, router, songs, sleep_fn=_noop_sleep, now=1000.0)

    assert built == 1
    row = con.execute("SELECT video_id, familiarity, deep_story FROM song_cards").fetchone()
    assert row == ("AAAAAAAAAAA", "高", "寫給遠行的伴侶")


@pytest.mark.asyncio
async def test_run_card_phase_skips_existing_video_id(con):
    con.execute(
        "INSERT INTO song_cards (video_id, artist, title, imagery, deep_story, relation, weight, "
        "familiarity, created_at) VALUES ('AAAAAAAAAAA','a','b','','','','輕','低',0)"
    )
    con.commit()
    songs = {
        "k1": {"artist": "陳綺貞", "title": "旅行的意義", "total_plays": 5,
               "webpage_url": "https://www.youtube.com/watch?v=AAAAAAAAAAA"},
    }
    router = FakeRouter()
    built = await run_card_phase(con, router, songs, sleep_fn=_noop_sleep, now=1000.0)
    assert built == 0
    assert router.calls == []


# ── heard_by 組裝 ────────────────────────────────────────────────────────

def test_compute_heard_by_unions_across_windows():
    events = [
        {"ts": 100.0, "user_name": "showay", "event": "join", "is_bot": False},
        {"ts": 2000.0, "user_name": "showay", "event": "leave", "is_bot": False},
        {"ts": 5000.0, "user_name": "大肚", "event": "join", "is_bot": False},
        {"ts": 5100.0, "user_name": "大肚", "event": "leave", "is_bot": False},
    ]
    # 窗 1：900~1000（showay 在場）；窗 2：4900~5050（大肚在場）
    heard = compute_heard_by([1000.0, 5050.0], events)
    assert heard == ["showay", "大肚"]


# ── tag 釋義＋敏感＋房間把關 ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_theme_phase_creates_row_with_meaning_and_veto_null_when_too_few_lines(con):
    entries = [
        _FakeEntry("2026-10-20 10:00:00", "露營"),
        _FakeEntry("2026-10-21 10:00:00", "露營"),
    ]
    now = 1800000000.0  # 任意固定 epoch（entries 的 ts 遠早於它，不會被 14 天窗卡掉的話另算）
    # 讓 entries 落在 now 往前 14 天內：用 entries 的最大 ts 當 now 的錨點
    from theme_link import parse_tpe_timestamp
    now = parse_tpe_timestamp("2026-10-27 12:00:00")

    router = FakeRouter(responses={
        "theme_link_meaning": json.dumps({"meaning": "戶外過夜；延伸：遠離城市", "sensitive": False}),
    })

    updated = await run_theme_phase(con, router, entries, events=[], now=now, sleep_fn=_noop_sleep, guild_id=1)

    assert updated == 1
    row = con.execute("SELECT meaning, sensitive, room_veto, heard_by FROM theme_tags WHERE tag='露營'").fetchone()
    assert row[0] == "戶外過夜；延伸：遠離城市"
    assert row[1] == 0
    assert row[2] is None  # <5 行逐字稿 → 不判斷
    assert row[3] == "[]"  # events=[] → heard_by 空


@pytest.mark.asyncio
async def test_run_theme_phase_room_veto_true_when_enough_sensitive_lines(con):
    from theme_link import parse_tpe_timestamp
    last_seen = parse_tpe_timestamp("2026-10-21 10:00:00")
    entries = [
        _FakeEntry("2026-10-20 10:00:00", "搬家"),
        _FakeEntry("2026-10-21 10:00:00", "搬家"),
    ]
    now = parse_tpe_timestamp("2026-10-27 12:00:00")

    for i in range(6):
        con.execute(
            "INSERT INTO transcripts (speaker, guild_id, channel_id, text, timestamp) VALUES (?,1,0,?,?)",
            ("某人", f"第{i}句", last_seen - 60 * (i + 1)),
        )
    con.commit()

    router = FakeRouter(responses={
        "theme_link_meaning": json.dumps({"meaning": "離開舊住處", "sensitive": False}),
        "theme_link_veto": json.dumps({"touches_sensitive": True}),
    })

    await run_theme_phase(con, router, entries, events=[], now=now, sleep_fn=_noop_sleep, guild_id=1)

    row = con.execute("SELECT room_veto FROM theme_tags WHERE tag='搬家'").fetchone()
    assert row[0] == 1


@pytest.mark.asyncio
async def test_run_theme_phase_skips_recompute_when_last_seen_unchanged(con):
    from theme_link import parse_tpe_timestamp
    entries = [
        _FakeEntry("2026-10-20 10:00:00", "露營"),
        _FakeEntry("2026-10-21 10:00:00", "露營"),
    ]
    now = parse_tpe_timestamp("2026-10-27 12:00:00")
    router = FakeRouter(responses={
        "theme_link_meaning": json.dumps({"meaning": "戶外過夜", "sensitive": False}),
    })

    first = await run_theme_phase(con, router, entries, events=[], now=now, sleep_fn=_noop_sleep, guild_id=1)
    assert first == 1

    router.calls.clear()
    second = await run_theme_phase(con, router, entries, events=[], now=now, sleep_fn=_noop_sleep, guild_id=1)
    assert second == 0
    assert router.calls == []  # last_seen 沒變 → 不重算也不再打 LLM


# ── 配歌（粗篩）──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_match_phase_skips_llm_when_no_candidates(con):
    con.execute(
        "INSERT INTO theme_tags (tag, meaning, sensitive, room_veto, heard_by, first_seen, last_seen, "
        "day_count, updated_at) VALUES ('露營','戶外過夜',0,NULL,'[]',0,0,2,0)"
    )
    con.commit()
    router = FakeRouter()  # 若被呼叫，responses 查無 caller 回 None，但我們用 calls 斷言完全沒被呼叫

    matched = await run_match_phase(con, router, songs={}, sleep_fn=_noop_sleep)

    assert matched == 0
    assert router.calls == []


@pytest.mark.asyncio
async def test_run_match_phase_parses_picks_and_stores(con):
    con.execute(
        "INSERT INTO theme_tags (tag, meaning, sensitive, songable, room_veto, heard_by, first_seen, "
        "last_seen, day_count, updated_at) VALUES ('旅行','離開熟悉的地方',0,1,0,'[]',0,0,2,0)"
    )
    con.execute(
        "INSERT INTO song_cards (video_id, artist, title, imagery, deep_story, relation, weight, "
        "familiarity, created_at) VALUES ('AAAAAAAAAAA','陳綺貞','旅行的意義','海邊','離別故事','伴侶','中','高',0)"
    )
    con.commit()
    songs = {
        "k1": {"total_plays": 10, "webpage_url": "https://www.youtube.com/watch?v=AAAAAAAAAAA"},
    }
    router = FakeRouter(responses={
        "theme_link_match": json.dumps({"picks": [{"index": 0, "layer": "深層"}]}),
    })

    matched = await run_match_phase(con, router, songs, sleep_fn=_noop_sleep)

    assert matched == 1
    row = con.execute("SELECT tag, video_id, rank, layer FROM theme_picks").fetchone()
    assert row == ("旅行", "AAAAAAAAAAA", 0, "深層")


@pytest.mark.asyncio
async def test_run_match_phase_skips_tag_with_existing_picks(con):
    con.execute(
        "INSERT INTO theme_tags (tag, meaning, sensitive, room_veto, heard_by, first_seen, last_seen, "
        "day_count, updated_at) VALUES ('旅行','離開熟悉的地方',0,0,'[]',0,0,2,0)"
    )
    con.execute(
        "INSERT INTO song_cards (video_id, artist, title, imagery, deep_story, relation, weight, "
        "familiarity, created_at) VALUES ('AAAAAAAAAAA','陳綺貞','旅行的意義','海邊','離別','伴侶','中','高',0)"
    )
    con.execute("INSERT INTO theme_picks (tag, video_id, rank, layer) VALUES ('旅行','AAAAAAAAAAA',0,'深層')")
    con.commit()
    songs = {"k1": {"total_plays": 10, "webpage_url": "https://www.youtube.com/watch?v=AAAAAAAAAAA"}}
    router = FakeRouter()

    matched = await run_match_phase(con, router, songs, sleep_fn=_noop_sleep)

    assert matched == 0
    assert router.calls == []


# ── LLM 呼叫失敗（None/空字串）＝沒有結果，不寫入、下次重試（2026-10-27 首次實跑：限流的 None
#    被存成 familiarity=低 / sensitive=1 / room_veto=1 且永不重算）──────────────────────────

def _songs(n):
    return {f"k{i}": {"artist": "A", "title": f"歌{i}", "total_plays": 10 - i,
                      "webpage_url": f"https://www.youtube.com/watch?v=VID{i:08d}"} for i in range(n)}


@pytest.mark.asyncio
async def test_card_phase_llm_none_writes_nothing_and_stops_after_consecutive_failures(con):
    router = FakeRouter(responses={"theme_link_card": None})
    built = await run_card_phase(con, router, _songs(6), sleep_fn=_noop_sleep, now=1000.0)
    assert built == 0
    assert con.execute("SELECT COUNT(*) FROM song_cards").fetchone()[0] == 0
    assert len(router.calls) == 3  # MAX_CONSECUTIVE_LLM_FAILURES


@pytest.mark.asyncio
async def test_card_phase_empty_string_is_failure_not_low_familiarity(con):
    router = FakeRouter(responses={"theme_link_card": "  "})
    await run_card_phase(con, router, _songs(1), sleep_fn=_noop_sleep, now=1000.0)
    assert con.execute("SELECT COUNT(*) FROM song_cards").fetchone()[0] == 0


def _two_day_entries(tag="露營"):
    return [_FakeEntry("2026-10-20 10:00:00", tag), _FakeEntry("2026-10-21 10:00:00", tag)]


@pytest.mark.asyncio
async def test_theme_phase_meaning_none_writes_no_row(con):
    from theme_link import parse_tpe_timestamp
    now = parse_tpe_timestamp("2026-10-27 12:00:00")
    router = FakeRouter(responses={"theme_link_meaning": None})
    await run_theme_phase(con, router, _two_day_entries(), events=[], now=now,
                          sleep_fn=_noop_sleep, guild_id=1)
    assert con.execute("SELECT COUNT(*) FROM theme_tags").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_theme_phase_room_veto_none_writes_no_row(con):
    from theme_link import parse_tpe_timestamp
    now = parse_tpe_timestamp("2026-10-27 12:00:00")
    last = parse_tpe_timestamp("2026-10-21 10:00:00")
    for i in range(6):
        con.execute("INSERT INTO transcripts (speaker, guild_id, channel_id, text, timestamp) "
                    "VALUES ('showay',1,0,?,?)", (f"第{i}句", last - 60 * (i + 1)))
    con.commit()
    router = FakeRouter(responses={
        "theme_link_meaning": json.dumps({"meaning": "戶外過夜", "sensitive": False}),
        "theme_link_veto": None,
    })
    await run_theme_phase(con, router, _two_day_entries(), events=[], now=now,
                          sleep_fn=_noop_sleep, guild_id=1)
    assert con.execute("SELECT COUNT(*) FROM theme_tags").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_match_phase_none_writes_no_picks(con):
    con.execute("INSERT INTO theme_tags (tag, meaning, sensitive, songable, room_veto, heard_by, first_seen, "
                "last_seen, day_count, updated_at) VALUES ('露營','戶外過夜',0,1,NULL,'[]',0,0,2,0)")
    con.execute("INSERT INTO song_cards (video_id, artist, title, imagery, deep_story, relation, weight, "
                "familiarity, created_at) VALUES ('VID00000000','A','歌','山','露營','朋友','輕','高',0)")
    con.commit()
    router = FakeRouter(responses={"theme_link_match": None})
    matched = await run_match_phase(con, router, _songs(1), sleep_fn=_noop_sleep)
    assert matched == 0
    assert con.execute("SELECT COUNT(*) FROM theme_picks").fetchone()[0] == 0
    assert len(router.calls) == 1


# ── init_tables：舊表（沒有 songable 欄）補欄位 ─────────────────────────────

def test_init_tables_adds_songable_column_to_old_table(tmp_path):
    c = sqlite3.connect(str(tmp_path / "old.db"))
    c.execute("""
        CREATE TABLE theme_tags (
            tag TEXT PRIMARY KEY, meaning TEXT, sensitive INTEGER, room_veto INTEGER,
            heard_by TEXT, first_seen REAL, last_seen REAL, day_count INTEGER, updated_at REAL
        )
    """)
    c.commit()
    init_tables(c)
    cols = {r[1] for r in c.execute("PRAGMA table_info(theme_tags)")}
    assert "songable" in cols
    init_tables(c)  # 重複呼叫不報錯
    c.close()


# ── load_canon_names ─────────────────────────────────────────────────────

def test_load_canon_names_missing_file_returns_empty(tmp_path):
    assert load_canon_names(tmp_path / "nope.json") == {}


def test_load_canon_names_only_canon_prefix_and_nonempty(tmp_path):
    path = tmp_path / "song_knowledge.json"
    path.write_text(json.dumps({
        "canon::AAAAAAAAAAA": {"artist": "陳綺貞", "title": "旅行的意義"},
        "canon::BBBBBBBBBBB": {"artist": "", "title": "缺歌手"},
        "not_canon::CCCCCCCCCCC": {"artist": "x", "title": "y"},
    }), encoding="utf-8")
    result = load_canon_names(path)
    assert result == {"AAAAAAAAAAA": ("陳綺貞", "旅行的意義")}


def test_load_canon_names_malformed_json_returns_empty(tmp_path):
    path = tmp_path / "song_knowledge.json"
    path.write_text("這不是 JSON", encoding="utf-8")
    assert load_canon_names(path) == {}


# ── 歌卡：canon 命中時用正規化歌名歌手 ───────────────────────────────────────

@pytest.mark.asyncio
async def test_run_card_phase_uses_canon_name_when_hit(con):
    songs = {
        "k1": {"artist": "", "title": "Timeless Music - 陶喆 David Tao – 找自己", "total_plays": 5,
               "webpage_url": "https://www.youtube.com/watch?v=AAAAAAAAAAA"},
    }
    seen_prompts = {}

    def _capture(prompt):
        seen_prompts["prompt"] = prompt
        return json.dumps({"imagery": "城市", "deep_story": "找回自己", "relation": "自己",
                           "weight": "中", "familiarity": "高"})

    router = FakeRouter(responses={"theme_link_card": _capture})
    canon_names = {"AAAAAAAAAAA": ("陶喆", "找自己")}

    built = await run_card_phase(con, router, songs, sleep_fn=_noop_sleep, now=1000.0,
                                 canon_names=canon_names)

    assert built == 1
    assert "陶喆" in seen_prompts["prompt"] and "找自己" in seen_prompts["prompt"]
    row = con.execute("SELECT artist, title FROM song_cards").fetchone()
    assert row == ("陶喆", "找自己")


@pytest.mark.asyncio
async def test_run_card_phase_no_canon_hit_uses_existing_display_logic(con):
    songs = {
        "k1": {"artist": "陳綺貞", "title": "旅行的意義", "total_plays": 5,
               "webpage_url": "https://www.youtube.com/watch?v=AAAAAAAAAAA"},
    }
    router = FakeRouter(responses={
        "theme_link_card": json.dumps({"imagery": "海邊", "deep_story": "離別", "relation": "朋友",
                                       "weight": "中", "familiarity": "高"}),
    })

    built = await run_card_phase(con, router, songs, sleep_fn=_noop_sleep, now=1000.0, canon_names={})

    assert built == 1
    row = con.execute("SELECT artist, title FROM song_cards").fetchone()
    assert row == ("陳綺貞", "旅行的意義")


# ── 解析失敗（非 JSON）＝沒有結果，不寫入、連續 3 次停止 ─────────────────────

@pytest.mark.asyncio
async def test_card_phase_unparseable_text_writes_nothing_and_stops_after_3(con):
    router = FakeRouter(responses={"theme_link_card": "這不是 JSON"})
    built = await run_card_phase(con, router, _songs(6), sleep_fn=_noop_sleep, now=1000.0)
    assert built == 0
    assert con.execute("SELECT COUNT(*) FROM song_cards").fetchone()[0] == 0
    assert len(router.calls) == 3


@pytest.mark.asyncio
async def test_theme_phase_unparseable_meaning_writes_no_row(con):
    from theme_link import parse_tpe_timestamp
    now = parse_tpe_timestamp("2026-10-27 12:00:00")
    router = FakeRouter(responses={"theme_link_meaning": "這不是 JSON"})
    await run_theme_phase(con, router, _two_day_entries(), events=[], now=now,
                          sleep_fn=_noop_sleep, guild_id=1)
    assert con.execute("SELECT COUNT(*) FROM theme_tags").fetchone()[0] == 0


# ── 配歌：songable=0 不呼叫 LLM ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_match_phase_skips_llm_when_tag_not_songable(con):
    con.execute(
        "INSERT INTO theme_tags (tag, meaning, sensitive, songable, room_veto, heard_by, first_seen, "
        "last_seen, day_count, updated_at) VALUES ('裝備升級','遊戲機制',0,0,NULL,'[]',0,0,2,0)"
    )
    # 要有合格候選卡，否則「候選為空不呼叫 LLM」會讓這條測試不管有沒有 songable 過濾都過
    con.execute("INSERT INTO song_cards (video_id, artist, title, imagery, deep_story, relation, weight, "
                "familiarity, created_at) VALUES ('VID00000000','A','歌','山','露營','朋友','輕','高',0)")
    con.commit()
    router = FakeRouter(responses={"theme_link_match": '{"picks":[{"index":0,"layer":"意象"}]}'})

    matched = await run_match_phase(con, router, songs=_songs(1), sleep_fn=_noop_sleep)

    assert matched == 0
    assert router.calls == []


# ── load_presence_events：不帶參數時讀呼叫當下的 PRESENCE_LOG_PATH ──────────

def test_load_presence_events_no_arg_reads_current_module_path(tmp_path, monkeypatch):
    path = tmp_path / "voice_presence.jsonl"
    path.write_text('{"ts": 1.0, "user_name": "showay", "event": "join", "is_bot": false}\n',
                    encoding="utf-8")
    monkeypatch.setattr(build_theme_links, "PRESENCE_LOG_PATH", path)
    events = load_presence_events()
    assert events == [{"ts": 1.0, "user_name": "showay", "event": "join", "is_bot": False}]


def test_batch_router_quick_pool_uses_35_flash_lite_not_daily_20_flash():
    """批次走 quick tier：池子第一順位是 3.5-flash-lite，且不准有 gemini-3.5-flash（免費一天 20 次）。"""
    from llm_pool import build_tier_pools
    from scripts.build_theme_links import BATCH_MODEL_OVERRIDES

    quick, _analyze = build_tier_pools(
        env={"GOOGLE_API_KEY": "fake-key", **BATCH_MODEL_OVERRIDES},
        client_factory=lambda base_url, key: object(),
    )
    models = [ep.model for ep in quick.endpoints]
    assert models and models[0] == "gemini-3.5-flash-lite"
    assert "gemini-3.5-flash" not in models


@pytest.mark.asyncio
async def test_theme_phase_processes_most_recent_tag_first(con):
    """額度不夠跑完時，最近出現的 tag 要先處理（不是照字元排序）。"""
    from theme_link import parse_tpe_timestamp
    now = parse_tpe_timestamp("2026-10-27 12:00:00")
    entries = [
        _FakeEntry("2026-10-14 10:00:00", "bug"), _FakeEntry("2026-10-15 10:00:00", "bug"),
        _FakeEntry("2026-10-20 10:00:00", "露營"), _FakeEntry("2026-10-26 10:00:00", "露營"),
    ]
    router = FakeRouter(responses={
        "theme_link_meaning": json.dumps({"meaning": "m", "songable": True, "sensitive": False}),
    })
    await run_theme_phase(con, router, entries, events=[], now=now, sleep_fn=_noop_sleep, guild_id=1)
    meaning_calls = [c["prompt"] for c in router.calls if c["caller"] == "theme_link_meaning"]
    assert "露營" in meaning_calls[0]
    assert "bug" in meaning_calls[1]
