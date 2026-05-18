"""Bug: 4 remaining `memory.data` dead refs after SQLite refactor.

These spots still pretend MemoryManager has the old `.data["players"]` /
`.data.get("marvin_performance", ...)` shape:

  - gemini_router_content.py:296,299 → `has_player(username)` + `get_player_memory`
  - gemini_router_content.py:519    → `get_meta("marvin_performance")` (top-level JSON key)
  - cogs/voice_controller.py:3316  → `list_players()`

Also: `_export_json()` overwrites `suki_memory.json` with only `{"players": ...}`,
nuking any top-level meta (marvin_performance) that the daily cron writes.

This test suite drives:
  * 3 new public APIs (list_players, has_player, get_meta)
  * Preservation behavior in _export_json
  * Static check that the 4 call sites no longer touch `.data`
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from suki_memory import MemoryManager


ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def mem(tmp_path):
    return MemoryManager(
        db_path=str(tmp_path / "t.db"),
        json_compat_path=str(tmp_path / "t.json"),
    )


# ── list_players ──────────────────────────────────────────────────────────────

def test_list_players_empty(mem):
    assert mem.list_players() == []


def test_list_players_returns_cached_usernames(mem):
    mem.get_player_memory("Alice")
    mem.get_player_memory("Bob")
    assert set(mem.list_players()) == {"Alice", "Bob"}


# ── has_player ────────────────────────────────────────────────────────────────

def test_has_player_false_for_unknown(mem):
    assert mem.has_player("Ghost") is False
    # 關鍵不變式：has_player 不能 silently 建立新紀錄
    assert "Ghost" not in mem.list_players()


def test_has_player_true_after_creation(mem):
    mem.get_player_memory("Alice")
    assert mem.has_player("Alice") is True


# ── get_meta（讀取 JSON 頂層非 players 的 key） ────────────────────────────────

def test_get_meta_returns_default_when_json_missing(tmp_path):
    m = MemoryManager(
        db_path=str(tmp_path / "t.db"),
        json_compat_path=str(tmp_path / "nope.json"),
    )
    assert m.get_meta("marvin_performance") is None
    assert m.get_meta("marvin_performance", default={}) == {}


def test_get_meta_reads_top_level_key_from_json(tmp_path):
    jpath = tmp_path / "t.json"
    jpath.write_text(json.dumps({
        "players": {},
        "marvin_performance": {"optimal_response_length": 42},
    }), encoding="utf-8")

    m = MemoryManager(
        db_path=str(tmp_path / "t.db"),
        json_compat_path=str(jpath),
    )
    mp = m.get_meta("marvin_performance")
    assert mp == {"optimal_response_length": 42}


def test_get_meta_returns_default_for_corrupt_json(tmp_path):
    jpath = tmp_path / "t.json"
    jpath.write_text("not valid json", encoding="utf-8")

    m = MemoryManager(
        db_path=str(tmp_path / "t.db"),
        json_compat_path=str(jpath),
    )
    assert m.get_meta("anything", default="fallback") == "fallback"


# ── _export_json 保留 top-level meta ──────────────────────────────────────────

def test_export_json_preserves_existing_meta(tmp_path):
    jpath = tmp_path / "t.json"
    # 模擬 daily cron 已經寫了 marvin_performance
    jpath.write_text(json.dumps({
        "players": {"Alice": {"likes": ["old"]}},
        "marvin_performance": {"optimal_response_length": 42, "score": 88},
        "proactive_topics": ["foo"],
    }), encoding="utf-8")

    m = MemoryManager(db_path=str(tmp_path / "t.db"), json_compat_path=str(jpath))
    # 觸發一次 player save，會呼叫 _export_json
    m.set_player_impression("Alice", "新印象")

    written = json.loads(jpath.read_text(encoding="utf-8"))
    assert written["marvin_performance"] == {"optimal_response_length": 42, "score": 88}, (
        "_export_json 不應 nuke daily cron 寫入的頂層 marvin_performance"
    )
    assert written["proactive_topics"] == ["foo"]
    # players 區段必須是最新的 cache
    assert written["players"]["Alice"]["suki_impression"] == "新印象"


# ── Static guard: no more memory.data references in production code ──────────

@pytest.mark.parametrize("rel_path", [
    "cogs/voice_controller.py",
    "gemini_router_content.py",
])
def test_no_memory_data_attribute_access(rel_path):
    src = (ROOT / rel_path).read_text(encoding="utf-8")
    # 允許 hasattr(..., "data") 因為那是 defensive check，不會 raise
    bad_patterns = [".memory.data[", ".memory.data.get("]
    for pat in bad_patterns:
        assert pat not in src, (
            f"{rel_path} 仍含 `{pat}` — MemoryManager 重構後 .data 已不存在，"
            f"執行到該行會噴 AttributeError。改用 list_players() / has_player() / get_meta()。"
        )
