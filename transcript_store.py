from __future__ import annotations

import sqlite3
import time

import memory_sandbox


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
        return memory_sandbox.connect(self._db_path)

    def _release(self, con: sqlite3.Connection) -> None:
        if self._con is None:
            con.close()

    def _init_db(self) -> None:
        if memory_sandbox.active():
            return  # 沙盒：正本 schema 已存在、唯讀連線不能建表
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
        if memory_sandbox.active():
            return  # 沙盒：寫入 no-op（ephemeral，斷線丟棄）
        con = self._connect()
        try:
            con.execute(
                "INSERT INTO transcripts (speaker, guild_id, channel_id, text, timestamp) VALUES (?, ?, ?, ?, ?)",
                (speaker, guild_id, channel_id, text, timestamp),
            )
            con.commit()
        finally:
            self._release(con)

    def get_recent(
        self,
        speaker: str | None = None,
        guild_id: int = 0,
        days: int = 7,
        minutes: int | None = None,
    ) -> list[dict]:
        if minutes is not None:
            cutoff = time.time() - minutes * 60
        else:
            cutoff = time.time() - days * 86400

        con = self._connect()
        try:
            if speaker is None:
                rows = con.execute(
                    "SELECT speaker, text, timestamp FROM transcripts "
                    "WHERE guild_id = ? AND timestamp >= ? "
                    "ORDER BY timestamp ASC",
                    (guild_id, cutoff),
                ).fetchall()
            else:
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

    def prune(self, retention_days: int = 14, now: float | None = None) -> int:
        """寬放 ZDR：刪除超過 retention_days 的原文，回傳刪除筆數。

        安全性：live bot 讀 raw transcript 的最長回看是 profile_compressor 的 7 天，
        retention_days=14 留足緩衝，prune 不影響任何即時行為。長期語意記憶由向量庫
        負責（不在此表）。邊界用嚴格小於，避免誤刪剛好落在 cutoff 的近期資料。
        """
        if memory_sandbox.active():
            return 0  # 沙盒：DELETE no-op（不動正本）
        cutoff = (now if now is not None else time.time()) - retention_days * 86400
        con = self._connect()
        try:
            cur = con.execute("DELETE FROM transcripts WHERE timestamp < ?", (cutoff,))
            con.commit()
            return cur.rowcount
        finally:
            self._release(con)
