import sqlite3

import pytest

from game.player_score_db import add_scores, init_table


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "test.db")
    con = sqlite3.connect(path)
    init_table(con)
    con.commit()
    con.close()
    return path


def _score(db_path: str, user_id: str) -> int | None:
    con = sqlite3.connect(db_path)
    row = con.execute(
        "SELECT score FROM player_scores WHERE user_id = ?", (user_id,)
    ).fetchone()
    con.close()
    return row[0] if row else None


def test_add_scores_creates_new_player(db):
    con = sqlite3.connect(db)
    add_scores(con, [("u1", "Alice", 100)])
    con.commit()
    con.close()
    assert _score(db, "u1") == 100


def test_add_scores_accumulates(db):
    con = sqlite3.connect(db)
    add_scores(con, [("u1", "Alice", 100)])
    add_scores(con, [("u1", "Alice", 50)])
    con.commit()
    con.close()
    assert _score(db, "u1") == 150


def test_add_scores_cross_game_uses_same_column(db):
    con = sqlite3.connect(db)
    add_scores(con, [("u1", "Alice", 100)])
    add_scores(con, [("u1", "Alice", 80)])
    add_scores(con, [("u1", "Alice", 50)])
    con.commit()
    con.close()
    assert _score(db, "u1") == 230


def test_add_scores_skips_zero_delta(db):
    con = sqlite3.connect(db)
    add_scores(con, [("u1", "Alice", 0)])
    con.commit()
    con.close()
    assert _score(db, "u1") is None


def test_add_scores_allows_negative_delta(db):
    con = sqlite3.connect(db)
    add_scores(con, [("u1", "Alice", 100)])
    add_scores(con, [("u1", "Alice", -50)])
    con.commit()
    con.close()
    assert _score(db, "u1") == 50


def test_add_scores_updates_display_name(db):
    con = sqlite3.connect(db)
    add_scores(con, [("u1", "Alice", 100)])
    add_scores(con, [("u1", "Alice 2.0", 50)])
    con.commit()
    con.close()
    con2 = sqlite3.connect(db)
    name = con2.execute(
        "SELECT display_name FROM player_scores WHERE user_id='u1'"
    ).fetchone()[0]
    con2.close()
    assert name == "Alice 2.0"


def test_add_scores_multiple_players(db):
    con = sqlite3.connect(db)
    add_scores(con, [("u1", "Alice", 100), ("u2", "Bob", 50)])
    con.commit()
    con.close()
    assert _score(db, "u1") == 100
    assert _score(db, "u2") == 50
