"""
context_injector.py
記憶系統第四層：Context 自動注入

整合 ProfileCompressor（living profile）與 VectorStore（相關過去片段），
在 LLM 呼叫前將使用者上下文 prepend 到 user_prompt。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class ContextInjector:
    def __init__(
        self,
        transcript_store=None,
        profile_compressor=None,
        vector_store=None,
    ):
        """
        全部可選注入，沒注入就用預設（db_path="marvin.db"）。
        預設初始化會 lazy import，避免在測試環境強制拉起 DB/ChromaDB。
        """
        self._compressor = profile_compressor
        self._vector = vector_store
        self._transcript_store = transcript_store  # 保留引用，供未來擴充

        # 若未注入，lazy 初始化留到第一次 enrich 呼叫時建立
        self._initialized = (profile_compressor is not None and vector_store is not None)

    def _lazy_init(self) -> None:
        """若外部未注入下層元件，使用預設 db_path 初始化。"""
        if self._initialized:
            return
        from profile_compressor import ProfileCompressor
        from vector_store import VectorStore

        if self._compressor is None:
            self._compressor = ProfileCompressor(db_path="marvin.db")
        if self._vector is None:
            self._vector = VectorStore(persist_dir=".chroma_db")
        self._initialized = True

    async def enrich(self, speaker: str, guild_id: int, query: str) -> str:
        """
        回傳應該 prepend 到 user_prompt 的上下文字串。
        若無相關記憶，回傳空字串。

        格式：
        【{speaker} 的過去上下文】
        - 整體印象：{profile}（若有）
        - 相關片段 1
        - 相關片段 2
        ...
        """
        self._lazy_init()

        parts: list[str] = []

        # 1. 取 living profile（若有）
        try:
            profile = self._compressor.get_profile(speaker, guild_id)
            if profile:
                parts.append(f"整體印象：{profile}")
        except Exception as e:
            logger.warning(f"[ContextInjector] get_profile 失敗: {e}")

        # 2. 向量搜尋相關片段（top 3）
        try:
            snippets = self._vector.search(speaker, guild_id, query, top_k=3)
            for s in snippets:
                parts.append(s)
        except Exception as e:
            logger.warning(f"[ContextInjector] vector search 失敗: {e}")

        if not parts:
            return ""

        lines = "\n".join(f"- {p}" for p in parts)
        return f"【{speaker} 的過去上下文】\n{lines}\n"
