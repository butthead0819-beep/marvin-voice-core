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

MAX_PLAYERS = 8
GUESSER_CORRECT_SCORE = 50    # 猜中謊言
DECLARER_PER_FOOL_SCORE = 30  # 陳述者每騙過一人


class DetectiveEngine:
    """
    Core state machine for the Two Truths One Lie detective game.

    All Discord UI is delegated via callbacks injected at construction —
    this class never imports discord.

    Parameters
    ----------
    session:
        The DetectiveSession this engine manages.
    on_state_change:
        Async callable(session: DetectiveSession) — called after every state transition.
    db_path:
        Path to the SQLite database file (default: "marvin.db").
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

        # Ensure DB tables exist
        try:
            loop = asyncio.get_running_loop()
            loop.run_in_executor(None, self._init_db)
        except RuntimeError:
            # No running loop (e.g. during tests before first await)
            pass

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
            con.commit()
        finally:
            con.close()

    def _write_votes(
        self,
        session_id: str,
        round_num: int,
        votes: list[tuple[str, str, int, int]],  # (voter_id, voter_name, vote_index, correct)
    ) -> None:
        con = sqlite3.connect(self._db_path)
        try:
            for voter_id, voter_name, vote_index, correct in votes:
                con.execute(
                    """
                    INSERT INTO detective_votes (
                        session_id, round_num, voter_id, voter_name, vote_index, correct
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (session_id, round_num, voter_id, voter_name, vote_index, correct),
                )
            con.commit()
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
            con.commit()
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

    def _clear_votes(self) -> None:
        """Clear all player votes except the current declarer."""
        for p in self.session.players:
            if p.user_id != self.session.current_declarer_id:
                p.vote = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def add_player(self, user_id: str, display_name: str) -> bool:
        """
        Add a player to the session during IDLE or JOINING phase.

        Returns False if:
        - game is not in IDLE/JOINING state
        - player is already in the session
        - session is full (max 8 players)
        """
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
            await self._notify()
            return True

    async def start_game(self) -> bool:
        """
        Start the game. Requires at least 3 players.

        Transitions JOINING → DECLARING.
        Randomly shuffles declarer_queue, sets current_declarer_id to first.
        Clears all votes.
        Returns False if not enough players or wrong state.
        """
        async with self._lock:
            if self.session.state != DetectiveState.JOINING:
                return False
            if len(self.session.players) < 3:
                return False

            # Build declarer queue from all player IDs
            all_ids = [p.user_id for p in self.session.players]
            random.shuffle(all_ids)
            self.session.declarer_queue = all_ids
            # Pop first declarer
            self.session.current_declarer_id = self.session.declarer_queue.pop(0)
            self.session.started_at = time.time()

            # Clear votes
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
        Submit the three statements (two truths + one lie).

        Only valid when:
        - state is DECLARING
        - declarer_id matches current_declarer_id
        - lie_index is 0, 1, or 2

        Transitions DECLARING → VOTING.
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
            # Clear votes for all non-declarers
            for p in self.session.players:
                if p.user_id != declarer_id:
                    p.vote = None
            self.session.state = DetectiveState.VOTING
            await self._notify()
            return True

    async def submit_vote(self, voter_id: str, vote_index: int) -> dict[str, Any]:
        """
        Submit a vote.

        Returns:
        - {"error": "invalid_state"} if not in VOTING state
        - {"error": "invalid_voter"} if voter is the current declarer or not in session
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

            # Check if all non-declarers have voted
            non_declarers = [p for p in self.session.players if p.user_id != self.session.current_declarer_id]
            all_voted = all(p.vote is not None for p in non_declarers)

            return {"already_voted": False, "all_voted": all_voted}

    async def close_voting(self) -> dict[str, Any]:
        """
        Close the voting phase. Transitions VOTING → REVEALING.

        Scoring:
        - Voters who guessed correctly (vote == lie_index): +50
        - Declarer: +30 for each person fooled (voted wrong)
        - Unvoted players: +0 (not penalised)

        Returns:
        {
            "lie_index": int,
            "correct_voters": [user_id],
            "fooled_voters": [user_id],
            "score_changes": {user_id: delta},
            "skipped": False,
        }
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
            score_changes: dict[str, int] = {}

            for p in self.session.players:
                if p.user_id == declarer_id:
                    continue
                if p.vote is None:
                    continue
                if p.vote == lie_index:
                    correct_voters.append(p.user_id)
                    p.score += GUESSER_CORRECT_SCORE
                    score_changes[p.user_id] = GUESSER_CORRECT_SCORE
                else:
                    fooled_voters.append(p.user_id)

            # Declarer scoring: +30 per fooled voter
            if declarer_id is not None:
                declarer = self._get_player(declarer_id)
                declarer_pts = len(fooled_voters) * DECLARER_PER_FOOL_SCORE
                if declarer is not None and declarer_pts > 0:
                    declarer.score += declarer_pts
                    score_changes[declarer_id] = declarer_pts

            # Mark current declarer as declared
            declarer = self._declarer_player()
            if declarer is not None:
                declarer.has_declared = True

            self.session.state = DetectiveState.REVEALING
            await self._notify()

            # Persist round asynchronously
            loop = asyncio.get_running_loop()
            declarer_name = declarer.display_name if declarer else (declarer_id or "")
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
                0,  # not skipped
            )

            return {
                "lie_index": lie_index,
                "correct_voters": correct_voters,
                "fooled_voters": fooled_voters,
                "score_changes": score_changes,
                "skipped": False,
            }

    async def skip_declaring(self) -> bool:
        """
        Declarer timed out — skip this round.

        Marks current declarer as has_declared.
        Does NOT set current_statements.
        Calls advance_declaring internally.
        Returns True if game continues, False if game over.
        """
        async with self._lock:
            if self.session.state != DetectiveState.DECLARING:
                return False
            declarer = self._declarer_player()
            if declarer is not None:
                declarer.has_declared = True

            # Persist skipped round
            loop = asyncio.get_running_loop()
            declarer_name = declarer.display_name if declarer else (self.session.current_declarer_id or "")
            loop.run_in_executor(
                None,
                self._write_round,
                self.session.session_id,
                self.session.round_num,
                self.session.current_declarer_id or "",
                declarer_name,
                None,   # lie_index unknown
                None, None, None,
                0, 0,
                1,  # skipped=True
            )

        # advance_declaring handles its own locking
        return await self.advance_declaring()

    async def advance_declaring(self) -> bool:
        """
        Advance to the next declarer after REVEALING or skip.

        If declarer_queue has more players:
        - Sets current_declarer_id to next
        - Clears all votes
        - Transitions to DECLARING
        - Returns True

        If declarer_queue is empty:
        - Transitions to GAME_OVER
        - Persists session
        - Returns False
        """
        async with self._lock:
            if not self.session.declarer_queue:
                # Game over
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
                self.session.state = DetectiveState.GAME_OVER
                await self._notify()
                return False

            # Advance to next declarer
            self.session.current_declarer_id = self.session.declarer_queue.pop(0)
            self.session.round_num += 1
            self.session.current_statements = None
            # Clear votes for all players
            for p in self.session.players:
                p.vote = None
            self.session.state = DetectiveState.DECLARING
            await self._notify()
            return True
