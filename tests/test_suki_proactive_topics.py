"""TDD: MemoryManager.get_proactive_topics() — 2026-05-26 修復 stub。

2026-05-18 commit 7b15dfe 重寫 MemoryManager 時，這個方法被 gut 成
`return []`，導致 suki_memory.json 內既有 proactive_topics 拿不到，
ProactiveTopicAgent / 舊 slow_system_loop 路徑都進死巷不出聲音 8 天。

修法：複用既有 get_meta(key) helper 讀 JSON top-level 欄位。
"""
from __future__ import annotations

import json

from suki_memory import MemoryManager


def _seed_json(path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False))


def test_returns_topics_from_json_compat_top_level(tmp_path):
    db = str(tmp_path / "x.db")
    j = tmp_path / "suki.json"
    _seed_json(j, {
        "players": {},
        "proactive_topics": [
            {"id": "t1", "title": "X", "script": "...", "target_players": ["A"]},
            {"id": "t2", "title": "Y", "script": "...", "target_players": []},
        ],
    })
    mm = MemoryManager(guild_id=0, db_path=db, json_compat_path=str(j))
    topics = mm.get_proactive_topics()
    assert len(topics) == 2
    assert topics[0]["id"] == "t1"
    assert topics[1]["id"] == "t2"


def test_returns_empty_when_field_missing(tmp_path):
    db = str(tmp_path / "x.db")
    j = tmp_path / "suki.json"
    _seed_json(j, {"players": {}})   # 沒 proactive_topics 欄位
    mm = MemoryManager(guild_id=0, db_path=db, json_compat_path=str(j))
    assert mm.get_proactive_topics() == []


def test_returns_empty_when_json_missing(tmp_path):
    """JSON 檔不存在 → 不炸，回 []（安全 fallback）。"""
    db = str(tmp_path / "x.db")
    j = tmp_path / "nope.json"     # 不存在
    mm = MemoryManager(guild_id=0, db_path=db, json_compat_path=str(j))
    assert mm.get_proactive_topics() == []


def test_returns_empty_when_field_is_wrong_type(tmp_path):
    """字段被髒資料寫成 dict / str → 回 []，caller 不會 TypeError。"""
    db = str(tmp_path / "x.db")
    j = tmp_path / "suki.json"
    _seed_json(j, {"proactive_topics": {"oops": "dict not list"}})
    mm = MemoryManager(guild_id=0, db_path=db, json_compat_path=str(j))
    assert mm.get_proactive_topics() == []
