"""Bug 2b: MemoryManager.replace_player_memory（原為 audit_player_memory writeback；audit 已於 2026-09-18 移除）。

舊版 audit pipeline 透過 `memory.data["players"][u] = cleaned` + `memory._save_data()`
做整片覆寫；SQLite 重構後 .data 和 _save_data 都被刪了，整條記憶清洗 silently broken
（被 audit_player_memory 的 except Exception 吞掉）。

修法：
  - MemoryManager 加 replace_player_memory(username, data)：full-record overwrite + persist
  - gemini_router_content.audit_player_memory 改用該方法
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from suki_memory import MemoryManager


@pytest.fixture
def mem(tmp_path):
    return MemoryManager(
        db_path=str(tmp_path / "t.db"),
        json_compat_path=str(tmp_path / "t.json"),
    )


# ── replace_player_memory: 公開的整片覆寫 API ─────────────────────────────────

def test_replace_player_memory_overwrites_full_record(mem):
    mem.update_player_memory("Alice", {"likes": ["music"]})
    mem.set_player_impression("Alice", "舊印象")

    cleaned = {
        "personal_info": {"food": "拉麵"},
        "likes": ["cleaned-likes"],
        "dislikes": [],
        "taboos": [],
        "suki_impression": "新印象",
    }
    mem.replace_player_memory("Alice", cleaned)

    got = mem.get_player_memory("Alice")
    # 完全覆寫：舊 likes 不見了
    assert got["likes"] == ["cleaned-likes"]
    assert got["suki_impression"] == "新印象"
    assert got["personal_info"]["food"] == "拉麵"


def test_replace_player_memory_persists_to_sqlite(tmp_path):
    db = str(tmp_path / "t.db")
    jpath = str(tmp_path / "t.json")
    m1 = MemoryManager(db_path=db, json_compat_path=jpath)
    m1.replace_player_memory("Bob", {"personal_info": {}, "suki_impression": "已洗"})

    # 直接讀 SQLite 確認落盤（不依賴 cache）
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT data FROM players WHERE username = ?", ("Bob",)
        ).fetchone()
    assert row is not None
    assert json.loads(row[0])["suki_impression"] == "已洗"


def test_replace_player_memory_rejects_non_dict(mem):
    with pytest.raises((TypeError, ValueError)):
        mem.replace_player_memory("Alice", "not a dict")  # type: ignore[arg-type]
