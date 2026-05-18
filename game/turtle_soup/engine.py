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

    async def request_hint(self) -> Optional[str]:
        """玩家主動或 idle timer 觸發。回傳下一條 hint 的文字，或 None 表已用完。

        v1 升級：個人化排序 — 不再依 puzzle.hints 的線性順序，改用「資訊增益」演算法：
        1. 從 session.given_hint_indices + asked_questions 推斷玩家已探索的節點
        2. 對每個未給過的 hint，算 new_nodes = reveals - explored
        3. 選 new_nodes 最少（最循序漸進）的 hint；同數量選 reveals 最少（最乾淨）的
        4. 跳過已探索完所有節點的 hint（沒新資訊不重複給）

        對於線性 ELEVATOR_18F 來說，這個演算法產生的順序跟原本一樣（向下相容）；
        但對於有分支的 puzzle，會根據玩家問題動態挑最適合的 hint。
        """
        async with self._lock:
            if self.session.state != TurtleSoupState.ASKING:
                return None
            idx = self._select_next_hint_index()
            if idx is None:
                return None
            hint = self.puzzle.hints[idx]
            self.session.given_hint_indices.append(idx)
            self.session.hints_given += 1
            return hint.text

    def _explored_node_ids(self) -> set[str]:
        """從已給的 hint + 玩家已問問題（keyword 匹配）推斷玩家「探索過」的節點。

        探索 != 知道答案，只表示玩家曾「碰到」這個節點對應的方向。
        後續 hint 排序時會降低重複探索節點的優先度。
        """
        explored: set[str] = set()
        for i in self.session.given_hint_indices:
            if 0 <= i < len(self.puzzle.hints):
                explored.update(self.puzzle.hints[i].reveals)

        # 玩家問題的 keyword 匹配
        asked_texts = [q.question for q in self.session.asked_questions]
        for node in self.puzzle.hint_nodes:
            for kw in node.keywords:
                if any(kw in text for text in asked_texts):
                    explored.add(node.id)
                    break
        return explored

    def _select_next_hint_index(self) -> Optional[int]:
        """選下一條最適合的 hint，回傳 puzzle.hints 的 index。

        排序：
        - 主鍵：new_nodes 數（少 → 多）— 越循序漸進越好
        - 次鍵：total reveals 數（少 → 多）— 同樣 info gain 下選乾淨的
        - 末鍵：原 list 順序（先 → 後）— 作者決定的優先序當 tie-breaker
        """
        explored = self._explored_node_ids()
        given = set(self.session.given_hint_indices)

        candidates: list[tuple[int, int, int]] = []
        for i, hint in enumerate(self.puzzle.hints):
            if i in given:
                continue
            reveals = set(hint.reveals)
            new_nodes = reveals - explored
            if not new_nodes:
                continue  # 沒新資訊，跳過
            candidates.append((len(new_nodes), len(reveals), i))

        if not candidates:
            return None
        candidates.sort()  # ascending: 小 new_nodes 優先，同時小 reveals 優先
        return candidates[0][2]

    async def cancel(self) -> bool:
        """從任何 state 強制結束。GAME_OVER 不重複觸發。"""
        async with self._lock:
            if self.session.state == TurtleSoupState.GAME_OVER:
                return False
            self.session.state = TurtleSoupState.GAME_OVER
            self.session.end_reason = EndReason.CANCELLED
        await self._on_state_change(self.session)
        return True
