"""
DetectiveEngine — 謊言偵探遊戲引擎 stub。
實際邏輯由另一個 agent 實作；此處僅提供介面定義，讓 cog 可以 import。
"""
from __future__ import annotations

from typing import Callable, Awaitable, Optional

from game.detective.session import DetectiveSession, DetectiveState


class DetectiveEngine:
    def __init__(
        self,
        session: DetectiveSession,
        on_state_change: Callable[[DetectiveSession], Awaitable[None]],
        db_path: str = "marvin.db",
    ):
        self._session = session
        self._on_state_change = on_state_change
        self._db_path = db_path

    async def add_player(self, user_id: str, display_name: str) -> bool:
        """加入玩家；回傳 True 表示成功加入。"""
        raise NotImplementedError

    async def start_game(self) -> bool:
        """開始遊戲（至少 3 人）；回傳 True 表示成功。"""
        raise NotImplementedError

    async def submit_statements(
        self, declarer_id: str, a: str, b: str, c: str, lie_index: int
    ) -> bool:
        """陳述者提交三句話與謊言索引（0=A,1=B,2=C）；回傳 True 表示成功。"""
        raise NotImplementedError

    async def submit_vote(self, voter_id: str, vote_index: int) -> dict:
        """
        投票；回傳：
          {"already_voted": bool, "all_voted": bool}
          或 {"error": str}
        """
        raise NotImplementedError

    async def close_voting(self) -> dict:
        """
        結束投票；回傳：
          {
            "lie_index": int,
            "correct_voters": [...],
            "fooled_voters": [...],
            "score_changes": {uid: delta},
            "skipped": bool,
          }
        """
        raise NotImplementedError

    async def skip_declaring(self) -> bool:
        """跳過當前陳述者；回傳 True 表示成功。"""
        raise NotImplementedError

    async def advance_declaring(self) -> bool:
        """推進到下一個陳述者（或結束遊戲）；回傳 True 表示成功。"""
        raise NotImplementedError
