"""Tests for three data-hygiene fixes found in production suki_memory data:

A1. Legacy numeric taste values (`"伍佰": 10.0`) must project into likes/dislikes.
A2. Pseudo-player names (autopilot self-attribution, system speaker) must never
    be persisted as real players.
A3. STT `__META__` leakage must not be stored in / loaded from emotional_highlights.
"""
import json
import sqlite3

import memory_sandbox
from suki_memory import (
    MemoryManager,
    _new_player,
    _repair_player,
    _normalize_numeric_taste,
    is_pseudo_player,
)


# ── A1: legacy numeric taste → likes/dislikes projection ──────────────────────

def test_normalize_numeric_taste_converts_plain_numbers():
    p = {"taste": {"伍佰": 10.0, "露營": 9}}
    _normalize_numeric_taste(p)
    assert p["taste"]["伍佰"] == {
        "score": 10.0, "mentions": 1, "first_seen": 0.0, "last_update": 0.0,
    }
    assert p["taste"]["露營"]["score"] == 9.0


def test_normalize_numeric_taste_clamps_out_of_range():
    p = {"taste": {"太愛了": 50.0, "太討厭了": -50}}
    _normalize_numeric_taste(p)
    assert p["taste"]["太愛了"]["score"] == 10.0
    assert p["taste"]["太討厭了"]["score"] == -10.0


def test_normalize_numeric_taste_skips_bool_and_str_and_dict():
    p = {"taste": {"flag": True, "note": "x", "already": {"score": 5.0, "mentions": 2}}}
    _normalize_numeric_taste(p)
    assert p["taste"]["flag"] is True
    assert p["taste"]["note"] == "x"
    assert p["taste"]["already"] == {"score": 5.0, "mentions": 2}


def test_repair_player_projects_legacy_numeric_taste_into_likes():
    repaired = _repair_player({"taste": {"伍佰": 10.0, "露營": 9, "討厭的東西": -5}})
    assert "伍佰" in repaired["likes"]
    assert "露營" in repaired["likes"]
    assert "討厭的東西" in repaired["dislikes"]
    assert repaired["taste"]["伍佰"]["score"] == 10.0
    assert repaired["taste"]["伍佰"]["last_update"] == 0.0


# ── A2: pseudo-player filtering ────────────────────────────────────────────────

def test_is_pseudo_player_true_cases():
    assert is_pseudo_player("Marvin推薦（為showay）") is True
    assert is_pseudo_player("Marvin推薦（點給大家）") is True
    assert is_pseudo_player("系統") is True
    assert is_pseudo_player("以「人類又想聽笑話」為主題的自嘲冷笑話") is True
    assert is_pseudo_player("") is True
    assert is_pseudo_player(None) is True


def test_is_pseudo_player_false_cases():
    assert is_pseudo_player("大肚") is False
    assert is_pseudo_player("馬文") is False
    assert is_pseudo_player("Marvin") is False


def test_get_player_memory_pseudo_player_not_persisted(tmp_path):
    db = str(tmp_path / "m.db")
    j = str(tmp_path / "m.json")
    m = MemoryManager(db_path=db, json_compat_path=j)

    p = m.get_player_memory("Marvin推薦（為X）")
    assert isinstance(p, dict)
    assert p["likes"] == []  # default fields present

    assert m.has_player("Marvin推薦（為X）") is False
    assert "Marvin推薦（為X）" not in m.list_players()


def test_pseudo_player_writes_do_not_leak_into_list_players(tmp_path):
    db = str(tmp_path / "m.db")
    j = str(tmp_path / "m.json")
    m = MemoryManager(db_path=db, json_compat_path=j)

    m.add_song_history("系統", "some song")
    m.add_emotional_highlight("系統", "some moment")

    assert "系統" not in m.list_players()
    assert m.has_player("系統") is False


