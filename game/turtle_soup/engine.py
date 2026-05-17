"""海龜湯 Engine — 狀態機 + 問答紀錄。

不 import discord。所有 Discord 行為由 cog 透過 on_state_change 觸發。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, Optional

from game.turtle_soup.session import (
    AskedQuestion,
    EndReason,
    TurtleSoupPlayer,
    TurtleSoupSession,
    TurtleSoupState,
)
from game.turtle_soup.puzzles import Puzzle
from game.turtle_soup import llm_judge

logger = logging.getLogger(__name__)


class TurtleSoupEngine:
    """主狀態機。每次 state 變動會 await on_state_change(session)。"""

    def __init__(
        self,
        session: TurtleSoupSession,
        puzzle: Puzzle,
        *,
        on_state_change: Callable[[TurtleSoupSession], Awaitable[None]],
    ) -> None:
        self.session = session
        self.puzzle = puzzle
        self.session.puzzle_id = puzzle.id
        self._on_state_change = on_state_change
        self._lock = asyncio.Lock()

    # ── 階段轉移 ──────────────────────────────────────────────────────────────

    async def start_game(self) -> bool:
        """IDLE → JOINING。"""
        async with self._lock:
            if self.session.state != TurtleSoupState.IDLE:
                return False
            self.session.state = TurtleSoupState.JOINING
            self.session.started_at = time.time()
        await self._on_state_change(self.session)
        return True

    async def add_player(self, user_id: str, display_name: str) -> bool:
        """JOINING 階段加玩家。已加入回 False。"""
        async with self._lock:
            if self.session.state != TurtleSoupState.JOINING:
                return False
            if any(p.user_id == user_id for p in self.session.players):
                return False
            self.session.players.append(TurtleSoupPlayer(user_id=user_id, display_name=display_name))
            return True

    async def begin_presenting(self) -> bool:
        """JOINING → PRESENTING。需至少 1 位玩家。"""
        async with self._lock:
            if self.session.state != TurtleSoupState.JOINING:
                return False
            if not self.session.players:
                return False
            self.session.state = TurtleSoupState.PRESENTING
        await self._on_state_change(self.session)
        return True

    async def begin_asking(self) -> bool:
        """PRESENTING → ASKING。"""
        async with self._lock:
            if self.session.state != TurtleSoupState.PRESENTING:
                return False
            self.session.state = TurtleSoupState.ASKING
        await self._on_state_change(self.session)
        return True

    # ── 核心動作：問是非題 ───────────────────────────────────────────────────

    async def submit_question(
        self,
        asker_id: str,
        asker_name: str,
        question: str,
    ) -> Optional[dict]:
        """ASKING 階段送出問題。回傳 judge result，或 None 表 reject。

        達 max_questions 後該問題仍 judge，但 state 隨後轉 GAME_OVER（exhausted）。
        """
        # 狀態檢查（在 lock 外做快速 reject，省 LLM 呼叫）
        if self.session.state != TurtleSoupState.ASKING:
            return None

        history = self.session.recent_question_texts

        # LLM judge 在 lock 外執行（耗時 0.5-2s，不能卡 state 操作）
        result = await llm_judge.judge_question(
            surface=self.puzzle.surface,
            truth=self.puzzle.truth,
            question=question,
            history=history,
            leak_keywords=self.puzzle.leak_keywords,
        )

        is_exhausted = False
        async with self._lock:
            # TOCTOU：LLM 期間 state 可能變
            if self.session.state != TurtleSoupState.ASKING:
                return None
            self.session.asked_questions.append(AskedQuestion(
                asker_id=asker_id,
                asker_name=asker_name,
                question=question,
                verdict=result["verdict"],
                narration=result["narration"],
                provider=result["_provider"],
                timestamp=time.time(),
            ))
            if self.session.questions_count >= self.session.max_questions:
                self.session.state = TurtleSoupState.GAME_OVER
                self.session.end_reason = EndReason.EXHAUSTED
                is_exhausted = True

        if is_exhausted:
            await self._on_state_change(self.session)
            return result

        # 自動 WIN 偵測：verdict=yes 時，跑二次 final_guess 檢查
        # 若玩家的問題本身已涵蓋兩個核心 key_fact → 視同猜中，end_reason=WIN
        if result["verdict"] == "yes":
            final_check = await llm_judge.judge_final_guess(
                surface=self.puzzle.surface,
                truth=self.puzzle.truth,
                key_facts=self.puzzle.key_facts,
                player_answer=question,
            )
            if final_check["accepted"]:
                async with self._lock:
                    if self.session.state != TurtleSoupState.ASKING:
                        return result
                    self.session.state = TurtleSoupState.GAME_OVER
                    self.session.end_reason = EndReason.WIN
                await self._on_state_change(self.session)
                # 把 auto-win 訊號加進結果，cog 可選擇是否要特別播報
                result = {**result, "auto_win": True, "final_check": final_check}

        return result

    # ── 結束流程 ──────────────────────────────────────────────────────────────

    async def surrender(self, user_id: str, display_name: str) -> bool:
        """ASKING → GAME_OVER (SURRENDER)。非 ASKING 狀態 no-op。"""
        async with self._lock:
            if self.session.state != TurtleSoupState.ASKING:
                return False
            self.session.state = TurtleSoupState.GAME_OVER
            self.session.end_reason = EndReason.SURRENDER
        await self._on_state_change(self.session)
        return True

    async def submit_final_guess(
        self,
        user_id: str,
        display_name: str,
        player_answer: str,
    ) -> Optional[dict]:
        """ASKING 階段送最終答案。

        accepted=True → 進入 GAME_OVER (WIN)。
        accepted=False → 留在 ASKING。
        非 ASKING 狀態回 None。
        """
        if self.session.state != TurtleSoupState.ASKING:
            return None

        result = await llm_judge.judge_final_guess(
            surface=self.puzzle.surface,
            truth=self.puzzle.truth,
            key_facts=self.puzzle.key_facts,
            player_answer=player_answer,
        )

        accepted = result["accepted"]
        async with self._lock:
            if self.session.state != TurtleSoupState.ASKING:
                return None
            if accepted:
                self.session.state = TurtleSoupState.GAME_OVER
                self.session.end_reason = EndReason.WIN

        if accepted:
            await self._on_state_change(self.session)

        return result

    async def cancel(self) -> bool:
        """從任何 state 強制結束。GAME_OVER 不重複觸發。"""
        async with self._lock:
            if self.session.state == TurtleSoupState.GAME_OVER:
                return False
            self.session.state = TurtleSoupState.GAME_OVER
            self.session.end_reason = EndReason.CANCELLED
        await self._on_state_change(self.session)
        return True
