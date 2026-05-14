import sqlite3

import pytest

from game.game_memory_db import get_context_block, init_table, read_recent, write_event


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "test.db")
    con = sqlite3.connect(path)
    init_table(con)
    con.commit()
    con.close()
    return path


def test_write_and_read_single_event(db):
    con = sqlite3.connect(db)
    write_event(con, "【Busted】showay 250分")
    con.commit()
    con.close()
    events = read_recent(db)
    assert events == ["【Busted】showay 250分"]


def test_read_returns_chronological_order(db):
    con = sqlite3.connect(db)
    write_event(con, "第1場")
    write_event(con, "第2場")
    write_event(con, "第3場")
    con.commit()
    con.close()
    events = read_recent(db)
    assert events == ["第1場", "第2場", "第3場"]


def test_read_respects_limit(db):
    con = sqlite3.connect(db)
    for i in range(15):
        write_event(con, f"場次{i}")
    con.commit()
    con.close()
    events = read_recent(db, n=5)
    assert len(events) == 5
    assert events[-1] == "場次14"


def test_read_empty_returns_empty_list(db):
    assert read_recent(db) == []


def test_get_context_block_formats_correctly(db):
    con = sqlite3.connect(db)
    write_event(con, "【Busted99】答案 67，大肚 bust")
    write_event(con, "【謊言偵探】showay 150分")
    con.commit()
    con.close()
    block = get_context_block(db)
    assert block.startswith("[🎮 最近遊戲記憶]")
    assert "【Busted99】" in block
    assert "【謊言偵探】" in block


def test_get_context_block_empty_returns_empty_string(db):
    assert get_context_block(db) == ""
