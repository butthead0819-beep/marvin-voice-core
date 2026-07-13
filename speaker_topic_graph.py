"""SpeakerTopicGraph — 三個核心 social agent 共用的社交記憶。

設計來源：docs/social_catalyst_plan.md（Week 1 基建）。

職責：
  - 記錄每句 utterance 的 (speaker, channel, text, embedding, emotion)
  - 供 BridgeAgent 查詢「topic 相似 + 非 self + 在場 + 未在 cooldown」的橋接候選
  - 供 MoodAgent 寫入 emotion 標籤
  - 供 DuckingAgent 拉 recent 做 turn-taking 偵測

不變式：
  - 寫入是同步、idempotent-safe（重複呼叫不爆但會多筆）
  - 不依賴 sentence-transformers，embedding 是 optional bytes blob
  - 沿用 marvin.db，不開新 DB（plan §SpeakerTopicGraph）
  - 與既有 TranscriptStore 平行存在（前者是純文字，本表加 embedding + emotion）
"""
from __future__ import annotations

import sqlite3
import time
from typing import Iterable

import numpy as np

import memory_sandbox


_DEFAULT_COOLDOWN_DAYS = 30
_EMBED_DIM_TOLERANCE = 1024  # 防呆，最大允許 dim


class SpeakerTopicGraph:
    def __init__(self, db_path: str = "marvin.db") -> None:
        self._db_path = db_path
        if db_path == ":memory:":
            self._con: sqlite3.Connection | None = sqlite3.connect(":memory:")
        else:
            self._con = None
        self._init_db()

    # ── connection helpers ───────────────────────────────────────────────────

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
                CREATE TABLE IF NOT EXISTS speaker_topic_graph (
                    transcript_id   INTEGER PRIMARY KEY AUTOINCREMENT,
                    speaker         TEXT    NOT NULL,
                    channel_id      INTEGER NOT NULL,
                    text            TEXT    NOT NULL,
                    embedding       BLOB,
                    emotion_text    TEXT,
                    emotion_prosody TEXT,
                    last_bridged_at REAL    NOT NULL DEFAULT 0,
                    created_at      REAL    NOT NULL
                )
            """)
            con.execute("""
                CREATE INDEX IF NOT EXISTS idx_stg_channel_ts
                ON speaker_topic_graph (channel_id, created_at DESC)
            """)
            con.execute("""
                CREATE INDEX IF NOT EXISTS idx_stg_speaker_ts
                ON speaker_topic_graph (speaker, created_at DESC)
            """)
            con.commit()
        finally:
            self._release(con)

    # ── write path ───────────────────────────────────────────────────────────

    def record_utterance(
        self,
        speaker: str,
        channel_id: int,
        text: str,
        *,
        embedding: bytes | None = None,
        ts: float | None = None,
    ) -> int | None:
        """寫一句 utterance；空白文字直接 noop（回 None）。回傳 transcript_id。"""
        if not text or not text.strip():
            return None
        if memory_sandbox.active():
            return None  # 沙盒：寫入 no-op（ephemeral，斷線丟棄）
        if ts is None:
            ts = time.time()
        con = self._connect()
        try:
            cur = con.execute(
                """INSERT INTO speaker_topic_graph
                   (speaker, channel_id, text, embedding, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (speaker, channel_id, text, embedding, ts),
            )
            con.commit()
            return cur.lastrowid
        finally:
            self._release(con)

    def set_emotion(
        self,
        transcript_id: int,
        *,
        text_emotion: str | None = None,
        prosody_emotion: str | None = None,
    ) -> None:
        if memory_sandbox.active():
            return
        con = self._connect()
        try:
            con.execute(
                """UPDATE speaker_topic_graph
                   SET emotion_text = COALESCE(?, emotion_text),
                       emotion_prosody = COALESCE(?, emotion_prosody)
                   WHERE transcript_id = ?""",
                (text_emotion, prosody_emotion, transcript_id),
            )
            con.commit()
        finally:
            self._release(con)

    def mark_bridged(self, transcript_id: int, *, ts: float | None = None) -> None:
        """標記某句已被 BridgeAgent 使用過 → cooldown_days 內不再被選為 callback。"""
        if memory_sandbox.active():
            return
        if ts is None:
            ts = time.time()
        con = self._connect()
        try:
            con.execute(
                "UPDATE speaker_topic_graph SET last_bridged_at = ? WHERE transcript_id = ?",
                (ts, transcript_id),
            )
            con.commit()
        finally:
            self._release(con)

    # ── read path ────────────────────────────────────────────────────────────

    def recent(self, channel_id: int, n: int = 20) -> list[dict]:
        con = self._connect()
        try:
            rows = con.execute(
                """SELECT transcript_id, speaker, text, embedding,
                          emotion_text, emotion_prosody, last_bridged_at, created_at
                   FROM speaker_topic_graph
                   WHERE channel_id = ?
                   ORDER BY created_at DESC
                   LIMIT ?""",
                (channel_id, n),
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]
        finally:
            self._release(con)

    def find_similar(
        self,
        query_embedding: bytes,
        channel_id: int,
        *,
        exclude_speaker: str,
        present_speakers: Iterable[str] | None = None,
        min_similarity: float = 0.5,
        cooldown_days: int = _DEFAULT_COOLDOWN_DAYS,
        window_days: int = 30,
        limit: int = 20,
    ) -> list[dict]:
        """Cosine 相似度查詢。回傳排序好的 list[dict]，每筆多一個 'similarity' 欄位。"""
        q = _decode_embedding(query_embedding)
        if q is None:
            return []

        cutoff_ts = time.time() - window_days * 86400
        bridge_cutoff = time.time() - cooldown_days * 86400
        present = set(present_speakers) if present_speakers is not None else None

        con = self._connect()
        try:
            rows = con.execute(
                """SELECT transcript_id, speaker, text, embedding,
                          emotion_text, emotion_prosody, last_bridged_at, created_at
                   FROM speaker_topic_graph
                   WHERE channel_id = ?
                     AND speaker != ?
                     AND created_at >= ?
                     AND last_bridged_at < ?
                     AND embedding IS NOT NULL""",
                (channel_id, exclude_speaker, cutoff_ts, bridge_cutoff),
            ).fetchall()
        finally:
            self._release(con)

        results: list[dict] = []
        for r in rows:
            d = self._row_to_dict(r)
            if present is not None and d["speaker"] not in present:
                continue
            emb = _decode_embedding(d["embedding"])
            if emb is None or emb.shape != q.shape:
                continue
            sim = _cosine(q, emb)
            if sim < min_similarity:
                continue
            d["similarity"] = float(sim)
            d.pop("embedding", None)
            results.append(d)

        results.sort(key=lambda x: x["similarity"], reverse=True)
        return results[:limit]

    def find_similar_by_text(
        self,
        query_text: str,
        channel_id: int,
        *,
        exclude_speaker: str,
        present_speakers: Iterable[str] | None = None,
        window_days: int = 30,
        cooldown_days: int = _DEFAULT_COOLDOWN_DAYS,
        limit: int = 20,
    ) -> list[dict]:
        """無 embedding 時的退路：character-level overlap。粗糙但比 0 好。

        中文 token 化沒做（怕拉 jieba 依賴），用 unique-char 重疊近似 keyword overlap。
        英文短句準度差，但 BridgeAgent 主場景是中文。
        """
        cutoff_ts = time.time() - window_days * 86400
        bridge_cutoff = time.time() - cooldown_days * 86400
        present = set(present_speakers) if present_speakers is not None else None

        con = self._connect()
        try:
            rows = con.execute(
                """SELECT transcript_id, speaker, text, embedding,
                          emotion_text, emotion_prosody, last_bridged_at, created_at
                   FROM speaker_topic_graph
                   WHERE channel_id = ?
                     AND speaker != ?
                     AND created_at >= ?
                     AND last_bridged_at < ?""",
                (channel_id, exclude_speaker, cutoff_ts, bridge_cutoff),
            ).fetchall()
        finally:
            self._release(con)

        q_chars = set(query_text) - set(" \t\n，。！？,.!?")
        if not q_chars:
            return []

        scored: list[dict] = []
        for r in rows:
            d = self._row_to_dict(r)
            if present is not None and d["speaker"] not in present:
                continue
            t_chars = set(d["text"]) - set(" \t\n，。！？,.!?")
            if not t_chars:
                continue
            overlap = len(q_chars & t_chars) / max(len(q_chars), 1)
            if overlap <= 0:
                continue
            d["similarity"] = overlap
            d.pop("embedding", None)
            scored.append(d)

        scored.sort(key=lambda x: x["similarity"], reverse=True)
        return scored[:limit]

    # ── internals ────────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_dict(r) -> dict:
        return {
            "transcript_id": r[0],
            "speaker": r[1],
            "text": r[2],
            "embedding": r[3],
            "emotion_text": r[4],
            "emotion_prosody": r[5],
            "last_bridged_at": r[6],
            "created_at": r[7],
        }


# ── pure helpers ─────────────────────────────────────────────────────────────


def _decode_embedding(blob: bytes | None) -> np.ndarray | None:
    if blob is None:
        return None
    try:
        arr = np.frombuffer(blob, dtype=np.float32)
    except (ValueError, TypeError):
        return None
    if arr.size == 0 or arr.size > _EMBED_DIM_TOLERANCE:
        return None
    return arr


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))
