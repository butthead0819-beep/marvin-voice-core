"""Tests for MemoryManager (SQLite backend)."""
import json
import os
import pytest
from suki_memory import MemoryManager, _new_player, _repair_player


@pytest.fixture
def mem(tmp_path):
    db = str(tmp_path / "test.db")
    jpath = str(tmp_path / "test_memory.json")
    return MemoryManager(db_path=db, json_compat_path=jpath)


# ── Basic CRUD ────────────────────────────────────────────────────────────────

def test_get_player_creates_new_record(mem):
    p = mem.get_player_memory("Alice")
    assert p["relationship_stage"] == "陌生人"
    assert isinstance(p["stats"], dict)


def test_get_player_persists_across_instances(tmp_path):
    db = str(tmp_path / "test.db")
    jpath = str(tmp_path / "mem.json")
    m1 = MemoryManager(db_path=db, json_compat_path=jpath)
    m1.get_player_memory("Bob")
    m1.set_player_impression("Bob", "愛炸魚")

    m2 = MemoryManager(db_path=db, json_compat_path=jpath)
    assert m2.get_player_memory("Bob")["suki_impression"] == "愛炸魚"


def test_increment_stat(mem):
    mem.increment_stat("Alice", "interaction_count", 3)
    mem.increment_stat("Alice", "interaction_count", 2)
    assert mem.get_player_memory("Alice")["stats"]["interaction_count"] == 5.0


def test_adjust_bias_clamped(mem):
    mem.adjust_bias("Alice", 9.0)
    mem.adjust_bias("Alice", 5.0)   # would exceed +10
    assert mem.get_player_memory("Alice")["bias_score"] == 10.0

    mem.adjust_bias("Alice", -25.0)  # would go below -10
    assert mem.get_player_memory("Alice")["bias_score"] == -10.0


# ── News queue ────────────────────────────────────────────────────────────────

def test_news_queue_lifo_and_max_3(mem):
    mem.enqueue_news("Alice", "news1")
    mem.enqueue_news("Alice", "news2")
    mem.enqueue_news("Alice", "news3")
    mem.enqueue_news("Alice", "news4")  # oldest should be evicted

    p = mem.get_player_memory("Alice")
    texts = [n["text"] for n in p["news_queue"]]
    assert "news1" not in texts
    assert texts == ["news2", "news3", "news4"]


def test_pop_news_returns_oldest(mem):
    mem.enqueue_news("Alice", "first")
    mem.enqueue_news("Alice", "second")
    assert mem.pop_news("Alice") == "first"
    assert mem.pop_news("Alice") == "second"
    assert mem.pop_news("Alice") is None


def test_pop_news_unknown_player_returns_none(mem):
    assert mem.pop_news("Ghost") is None


# ── update_player_memory ──────────────────────────────────────────────────────

def test_update_player_memory_merges_likes(mem):
    mem.update_player_memory("Alice", {"likes": ["music", "cats"]})
    mem.update_player_memory("Alice", {"likes": ["cats", "dogs"]})
    likes = set(mem.get_player_memory("Alice")["likes"])
    assert likes == {"music", "cats", "dogs"}


def test_update_player_memory_sets_personal_info(mem):
    mem.update_player_memory("Alice", {"personal_info": {"food": "拉麵", "housing": None}})
    pi = mem.get_player_memory("Alice")["personal_info"]
    assert pi["food"] == "拉麵"
    assert pi["housing"] is None  # None should not overwrite


# ── JSON compat export ────────────────────────────────────────────────────────

def test_json_compat_exported_after_save(tmp_path):
    db = str(tmp_path / "test.db")
    jpath = str(tmp_path / "mem.json")
    mem = MemoryManager(db_path=db, json_compat_path=jpath)
    mem.get_player_memory("ExportTest")
    assert os.path.exists(jpath)
    with open(jpath, encoding="utf-8") as f:
        data = json.load(f)
    assert "ExportTest" in data["players"]


# ── Migration from JSON ───────────────────────────────────────────────────────

def test_migrates_from_json_on_empty_db(tmp_path):
    jpath = str(tmp_path / "suki_memory.json")
    with open(jpath, "w", encoding="utf-8") as f:
        json.dump({"players": {"Migrated": _new_player()}}, f)

    db = str(tmp_path / "test.db")
    mem = MemoryManager(db_path=db, json_compat_path=jpath)
    assert "Migrated" in mem._cache


# ── Song history ──────────────────────────────────────────────────────────────

def test_song_history_deduplicates_and_keeps_latest(mem):
    for song in ["A", "B", "C", "A"]:
        mem.add_song_history("Alice", song)
    history = mem.get_song_history("Alice")
    assert history[-1] == "A"
    assert history.count("A") == 1


# ── flush is a no-op ──────────────────────────────────────────────────────────

def test_flush_is_noop(mem):
    mem.get_player_memory("Alice")
    mem.flush()   # should not raise


# ── _repair_player ────────────────────────────────────────────────────────────

def test_repair_player_fills_missing_keys():
    sparse = {"likes": ["coffee"]}
    repaired = _repair_player(sparse)
    assert "stats" in repaired
    assert "news_queue" in repaired
    assert repaired["likes"] == ["coffee"]


def test_repair_player_handles_non_dict():
    assert _repair_player("bad") == _new_player()
