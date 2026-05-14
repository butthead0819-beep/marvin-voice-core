from __future__ import annotations

import sqlite3
import time


def init_table(con: sqlite3.Connection) -> None:
    con.execute("""
        CREATE TABLE IF NOT EXISTS game_memory (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            event_text TEXT NOT NULL,
            created_at REAL NOT NULL
        )
    """)


def write_event(con: sqlite3.Connection, event_text: str) -> None:
    con.execute(
        "INSERT INTO game_memory (event_text, created_at) VALUES (?, ?)",
        (event_text, time.time()),
    )


def read_recent(db_path: str, n: int = 10) -> list[str]:
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute(
            "SELECT event_text FROM game_memory ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()
        return [r[0] for r in reversed(rows)]
    except Exception:
        return []
    finally:
        con.close()


def get_context_block(db_path: str, n: int = 10) -> str:
    events = read_recent(db_path, n)
    if not events:
        return ""
    lines = "\n".join(f"- {e}" for e in events)
    return f"[🎮 最近遊戲記憶]\n{lines}"
