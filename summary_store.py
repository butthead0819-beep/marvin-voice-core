from __future__ import annotations

import json
import sqlite3
import time


class SummaryStore:
    def __init__(self, db_path: str = "marvin.db"):
        self._db_path = db_path
        self._con: sqlite3.Connection | None = sqlite3.connect(":memory:") if db_path == ":memory:" else None
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        if self._con is not None:
            return self._con
        return sqlite3.connect(self._db_path)

    def _release(self, con: sqlite3.Connection) -> None:
        if self._con is None:
            con.close()

    def _init_db(self) -> None:
        con = self._connect()
        try:
            con.execute("""
                CREATE TABLE IF NOT EXISTS session_summaries (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id     INTEGER NOT NULL,
                    window_start REAL    NOT NULL,
                    window_end   REAL    NOT NULL,
                    summary_text TEXT    NOT NULL,
                    speakers     TEXT    NOT NULL DEFAULT '[]',
                    created_at   REAL    NOT NULL
                )
            """)
            con.execute("""
                CREATE INDEX IF NOT EXISTS idx_summaries_guild_window
                ON session_summaries (guild_id, window_end)
            """)
            con.commit()
        finally:
            self._release(con)

    def save_summary(
        self,
        guild_id: int,
        window_start: float,
        window_end: float,
        summary_text: str,
        speakers: list[str],
    ) -> int:
        con = self._connect()
        try:
            cur = con.execute(
                """INSERT INTO session_summaries
                   (guild_id, window_start, window_end, summary_text, speakers, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (guild_id, window_start, window_end, summary_text,
                 json.dumps(speakers, ensure_ascii=False), time.time()),
            )
            con.commit()
            return cur.lastrowid
        finally:
            self._release(con)

    def get_summaries(self, guild_id: int, hours: int = 24, limit: int = 60) -> list[dict]:
        cutoff = time.time() - hours * 3600
        con = self._connect()
        try:
            rows = con.execute(
                "SELECT id, guild_id, window_start, window_end, summary_text, speakers, created_at "
                "FROM session_summaries "
                "WHERE guild_id = ? AND window_end >= ? "
                "ORDER BY window_start ASC LIMIT ?",
                (guild_id, cutoff, limit),
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]
        finally:
            self._release(con)

    def search(self, guild_id: int, keyword: str, hours: int = 24) -> list[dict]:
        cutoff = time.time() - hours * 3600
        pattern = f"%{keyword}%"
        con = self._connect()
        try:
            rows = con.execute(
                "SELECT id, guild_id, window_start, window_end, summary_text, speakers, created_at "
                "FROM session_summaries "
                "WHERE guild_id = ? AND window_end >= ? AND summary_text LIKE ? "
                "ORDER BY window_start ASC",
                (guild_id, cutoff, pattern),
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]
        finally:
            self._release(con)

    def get_by_window(self, guild_id: int, window_start: float, window_end: float) -> list[dict]:
        con = self._connect()
        try:
            rows = con.execute(
                "SELECT id, guild_id, window_start, window_end, summary_text, speakers, created_at "
                "FROM session_summaries "
                "WHERE guild_id = ? AND window_start >= ? AND window_end <= ? "
                "ORDER BY window_start ASC",
                (guild_id, window_start, window_end),
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]
        finally:
            self._release(con)

    @staticmethod
    def _row_to_dict(row: tuple) -> dict:
        return {
            "id": row[0],
            "guild_id": row[1],
            "window_start": row[2],
            "window_end": row[3],
            "summary_text": row[4],
            "speakers": json.loads(row[5]),
            "created_at": row[6],
        }