def test_load_all_skips_and_deletes_pseudo_player_rows(tmp_path):
    db = str(tmp_path / "m.db")
    j = str(tmp_path / "m.json")

    # Pre-seed DB directly with one pseudo-player row and one real-player row,
    # bypassing MemoryManager (simulates historical bad data already on disk).
    seed = MemoryManager(db_path=db, json_compat_path=j)
    seed._conn.execute(
        "INSERT OR REPLACE INTO players (guild_id, username, data) VALUES (?, ?, ?)",
        (seed._guild_id, "Marvin推薦（為showay）", json.dumps(_new_player(), ensure_ascii=False)),
    )
    seed._conn.execute(
        "INSERT OR REPLACE INTO players (guild_id, username, data) VALUES (?, ?, ?)",
        (seed._guild_id, "大肚", json.dumps(_new_player(), ensure_ascii=False)),
    )
    seed._conn.commit()

    m = MemoryManager(db_path=db, json_compat_path=j)
    assert "大肚" in m.list_players()
    assert "Marvin推薦（為showay）" not in m.list_players()

    con = sqlite3.connect(db)
    count = con.execute(
        "SELECT COUNT(*) FROM players WHERE username = ?", ("Marvin推薦（為showay）",)
    ).fetchone()[0]
    con.close()
    assert count == 0


def test_migrate_from_json_skips_pseudo_players(tmp_path):
    db = str(tmp_path / "m.db")
    j = str(tmp_path / "m.json")
    with open(j, "w", encoding="utf-8") as f:
        json.dump({
            "players": {
                "系統": _new_player(),
                "大肚": _new_player(),
            }
        }, f, ensure_ascii=False)

    m = MemoryManager(db_path=db, json_compat_path=j)
    assert "大肚" in m.list_players()
    assert "系統" not in m.list_players()


def test_sandbox_active_does_not_delete_pseudo_player_row(tmp_path):
    """Sandbox is read-only against the real DB — must not attempt a DELETE."""
    db = str(tmp_path / "m.db")
    j = str(tmp_path / "m.json")

    seed = MemoryManager(db_path=db, json_compat_path=j)
    seed._conn.execute(
        "INSERT OR REPLACE INTO players (guild_id, username, data) VALUES (?, ?, ?)",
        (seed._guild_id, "系統", json.dumps(_new_player(), ensure_ascii=False)),
    )
    seed._conn.commit()

    memory_sandbox.activate()
    try:
        m = MemoryManager(db_path=db, json_compat_path=j)
        assert "系統" not in m.list_players()  # skipped in-memory
    finally:
        memory_sandbox.deactivate()

    con = sqlite3.connect(db)
    count = con.execute(
        "SELECT COUNT(*) FROM players WHERE username = ?", ("系統",)
    ).fetchone()[0]
    con.close()
    assert count == 1  # row untouched on disk


# ── A3: __META__ leakage in emotional_highlights ───────────────────────────────

def test_add_emotional_highlight_rejects_meta_leakage(tmp_path):
    db = str(tmp_path / "m.db")
    j = str(tmp_path / "m.json")
    m = MemoryManager(db_path=db, json_compat_path=j)

    m.add_emotional_highlight("大肚", '__META__ {"min_confidence": 0.5}')
    assert m.get_player_memory("大肚")["emotional_highlights"] == []

    m.add_emotional_highlight("大肚", "  __META__ leading whitespace")
    assert m.get_player_memory("大肚")["emotional_highlights"] == []

    m.add_emotional_highlight("大肚", "真的很開心")
    highlights = m.get_player_memory("大肚")["emotional_highlights"]
    assert len(highlights) == 1
    assert highlights[0]["moment"] == "真的很開心"


def test_repair_player_filters_meta_leakage_from_existing_highlights():
    p = {
        "emotional_highlights": [
            {"moment": "__META__ {\"min_confidence\":0.5}", "valence": "warm", "timestamp": 1.0},
            {"moment": "真的很開心", "valence": "warm", "timestamp": 2.0},
            "not-a-dict-entry",
        ]
    }
    repaired = _repair_player(p)
    moments = [h.get("moment") for h in repaired["emotional_highlights"] if isinstance(h, dict)]
    assert "真的很開心" in moments
    assert not any(str(m).lstrip().startswith("__META__") for m in moments)
    assert "not-a-dict-entry" in repaired["emotional_highlights"]
