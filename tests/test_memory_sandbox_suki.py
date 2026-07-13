"""沙盒下 MemoryManager（suki_memory）：唯讀繼承正本、寫入 no-op、cache 內連貫。

ephemeral 語意：session 內改動只留 self._cache（讀得回剛寫的），永不落盤，
斷線（進程結束）即忘；正本 marvin.db + suki_memory.json 一個 byte 沒被碰。
"""
import os
import sqlite3

import pytest

import memory_sandbox
from suki_memory import MemoryManager


@pytest.fixture(autouse=True)
def _clean(tmp_path):
    memory_sandbox.deactivate()
    MemoryManager.reset_registry()
    yield
    memory_sandbox.deactivate()
    MemoryManager.reset_registry()


def _mk(db, jsonp, guild_id=1):
    return MemoryManager(guild_id=guild_id, db_path=db, json_compat_path=jsonp)


def test_suki_sandbox_write_noop_but_cache_coherent(tmp_path):
    db = str(tmp_path / "marvin.db")
    jsonp = str(tmp_path / "suki.json")

    # 正本：寫一個玩家 + 一筆 stat
    seed = _mk(db, jsonp)
    seed.increment_stat("狗與露", "messages", 5)
    seed_val = seed.get_player_memory("狗與露")["stats"]["messages"]
    assert seed_val == 5

    def _rows():
        con = sqlite3.connect(db)
        try:
            return con.execute("SELECT data FROM players WHERE username='狗與露'").fetchone()
        finally:
            con.close()

    disk_before = _rows()

    # 沙盒：新 manager 讀正本
    memory_sandbox.activate()
    MemoryManager.reset_registry()
    sb = _mk(db, jsonp)
    # 繼承正本＝Marvin 認得你
    assert sb.get_player_memory("狗與露")["stats"]["messages"] == 5
    # 寫入：cache 內連貫（讀得回）
    sb.increment_stat("狗與露", "messages", 100)
    assert sb.get_player_memory("狗與露")["stats"]["messages"] == 105
    # 但正本磁碟零污染
    assert _rows() == disk_before


def test_suki_sandbox_new_player_no_disk_write(tmp_path):
    db = str(tmp_path / "marvin.db")
    jsonp = str(tmp_path / "suki.json")
    # 先建空正本 schema（sandbox off）
    _mk(db, jsonp)

    memory_sandbox.activate()
    MemoryManager.reset_registry()
    sb = _mk(db, jsonp)
    # 全新玩家：cache 建立、可讀，但不落盤
    p = sb.get_player_memory("新來的")
    assert p is not None
    con = sqlite3.connect(db)
    try:
        cnt = con.execute("SELECT COUNT(*) FROM players WHERE username='新來的'").fetchone()[0]
    finally:
        con.close()
    assert cnt == 0  # 沒寫進正本


def test_suki_sandbox_json_export_not_written(tmp_path):
    db = str(tmp_path / "marvin.db")
    jsonp = str(tmp_path / "suki.json")
    _mk(db, jsonp)
    # sandbox off 下 is_home 才 export；這裡 guild_id=1 未必 home，關鍵是沙盒下絕不寫
    if os.path.exists(jsonp):
        os.remove(jsonp)

    memory_sandbox.activate()
    MemoryManager.reset_registry()
    sb = _mk(db, jsonp)
    sb.increment_stat("狗與露", "messages", 1)
    sb._export_json()  # 直接呼叫也不能寫
    assert not os.path.exists(jsonp)
