from __future__ import annotations

import sqlite3
import time


class TranscriptStore:
    def __init__(self, db_path: str = "marvin.db"):
        self._db_path = db_path
        # :memory: 的連線不能關閉再重開（資料會消失），所以保留持久連線
        if db_path == ":memory:":
            self._con: sqlite3.Connection | None = sqlite3.connect(":memory:")
        else:
            self._con = None
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
                CREATE TABLE IF NOT EXISTS transcripts (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    speaker    TEXT    NOT NULL,
                    guild_id   INTEGER NOT NULL DEFAULT 0,
                    channel_id INTEGER NOT NULL DEFAULT 0,
                    text       TEXT    NOT NULL,
                    timestamp  REAL    NOT NULL
                )
            """)
            con.execute("""
                CREATE INDEX IF NOT EXISTS idx_transcripts_speaker_guild_ts
                ON transcripts (speaker, guild_id, timestamp)
            """)
            con.commit()
        finally:
            self._release(con)

    def save(self, speaker: str, guild_id: int, text: str, timestamp: float, channel_id: int = 0) -> None:
        if not text.strip():
            return
        con = self._connect()
        try:
            con.execute(
                "INSERT INTO transcripts (speaker, guild_id, channel_id, text, timestamp) VALUES (?, ?, ?, ?, ?)",
                (speaker, guild_id, channel_id, text, timestamp),
            )
            con.commit()
        finally:
            self._release(con)

    def get_recent(self, speaker: str, guild_id: int, days: int = 7) -> list[dict]:
        cutoff = time.time() - days * 86400
        con = self._connect()
        try:
            rows = con.execute(
                "SELECT speaker, text, timestamp FROM transcripts "
                "WHERE speaker = ? AND guild_id = ? AND timestamp >= ? "
                "ORDER BY timestamp ASC",
                (speaker, guild_id, cutoff),
            ).fetchall()
            return [{"speaker": r[0], "text": r[1], "timestamp": r[2]} for r in rows]
        finally:
            self._release(con)

    def get_speakers(self, guild_id: int) -> list[str]:
        con = self._connect()
        try:
            rows = con.execute(
                "SELECT DISTINCT speaker FROM transcripts WHERE guild_id = ?",
                (guild_id,),
            ).fetchall()
            return [r[0] for r in rows]
        finally:
            self._release(con)
