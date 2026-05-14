from __future__ import annotations

import asyncio
import random
import sqlite3
import time
from typing import Any, Callable, Awaitable

from game.detective.session import (
    DetectiveSession,
    DetectiveState,
    PlayerDState,
    StatementSet,
)
from game.player_score_db import add_scores, init_table as init_scores_table
from game.game_memory_db import init_table as init_memory_table, write_event

MAX_PLAYERS = 8
GUESSER_CORRECT_SCORE = 50    # 猜中謊言
DECLARER_PER_FOOL_SCORE = 30  # 陳述者每騙過一人


class DetectiveEngine:
    """
    Core state machine for the Two Truths One Lie detective game.

    All Discord UI is delegated via callbacks injected at construction —
    this class never imports discord.

    _notify() is always called OUTSIDE the lock to avoid deadlock and
    to ensure session.last_round_result is visible before on_state_change runs.
    """

    def __init__(
        self,
        session: DetectiveSession,
        *,
        on_state_change: Callable[[DetectiveSession], Awaitable[None]],
        db_path: str = "marvin.db",
    ) -> None:
        self.session = session
        self._on_state_change = on_state_change
        self._db_path = db_path
        self._lock = asyncio.Lock()

        try:
            loop = asyncio.get_running_loop()
            loop.run_in_executor(None, self._init_db)
        except RuntimeError:
            self._init_db()  # 同步執行（測試環境無 event loop）

    # ------------------------------------------------------------------
    # DB helpers (run in thread via run_in_executor)
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        con = sqlite3.connect(self._db_path)
        try:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS detective_sessions (
                    session_id   TEXT PRIMARY KEY,
                    guild_id     INTEGER,
                    channel_id   INTEGER,
                    player_count INTEGER,
                    started_at   REAL,
                    ended_at     REAL
                );

                CREATE TABLE IF NOT EXISTS detective_rounds (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id     TEXT,
                    round_num      INTEGER,
                    declarer_id    TEXT,
                    declarer_name  TEXT,
                    lie_index      INTEGER,
                    stmt_a         TEXT,
                    stmt_b         TEXT,
                    stmt_c         TEXT,
                    fooled_count   INTEGER,
                    correct_count  INTEGER,
                    skipped        INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS detective_votes (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id  TEXT,
                    round_num   INTEGER,
                    voter_id    TEXT,
                    voter_name  TEXT,
                    vote_index  INTEGER,
                    correct     INTEGER
                );
            """)
            init_scores_table(con)
            init_memory_table(con)
            con.commit()
        finally:
            con.close()

    def _write_round(
        self,
        session_id: str,
        round_num: int,
        declarer_id: str,
        declarer_name: str,
        lie_index: int | None,
        stmt_a: str | None,
        stmt_b: str | None,
        stmt_c: str | None,
        fooled_count: int,
        correct_count: int,
        skipped: int,
    ) -> None:
        con = sqlite3.connect(self._db_path)
        try:
            con.execute(
                """
                INSERT INTO detective_rounds (
                    session_id, round_num, declarer_id, declarer_name,
                    lie_index, stmt_a, stmt_b, stmt_c,
                    fooled_count, correct_count, skipped
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id, round_num, declarer_id, declarer_name,
                    lie_index, stmt_a, stmt_b, stmt_c,
                    fooled_count, correct_count, skipped,
                ),
            )
            if not skipped:
                lie_label = "ABC"[lie_index] if lie_index is not None else "?"
                write_event(
                    con,
                    f"【謊言偵探】{declarer_name} 說謊（選項{lie_label}）"
                    f"騙倒 {fooled_count} 人，{correct_count} 人識破",
                )
            con.commit()
        except Exception:
            pass
        finally:
            con.close()

    def _write_session_end(
        self,
        session_id: str,
        guild_id: int,
        channel_id: int,
        player_count: int,
        started_at: float,
        ended_at: float,
    ) -> None:
        con = sqlite3.connect(self._db_path)
        try:
            con.execute(
                """
                INSERT OR REPLACE INTO detective_sessions (
                    session_id, guild_id, channel_id, player_count, started_at, ended_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (session_id, guild_id, channel_id, player_count, started_at, ended_at),
            )
            add_scores(con, [(p.user_id, p.display_name, p.score) for p in self.session.players])
            sorted_players = sorted(self.session.players, key=lambda p: p.score, reverse=True)
            scores_text = "、".join(
                f"{p.display_name} {p.score}分" for p in sorted_players if p.score > 0
            ) or "無人得分"
            write_event(con, f"【謊言偵探 結算】{scores_text}")
            con.commit()
        except Exception:
            pass
        finally:
            con.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _notify(self) -> None:
        await self._on_state_change(self.session)

    def _get_player(self, user_id: str) -> PlayerDState | None:
        for p in self.session.players:
            if p.user_id == user_id:
                return p
        return None

    def _declarer_player(self) -> PlayerDState | None:
        if self.session.current_declarer_id is None:
            return None
        return self._get_player(self.session.current_declarer_id)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def add_player(self, user_id: str, display_name: str) -> bool:
        """
        Add a player to the session during IDLE or JOINING phase.

        Returns False if game is active, player already joined, or session is full.
        """
        notify = False
        async with self._lock:
            if self.session.state not in (DetectiveState.IDLE, DetectiveState.JOINING):
                return False
            if len(self.session.players) >= MAX_PLAYERS:
                return False
            if self._get_player(user_id) is not None:
                return False
            self.session.players.append(PlayerDState(user_id=user_id, display_name=display_name))
            if self.session.state == DetectiveState.IDLE:
                self.session.state = DetectiveState.JOINING
            notify = True

        if notify:
            await self._notify()
        return True

    async def start_game(self) -> bool:
        """
        Start the game. Requires at least 3 players.
        Transitions JOINING → DECLARING.
        """
        async with self._lock:
            if self.session.state != DetectiveState.JOINING:
                return False
            if len(self.session.players) < 3:
                return False

            all_ids = [p.user_id for p in self.session.players]
            random.shuffle(all_ids)
            self.session.declarer_queue = all_ids
            self.session.current_declarer_id = self.session.declarer_queue.pop(0)
            self.session.started_at = time.time()

            for p in self.session.players:
                p.vote = None

            self.session.state = DetectiveState.DECLARING

        await self._notify()
        return True

    async def submit_statements(
        self,
        declarer_id: str,
        a: str,
        b: str,
        c: str,
        lie_index: int,
    ) -> bool:
        """
        Submit the three statements. Transitions DECLARING → VOTING.
        Returns False on invalid input.
        """
        async with self._lock:
            if self.session.state != DetectiveState.DECLARING:
                return False
            if self.session.current_declarer_id != declarer_id:
                return False
            if lie_index not in (0, 1, 2):
                return False

            self.session.current_statements = StatementSet(a=a, b=b, c=c, lie_index=lie_index)
            for p in self.session.players:
                if p.user_id != declarer_id:
                    p.vote = None
            self.session.state = DetectiveState.VOTING

        await self._notify()
        return True

    async def submit_vote(self, voter_id: str, vote_index: int) -> dict[str, Any]:
        """
        Submit a vote during VOTING phase.

        Returns:
        - {"error": "invalid_state"} if not in VOTING
        - {"error": "invalid_voter"} if voter is declarer or not in session
        - {"error": "invalid_vote_index"} if vote_index not in 0/1/2
        - {"already_voted": True, "all_voted": False} if already voted
        - {"already_voted": False, "all_voted": True/False} on success
        """
        async with self._lock:
            if self.session.state != DetectiveState.VOTING:
                return {"error": "invalid_state"}
            if voter_id == self.session.current_declarer_id:
                return {"error": "invalid_voter"}
            if vote_index not in (0, 1, 2):
                return {"error": "invalid_vote_index"}

            player = self._get_player(voter_id)
            if player is None:
                return {"error": "invalid_voter"}

            if player.vote is not None:
                return {"already_voted": True, "all_voted": False}

            player.vote = vote_index

            non_declarers = [p for p in self.session.players if p.user_id != self.session.current_declarer_id]
            all_voted = all(p.vote is not None for p in non_declarers)

            return {"already_voted": False, "all_voted": all_voted}

    async def close_voting(self) -> dict[str, Any]:
        """
        Close the voting phase. Transitions VOTING → REVEALING.

        Stores result in session.last_round_result before calling _notify(),
        so on_state_change(REVEALING) always reads the current round's data.

        Returns the result dict, or {"error": "invalid_state"} if not in VOTING.
        """
        async with self._lock:
            if self.session.state != DetectiveState.VOTING:
                return {"error": "invalid_state"}

            statements = self.session.current_statements
            if statements is None:
                return {"error": "no_statements"}

            lie_index = statements.lie_index
            declarer_id = self.session.current_declarer_id

            correct_voters: list[str] = []
            fooled_voters: list[str] = []
            unvoted: list[str] = []
            score_changes: dict[str, int] = {}

            for p in self.session.players:
                if p.user_id == declarer_id:
                    continue
                if p.vote is None:
                    unvoted.append(p.user_id)
                    continue
                if p.vote == lie_index:
                    correct_voters.append(p.user_id)
                    p.score += GUESSER_CORRECT_SCORE
                    score_changes[p.user_id] = GUESSER_CORRECT_SCORE
                else:
                    fooled_voters.append(p.user_id)

            if declarer_id is not None:
                declarer = self._get_player(declarer_id)
                declarer_pts = len(fooled_voters) * DECLARER_PER_FOOL_SCORE
                if declarer is not None and declarer_pts > 0:
                    declarer.score += declarer_pts
                    score_changes[declarer_id] = declarer_pts

            declarer_obj = self._declarer_player()
            if declarer_obj is not None:
                declarer_obj.has_declared = True

            result = {
                "lie_index": lie_index,
                "correct_voters": correct_voters,
                "fooled_voters": fooled_voters,
                "unvoted": unvoted,
                "score_changes": score_changes,
                "skipped": False,
            }

            # 存入 session，讓 on_state_change(REVEALING) 讀取
            self.session.last_round_result = result
            self.session.state = DetectiveState.REVEALING

        # DB 寫入（非阻塞）
        try:
            loop = asyncio.get_running_loop()
            declarer_name = declarer_obj.display_name if declarer_obj else (declarer_id or "")
            stmts = self.session.current_statements
            loop.run_in_executor(
                None,
                self._write_round,
                self.session.session_id,
                self.session.round_num,
                declarer_id or "",
                declarer_name,
                lie_index,
                stmts.a if stmts else None,
                stmts.b if stmts else None,
                stmts.c if stmts else None,
                len(fooled_voters),
                len(correct_voters),
                0,
            )
        except RuntimeError:
            pass

        await self._notify()
        return result

    async def skip_declaring(self) -> bool:
        """
        Declarer timed out — skip this round without statements.
        Marks has_declared, then calls advance_declaring.
        Returns True if game continues, False if game over.
        """
        async with self._lock:
            if self.session.state != DetectiveState.DECLARING:
                return False
            declarer = self._declarer_player()
            if declarer is not None:
                declarer.has_declared = True

            try:
                loop = asyncio.get_running_loop()
                declarer_name = declarer.display_name if declarer else (self.session.current_declarer_id or "")
                loop.run_in_executor(
                    None,
                    self._write_round,
                    self.session.session_id,
                    self.session.round_num,
                    self.session.current_declarer_id or "",
                    declarer_name,
                    None, None, None, None,
                    0, 0,
                    1,
                )
            except RuntimeError:
                pass

        return await self.advance_declaring()

    async def advance_declaring(self) -> bool:
        """
        Advance to the next declarer after REVEALING or skip.

        Only valid when state is REVEALING or DECLARING (skip path).
        Returns True if game continues, False if game over.
        """
        game_over = False
        async with self._lock:
            if self.session.state not in (DetectiveState.REVEALING, DetectiveState.DECLARING):
                return False

            if not self.session.declarer_queue:
                try:
                    loop = asyncio.get_running_loop()
                    loop.run_in_executor(
                        None,
                        self._write_session_end,
                        self.session.session_id,
                        self.session.guild_id,
                        self.session.channel_id,
                        len(self.session.players),
                        self.session.started_at,
                        time.time(),
                    )
                except RuntimeError:
                    pass
                self.session.state = DetectiveState.GAME_OVER
                game_over = True
            else:
                self.session.current_declarer_id = self.session.declarer_queue.pop(0)
                self.session.round_num += 1
                self.session.current_statements = None
                self.session.last_round_result = None
                for p in self.session.players:
                    p.vote = None
                self.session.state = DetectiveState.DECLARING

        await self._notify()
        return not game_over
