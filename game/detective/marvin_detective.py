"""
MarvinDetective — Marvin 在謊言偵探的 AI 行為 stub。
實際邏輯由另一個 agent 實作；此處僅提供介面定義，讓 cog 可以 import。
"""
from __future__ import annotations

from typing import Optional


class MarvinDetective:
    async def generate_vote(
        self, statements: dict, player_name: str
    ) -> tuple[int, str]:
        """
        分析陳述並決定投票。
        statements = {"a": str, "b": str, "c": str}
        回傳 (vote_index: int, comment: str)，vote_index 為 0=A/1=B/2=C。
        """
        raise NotImplementedError

    async def generate_statements(
        self, player_names: list[str]
    ) -> dict:
        """
        生成 Marvin 的三句話與謊言索引。
        回傳 {"a": str, "b": str, "c": str, "lie_index": int}。
        """
        raise NotImplementedError

    async def generate_reveal_quip(
        self, correct: bool, fooled_count: int, declarer_name: str
    ) -> str:
        """
        揭曉後的評語。
        correct: Marvin 是否猜中
        fooled_count: 被騙人數
        """
        raise NotImplementedError
