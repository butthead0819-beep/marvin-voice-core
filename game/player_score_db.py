from __future__ import annotations

import sqlite3
import time


def init_table(con: sqlite3.Connection) -> None:
    con.execute("""
        CREATE TABLE IF NOT EXISTS player_scores (
            user_id      TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            score        INTEGER NOT NULL DEFAULT 0,
            last_updated REAL    NOT NULL DEFAULT 0
        )
    """)


def add_scores(
    con: sqlite3.Connection,
    players: list[tuple[str, str, int]],  # (user_id, display_name, delta)
) -> None:
    now = time.time()
    for user_id, display_name, delta in players:
        if delta == 0:
            continue
        con.execute(
            """
            INSERT INTO player_scores (user_id, display_name, score, last_updated)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                display_name = excluded.display_name,
                score        = score + excluded.score,
                last_updated = excluded.last_updated
            """,
            (user_id, display_name, delta, now),
        )
