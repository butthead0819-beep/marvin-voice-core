"""ephemeral 記憶沙盒中央開關的行為測試。

沙盒 = satellite 唯讀繼承正本 + 寫入 no-op + 斷線丟棄，讓 satellite 進程能與
24/7 Discord bot 並存不搶寫正本（見 design_ephemeral_sandbox_memory）。
"""
import os
import sqlite3

import pytest

import memory_sandbox


@pytest.fixture(autouse=True)
def _clean_sandbox_state():
    """每個測試前後都清乾淨旗標與 env，避免污染其他測試。"""
    memory_sandbox.deactivate()
    os.environ.pop("MARVIN_MEMORY_SANDBOX", None)
    yield
    memory_sandbox.deactivate()
    os.environ.pop("MARVIN_MEMORY_SANDBOX", None)


def _make_db(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.execute("INSERT INTO t (v) VALUES ('seed')")
    conn.commit()
    conn.close()


def test_active_defaults_false():
    assert memory_sandbox.active() is False


def test_activate_deactivate_toggles():
    memory_sandbox.activate()
    assert memory_sandbox.active() is True
    memory_sandbox.deactivate()
    assert memory_sandbox.active() is False


def test_env_var_activates_sandbox():
    os.environ["MARVIN_MEMORY_SANDBOX"] = "1"
    assert memory_sandbox.active() is True


def test_connect_writable_when_inactive(tmp_path):
    db = str(tmp_path / "canon.db")
    _make_db(db)
    conn = memory_sandbox.connect(db)
    conn.execute("INSERT INTO t (v) VALUES ('live')")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 2
    conn.close()


def test_connect_readonly_when_active_reads_ok(tmp_path):
    db = str(tmp_path / "canon.db")
    _make_db(db)
    memory_sandbox.activate()
    conn = memory_sandbox.connect(db)
    # 讀＝繼承正本 OK
    assert conn.execute("SELECT v FROM t WHERE v='seed'").fetchone()[0] == "seed"
    conn.close()


def test_connect_readonly_when_active_blocks_write(tmp_path):
    db = str(tmp_path / "canon.db")
    _make_db(db)
    memory_sandbox.activate()
    conn = memory_sandbox.connect(db)
    # 寫＝撞物理牆（正本寫不進）
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO t (v) VALUES ('should_fail')")
        conn.commit()
    conn.close()


def test_memory_db_always_writable_even_in_sandbox():
    """:memory: 是純 RAM 測試/暫存 DB，沙盒下仍可寫（不是正本、無並行風險）。"""
    memory_sandbox.activate()
    conn = memory_sandbox.connect(":memory:")
    conn.execute("CREATE TABLE t (id INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1
    conn.close()
