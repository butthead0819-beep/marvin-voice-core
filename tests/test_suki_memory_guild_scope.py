"""Tests for guild-scoped MemoryManager (multi-guild isolation + migration + registry).

Phase 1 of the sequential multi-guild refactor. Design: one MemoryManager instance
per guild (registry-cached); `players` table keyed by (guild_id, username); legacy
single-guild rows migrate to the home guild (env GUILD_ID, fallback 0); JSON compat
export only runs for the home guild so offline scripts keep reading a flat players map.
"""
import json
import sqlite3
from suki_memory import MemoryManager


# ── Cross-guild isolation ─────────────────────────────────────────────────────

def test_same_username_isolated_across_guilds(tmp_path):
    """Same display name in two guilds must not share memory (the cross-guild bleed bug)."""
    db = str(tmp_path / "shared.db")
    a = MemoryManager(guild_id=111, db_path=db, json_compat_path=str(tmp_path / "a.json"))
    a.set_player_impression("Alice", "guild-A-Alice")

    b = MemoryManager(guild_id=222, db_path=db, json_compat_path=str(tmp_path / "b.json"))
    # Alice in guild 222 is a fresh record, not guild 111's Alice
    assert b.get_player_impression("Alice") == ""
    b.set_player_impression("Alice", "guild-B-Alice")

    # Reopen guild 111 → still its own value, untouched by guild 222
    a2 = MemoryManager(guild_id=111, db_path=db, json_compat_path=str(tmp_path / "a.json"))
    assert a2.get_player_impression("Alice") == "guild-A-Alice"


def test_news_queue_isolated_across_guilds(tmp_path):
    db = str(tmp_path / "shared.db")
    a = MemoryManager(guild_id=111, db_path=db, json_compat_path=str(tmp_path / "a.json"))
    b = MemoryManager(guild_id=222, db_path=db, json_compat_path=str(tmp_path / "b.json"))
    a.enqueue_news("Alice", "A-news")
    assert b.pop_news("Alice") is None  # guild 222 has no news for Alice


# ── Legacy migration ──────────────────────────────────────────────────────────

def test_legacy_rows_migrate_to_home_guild(tmp_path, monkeypatch):
    """Old single-guild DB (username PK, no guild_id) → rows assigned to home guild."""
    monkeypatch.setenv("GUILD_ID", "999")
    db = str(tmp_path / "legacy.db")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE players (username TEXT PRIMARY KEY, data TEXT NOT NULL DEFAULT '{}')")
    con.execute(
        "INSERT INTO players (username, data) VALUES (?, ?)",
        ("LegacyBob", json.dumps({"suki_impression": "old"}, ensure_ascii=False)),
    )
    con.commit()
    con.close()

    home = MemoryManager(guild_id=999, db_path=db, json_compat_path=str(tmp_path / "h.json"))
    assert home.get_player_impression("LegacyBob") == "old"

    other = MemoryManager(guild_id=111, db_path=db, json_compat_path=str(tmp_path / "o.json"))
    assert other.has_player("LegacyBob") is False


# ── Migration crash recovery (idempotent) ─────────────────────────────────────

def test_migration_recovers_orphaned_legacy_after_crash(tmp_path, monkeypatch):
    """Crash between CREATE and DROP: new-schema players (empty) + orphaned players_legacy.

    Next startup must drain legacy into players (INSERT OR IGNORE) and drop it — no data loss.
    """
    monkeypatch.setenv("GUILD_ID", "0")
    db = str(tmp_path / "crashed.db")
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE players (guild_id INTEGER NOT NULL, username TEXT NOT NULL, "
        "data TEXT NOT NULL DEFAULT '{}', PRIMARY KEY (guild_id, username))"
    )
    con.execute("CREATE TABLE players_legacy (username TEXT PRIMARY KEY, data TEXT NOT NULL DEFAULT '{}')")
    con.execute(
        "INSERT INTO players_legacy (username, data) VALUES (?, ?)",
        ("CrashBob", json.dumps({"suki_impression": "survived"}, ensure_ascii=False)),
    )
    con.commit()
    con.close()

    m = MemoryManager(guild_id=0, db_path=db, json_compat_path=str(tmp_path / "c.json"))
    assert m.get_player_impression("CrashBob") == "survived"
    con2 = sqlite3.connect(db)
    assert con2.execute("SELECT name FROM sqlite_master WHERE name='players_legacy'").fetchone() is None
    con2.close()


def test_migration_recovers_when_only_legacy_exists(tmp_path, monkeypatch):
    """Crash between RENAME and CREATE: only players_legacy survives, players gone."""
    monkeypatch.setenv("GUILD_ID", "0")
    db = str(tmp_path / "crashed2.db")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE players_legacy (username TEXT PRIMARY KEY, data TEXT NOT NULL DEFAULT '{}')")
    con.execute(
        "INSERT INTO players_legacy (username, data) VALUES (?, ?)",
        ("OrphanAlice", json.dumps({"suki_impression": "recovered"}, ensure_ascii=False)),
    )
    con.commit()
    con.close()

    m = MemoryManager(guild_id=0, db_path=db, json_compat_path=str(tmp_path / "c2.json"))
    assert m.get_player_impression("OrphanAlice") == "recovered"


# ── Registry ──────────────────────────────────────────────────────────────────

def test_registry_caches_one_instance_per_guild(tmp_path):
    db = str(tmp_path / "reg.db")
    j = str(tmp_path / "reg.json")
    MemoryManager.reset_registry()
    m1 = MemoryManager.for_guild(111, db_path=db, json_compat_path=j)
    m1b = MemoryManager.for_guild(111, db_path=db, json_compat_path=j)
    m2 = MemoryManager.for_guild(222, db_path=db, json_compat_path=j)
    assert m1 is m1b
    assert m1 is not m2
    assert m1._guild_id == 111
    assert m2._guild_id == 222


# ── Home-guild default (backward compat) ───────────────────────────────────────

def test_guild_id_defaults_to_home_env(tmp_path, monkeypatch):
    """Legacy construction without guild_id resolves to env GUILD_ID."""
    monkeypatch.setenv("GUILD_ID", "777")
    m = MemoryManager(db_path=str(tmp_path / "d.db"), json_compat_path=str(tmp_path / "d.json"))
    assert m._guild_id == 777


def test_guild_id_defaults_to_zero_without_env(tmp_path, monkeypatch):
    monkeypatch.delenv("GUILD_ID", raising=False)
    m = MemoryManager(db_path=str(tmp_path / "d.db"), json_compat_path=str(tmp_path / "d.json"))
    assert m._guild_id == 0


# ── JSON compat export gated to home guild ─────────────────────────────────────

def test_json_export_only_for_home_guild(tmp_path, monkeypatch):
    """Guest-guild managers must not clobber the home-guild suki_memory.json."""
    monkeypatch.setenv("GUILD_ID", "0")
    shared_json = str(tmp_path / "suki_memory.json")
    db = str(tmp_path / "shared.db")

    guest = MemoryManager(guild_id=555, db_path=db, json_compat_path=shared_json)
    guest.set_player_impression("Ghost", "guest")
    # guest guild != home (0) → no JSON written
    import os
    assert not os.path.exists(shared_json)

    home = MemoryManager(guild_id=0, db_path=db, json_compat_path=shared_json)
    home.set_player_impression("Owner", "home")
    assert os.path.exists(shared_json)
    with open(shared_json, encoding="utf-8") as f:
        data = json.load(f)
    assert "Owner" in data["players"]
    assert "Ghost" not in data["players"]  # guest never leaked into home JSON
