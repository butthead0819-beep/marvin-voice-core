from __future__ import annotations

import sqlite3
import time

_VALID_STATUSES = {"pending", "done", "cancelled"}
_VALID_DIRECTIONS = {"inbound", "outbound"}

_SELECT_COLS = (
    "id, text, direction, assignee, speaker, status, due_date, source_quote, "
    "source_window_start, source_window_end, created_at"
)


class TaskStore:
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
                CREATE TABLE IF NOT EXISTS tasks (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id            INTEGER NOT NULL,
                    text                TEXT    NOT NULL,
                    direction           TEXT    NOT NULL,
                    assignee            TEXT    NOT NULL,
                    speaker             TEXT    NOT NULL DEFAULT '',
                    status              TEXT    NOT NULL DEFAULT 'pending',
                    due_date            REAL,
                    source_quote        TEXT    NOT NULL DEFAULT '',
                    source_window_start REAL    NOT NULL,
                    source_window_end   REAL    NOT NULL,
                    created_at          REAL    NOT NULL
                )
            """)
            # Migration: 舊版 schema 沒 speaker 欄位 → 補上（避免後續 index 建立失敗）
            cols = {row[1] for row in con.execute("PRAGMA table_info(tasks)")}
            if "speaker" not in cols:
                con.execute("ALTER TABLE tasks ADD COLUMN speaker TEXT NOT NULL DEFAULT ''")
            con.execute("""
                CREATE INDEX IF NOT EXISTS idx_tasks_guild_status
                ON tasks (guild_id, status)
            """)
            con.execute("""
                CREATE INDEX IF NOT EXISTS idx_tasks_guild_speaker
                ON tasks (guild_id, speaker)
            """)
            con.commit()
        finally:
            self._release(con)

    def save_task(
        self,
        guild_id: int,
        text: str,
        direction: str,
        assignee: str,
        source_quote: str,
        source_window_start: float,
        source_window_end: float,
        speaker: str = "",
        due_date: float | None = None,
    ) -> int:
        con = self._connect()
        try:
            cur = con.execute(
                """INSERT INTO tasks
                   (guild_id, text, direction, assignee, speaker, status, due_date,
                    source_quote, source_window_start, source_window_end, created_at)
                   VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)""",
                (guild_id, text, direction, assignee, speaker, due_date,
                 source_quote, source_window_start, source_window_end, time.time()),
            )
            con.commit()
            return cur.lastrowid
        finally:
            self._release(con)

    def get_pending(
        self,
        guild_id: int,
        direction: str | None = None,
        speaker: str | None = None,
    ) -> list[dict]:
        con = self._connect()
        try:
            conditions = ["guild_id = ?", "status = 'pending'"]
            params: list = [guild_id]
            if direction is not None:
                conditions.append("direction = ?")
                params.append(direction)
            if speaker is not None:
                conditions.append("speaker = ?")
                params.append(speaker)
            where = " AND ".join(conditions)
            rows = con.execute(
                f"SELECT {_SELECT_COLS} FROM tasks WHERE {where} ORDER BY created_at ASC",
                params,
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]
        finally:
            self._release(con)

    def update_text(self, task_id: int, new_text: str) -> None:
        con = self._connect()
        try:
            con.execute("UPDATE tasks SET text = ? WHERE id = ?", (new_text, task_id))
            con.commit()
        finally:
            self._release(con)

    def update_status(self, task_id: int, status: str) -> None:
        if status not in _VALID_STATUSES:
            raise ValueError(f"Invalid status: {status!r}. Must be one of {_VALID_STATUSES}")
        con = self._connect()
        try:
            con.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))
            con.commit()
        finally:
            self._release(con)

    def get_done(self, guild_id: int, speaker: str | None = None, hours: int = 24) -> list[dict]:
        cutoff = time.time() - hours * 3600
        con = self._connect()
        try:
            conditions = ["guild_id = ?", "status = 'done'", "created_at >= ?"]
            params: list = [guild_id, cutoff]
            if speaker is not None:
                conditions.append("speaker = ?")
                params.append(speaker)
            where = " AND ".join(conditions)
            rows = con.execute(
                f"SELECT {_SELECT_COLS} FROM tasks WHERE {where} ORDER BY created_at DESC",
                params,
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]
        finally:
            self._release(con)

    def get_overdue(self, guild_id: int) -> list[dict]:
        now = time.time()
        con = self._connect()
        try:
            rows = con.execute(
                f"SELECT {_SELECT_COLS} FROM tasks WHERE guild_id = ? AND status = 'pending' "
                "AND due_date IS NOT NULL AND due_date < ? ORDER BY due_date ASC",
                (guild_id, now),
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]
        finally:
            self._release(con)

    def search(self, guild_id: int, keyword: str, speaker: str | None = None) -> list[dict]:
        pattern = f"%{keyword}%"
        con = self._connect()
        try:
            if speaker is not None:
                rows = con.execute(
                    f"SELECT {_SELECT_COLS} FROM tasks "
                    "WHERE guild_id = ? AND speaker = ? AND (text LIKE ? OR source_quote LIKE ?) "
                    "ORDER BY created_at ASC",
                    (guild_id, speaker, pattern, pattern),
                ).fetchall()
            else:
                rows = con.execute(
                    f"SELECT {_SELECT_COLS} FROM tasks "
                    "WHERE guild_id = ? AND (text LIKE ? OR source_quote LIKE ?) "
                    "ORDER BY created_at ASC",
                    (guild_id, pattern, pattern),
                ).fetchall()
            return [self._row_to_dict(r) for r in rows]
        finally:
            self._release(con)

    def get_by_window(self, guild_id: int, window_start: float, window_end: float) -> list[dict]:
        con = self._connect()
        try:
            rows = con.execute(
                f"SELECT {_SELECT_COLS} FROM tasks WHERE guild_id = ? "
                "AND source_window_start >= ? AND source_window_end <= ? "
                "ORDER BY source_window_start ASC",
                (guild_id, window_start, window_end),
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]
        finally:
            self._release(con)

    @staticmethod
    def _row_to_dict(row: tuple) -> dict:
        return {
            "id": row[0],
            "text": row[1],
            "direction": row[2],
            "assignee": row[3],
            "speaker": row[4],
            "status": row[5],
            "due_date": row[6],
            "source_quote": row[7],
            "source_window_start": row[8],
            "source_window_end": row[9],
            "created_at": row[10],
        }
