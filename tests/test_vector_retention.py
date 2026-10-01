"""測試 VectorStore 90 天逐字稿清理與 daily_vector_retention_loop。"""
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
import memory_sandbox
from vector_store import VectorStore


def test_vector_store_prune_older_than(tmp_path):
    store = VectorStore(persist_dir=str(tmp_path))
    now = 1790800000.0  # 參考時間點
    old_ts = now - 100 * 86400  # 100 天前 (> 90 天)
    new_ts = now - 10 * 86400   # 10 天前 (< 90 天)

    # 1. 舊的
    old_id = f"Alice_1_{int(old_ts * 1000)}"
    store._col.add(ids=[old_id], documents=["舊逐字稿"])

    # 2. 新的
    new_id = f"Bob_1_{int(new_ts * 1000)}"
    store._col.add(ids=[new_id], documents=["新逐字稿"])

    # 3. 格式不合（無時間戳）
    invalid_id = "Charlie_1_notanumber"
    store._col.add(ids=[invalid_id], documents=["格式不合逐字稿"])

    assert store._col.count() == 3

    # --- A. Dry-run ---
    res_dry = store.prune_older_than(90, now=now, apply=False)
    assert res_dry["matched"] == 1
    assert res_dry["deleted"] == 0
    assert abs(res_dry["oldest_ts"] - old_ts) < 1.0
    assert store._col.count() == 3

    # --- B. Apply ---
    res_apply = store.prune_older_than(90, now=now, apply=True)
    assert res_apply["matched"] == 1
    assert res_apply["deleted"] == 1
    assert store._col.count() == 2

    # 驗證保留的是新的一筆與格式不合的一筆
    remaining_ids = set(store._col.get(include=[])["ids"])
    assert old_id not in remaining_ids
    assert new_id in remaining_ids
    assert invalid_id in remaining_ids


def test_vector_store_prune_sandbox_active(tmp_path, monkeypatch):
    store = VectorStore(persist_dir=str(tmp_path))
    now = 1790800000.0
    old_ts = now - 100 * 86400
    old_id = f"Alice_1_{int(old_ts * 1000)}"
    store._col.add(ids=[old_id], documents=["舊逐字稿"])

    monkeypatch.setattr(memory_sandbox, "active", lambda: True)

    res = store.prune_older_than(90, now=now, apply=True)
    assert res["deleted"] == 0
    assert store._col.count() == 1


@pytest.mark.asyncio
async def test_daily_vector_retention_loop_env_handling(monkeypatch):
    """驗證 daily_vector_retention_loop 正確讀取 MARVIN_CHROMA_RETENTION_APPLY。"""
    from cogs.voice_controller_system_loops import SystemLoopsMixin

    class DummyVC(SystemLoopsMixin):
        def __init__(self):
            self._vector_store = MagicMock()
            self._vector_store.prune_older_than.return_value = {
                "matched": 5, "deleted": 0, "oldest_ts": 12345.0
            }

    vc = DummyVC()

    # 1. 預設 env 未設 -> apply=False
    monkeypatch.delenv("MARVIN_CHROMA_RETENTION_APPLY", raising=False)
    # 取出 loop 底層 coroutine 函式
    coro = vc.daily_vector_retention_loop.coro(vc)
    await coro
    vc._vector_store.prune_older_than.assert_called_with(90, apply=False)

    # 2. env="1" -> apply=True
    monkeypatch.setenv("MARVIN_CHROMA_RETENTION_APPLY", "1")
    vc._vector_store.prune_older_than.reset_mock()
    coro = vc.daily_vector_retention_loop.coro(vc)
    await coro
    vc._vector_store.prune_older_than.assert_called_with(90, apply=True)
