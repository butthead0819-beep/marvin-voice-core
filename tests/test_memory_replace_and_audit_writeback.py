"""Bug 2b: MemoryManager.replace_player_memory + audit_player_memory writeback.

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
from unittest.mock import AsyncMock, MagicMock

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


# ── audit_player_memory: 清洗成功後正確回寫 ───────────────────────────────────

@pytest.fixture
def router_factory(tmp_path, monkeypatch):
    """Build a ContentRouter wired to a real MemoryManager + mocked LLM."""
    from gemini_router_content import GeminiRouterContentMixin

    class _Router(GeminiRouterContentMixin):
        def __init__(self):
            pass

    def _make(llm_response: str):
        mem = MemoryManager(
            db_path=str(tmp_path / "audit.db"),
            json_compat_path=str(tmp_path / "audit.json"),
        )
        mem.update_player_memory("Alice", {"likes": ["dirty"]})

        router = _Router()
        router.memory = mem
        router.prompt_manager = MagicMock()
        router.prompt_manager.get_instruction.return_value = "sys"
        router.vision_enabled = False
        router.dna = {}
        router.temp_toxicity_override = None
        router._call_llm = AsyncMock(return_value=llm_response)
        return router, mem

    return _make


@pytest.mark.asyncio
async def test_audit_writeback_replaces_cache_and_disk(router_factory):
    cleaned = {
        "personal_info": {"food": "拉麵"},
        "likes": ["clean"],
        "dislikes": [],
        "taboos": [],
        "suki_impression": "清洗後",
    }
    router, mem = router_factory(json.dumps(cleaned, ensure_ascii=False))

    await router.audit_player_memory("Alice")

    after = mem.get_player_memory("Alice")
    assert after["likes"] == ["clean"], (
        "audit 完成後應該整片覆寫；若仍包含舊的 'dirty' 表示回寫沒生效"
    )
    assert after["suki_impression"] == "清洗後"


@pytest.mark.asyncio
async def test_audit_rejects_malformed_llm_output(router_factory):
    # 缺 personal_info：應拒絕寫入
    bad = {"likes": ["clean"]}
    router, mem = router_factory(json.dumps(bad))

    await router.audit_player_memory("Alice")

    after = mem.get_player_memory("Alice")
    assert "dirty" in after["likes"], "格式異常時不應覆寫，舊資料必須保留"
