"""
Living Profile 壓縮器

將使用者最近 7 天的語音逐字稿壓縮成結構化的 Living Profile，
存入 SQLite user_profiles table，支援 stale 檢查與增量更新。
"""

from __future__ import annotations

import logging
import sqlite3
import time

from groq import AsyncGroq

from transcript_store import TranscriptStore

logger = logging.getLogger(__name__)

_COMPRESS_PROMPT_TEMPLATE = """\
你是一個記憶壓縮器。以下是 {speaker} 在語音頻道說過的話（最近7天）：

{transcripts}

請用 200 字以內的繁體中文，摘要這個人的：
1. 正在進行的事或計畫
2. 常提到的主題或關心的事
3. 說話習慣或個性特徵
4. 提到的重要人物或關係

只輸出摘要本身，不要加任何標題或前綴。"""

_MIN_TRANSCRIPT_COUNT = 5


class ProfileCompressor:
    def __init__(self, db_path: str = "marvin.db", transcript_store: TranscriptStore | None = None):
        self._db_path = db_path
        # :memory: 保留持久連線（與 TranscriptStore 相同模式）
        if db_path == ":memory:":
            self._con: sqlite3.Connection | None = sqlite3.connect(":memory:")
        else:
            self._con = None

        self._store = transcript_store or TranscriptStore(db_path=db_path)
        self._init_db()

    # ── 內部連線管理 ──────────────────────────────────────────────────────────

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
                CREATE TABLE IF NOT EXISTS user_profiles (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    speaker      TEXT NOT NULL,
                    guild_id     INTEGER NOT NULL DEFAULT 0,
                    profile_text TEXT NOT NULL,
                    updated_at   REAL NOT NULL
                )
            """)
            con.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_user_profiles_speaker_guild
                    ON user_profiles(speaker, guild_id)
            """)
            con.commit()
        finally:
            self._release(con)

    # ── 內部寫入（測試可直接呼叫，不需 mock LLM）────────────────────────────

    def _upsert_profile(self, speaker: str, guild_id: int, profile_text: str, updated_at: float) -> None:
        """INSERT OR REPLACE 最新 profile（允許測試直接注入）"""
        con = self._connect()
        try:
            con.execute(
                """
                INSERT INTO user_profiles (speaker, guild_id, profile_text, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(speaker, guild_id) DO UPDATE SET
                    profile_text = excluded.profile_text,
                    updated_at   = excluded.updated_at
                """,
                (speaker, guild_id, profile_text, updated_at),
            )
            con.commit()
        finally:
            self._release(con)

    # ── 公開介面 ──────────────────────────────────────────────────────────────

    def get_profile(self, speaker: str, guild_id: int) -> str | None:
        """回傳最新的 profile 文字，沒有則 None"""
        con = self._connect()
        try:
            row = con.execute(
                "SELECT profile_text FROM user_profiles WHERE speaker = ? AND guild_id = ?",
                (speaker, guild_id),
            ).fetchone()
            return row[0] if row else None
        finally:
            self._release(con)

    def is_stale(self, speaker: str, guild_id: int, max_age_hours: int = 24) -> bool:
        """profile 超過 max_age_hours 或不存在 → True"""
        con = self._connect()
        try:
            row = con.execute(
                "SELECT updated_at FROM user_profiles WHERE speaker = ? AND guild_id = ?",
                (speaker, guild_id),
            ).fetchone()
        finally:
            self._release(con)

        if row is None:
            return True

        age_seconds = time.time() - row[0]
        return age_seconds > max_age_hours * 3600

    async def _call_llm(self, prompt: str) -> str:
        """呼叫 Groq LLM 壓縮 prompt（抽出讓測試可 mock）"""
        client = AsyncGroq()  # 從環境變數 GROQ_API_KEY 讀取
        response = await client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300,
        )
        return response.choices[0].message.content.strip()

    async def compress(self, speaker: str, guild_id: int) -> str | None:
        """
        1. 拉最近 7 天逐字稿
        2. 若少於 5 筆，回傳 None（資料不足）
        3. 呼叫 LLM 壓縮成 profile
        4. 存入 user_profiles，回傳 profile 文字
        """
        transcripts = self._store.get_recent(speaker, guild_id, days=7)

        if len(transcripts) < _MIN_TRANSCRIPT_COUNT:
            logger.debug(
                f"[ProfileCompressor] {speaker}@{guild_id} 逐字稿不足 "
                f"({len(transcripts)}/{_MIN_TRANSCRIPT_COUNT})，跳過壓縮。"
            )
            return None

        transcript_lines = "\n".join(
            f"[{t['speaker']}] {t['text']}" for t in transcripts
        )
        prompt = _COMPRESS_PROMPT_TEMPLATE.format(
            speaker=speaker,
            transcripts=transcript_lines,
        )

        try:
            profile_text = await self._call_llm(prompt)
        except Exception as e:
            logger.error(f"[ProfileCompressor] LLM 呼叫失敗: {e}")
            return None

        if not profile_text:
            logger.warning(f"[ProfileCompressor] LLM 回傳空字串，跳過儲存。")
            return None

        self._upsert_profile(speaker, guild_id, profile_text, time.time())
        logger.info(f"[ProfileCompressor] {speaker}@{guild_id} profile 已更新（{len(profile_text)} chars）")
        return profile_text

    async def compress_if_stale(self, speaker: str, guild_id: int, max_age_hours: int = 24) -> str | None:
        """
        若 is_stale → 呼叫 compress，否則直接回傳 get_profile
        """
        if not self.is_stale(speaker, guild_id, max_age_hours=max_age_hours):
            return self.get_profile(speaker, guild_id)
        return await self.compress(speaker, guild_id)
