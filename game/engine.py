from __future__ import annotations

import asyncio
import json
import random
import sqlite3
import time
from typing import Any, Callable, Awaitable

from game.session import GameSession, GameState, PlayerState
from game import scoring

MAX_HUMAN_PLAYERS = 5
BUZZ_LOCK_SECONDS = 5.0           # how long the buzz window stays locked while holder answers
BUZZ_COOLDOWN_SECONDS = 30.0      # personal cooldown after a wrong buzz
SETTER_TIMEOUT_PENALTY = -50      # score penalty when setter fails to submit within time limit
ANSWER_MIN_LEN = 2
ANSWER_MAX_LEN = 5


class GameEngine:
    """
    Core state machine for the Busted game.

    All Discord UI is delegated via callbacks injected at construction —
    this class never imports discord.

    Parameters
    ----------
    session:
        The GameSession this engine manages.
    on_state_change:
        Async callable(session: GameSession) — called after every state transition.
    db_path:
        Path to the SQLite database file (default: "marvin.db").
    judge_fn:
        Optional async callable(answer: str, guess: str) -> bool — injected LLM judge.
        If None, falls back to a simple case-insensitive equality check.
    clue_fn:
        Optional async callable(session: GameSession) -> None — called to request the
        next clue to be generated and delivered.
    """

    def __init__(
        self,
        session: GameSession,
        *,
        on_state_change: Callable[[GameSession], Awaitable[None]],
        db_path: str = "marvin.db",
        judge_fn: Callable[[str, str], Awaitable[bool]] | None = None,
        clue_fn: Callable[[GameSession], Awaitable[None]] | None = None,
    ) -> None:
        self.session = session
        self._on_state_change = on_state_change
        self._db_path = db_path
        self._judge_fn = judge_fn
        self._clue_fn = clue_fn
        self._lock = asyncio.Lock()
        self._round5_scores: dict[str, int] = {}  # user_id → partial score for current round 5

        # Ensure DB tables exist (fire-and-forget during init; safe because DB
        # writes are also serialised through run_in_executor).
        asyncio.get_running_loop().run_in_executor(None, self._init_db)

    # ------------------------------------------------------------------
    # DB helpers (run in thread via run_in_executor)
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        con = sqlite3.connect(self._db_path)
        try:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS busted_sessions (
                    session_id TEXT PRIMARY KEY,
                    guild_id INTEGER NOT NULL,
                    started_at REAL NOT NULL,
                    ended_at REAL,
                    players_json TEXT NOT NULL,
                    final_scores_json TEXT
                );

                CREATE TABLE IF NOT EXISTS busted_rounds (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    round_num INTEGER NOT NULL,
                    setter_id TEXT NOT NULL,
                    setter_name TEXT NOT NULL,
                    answer TEXT NOT NULL,
                    clues_json TEXT NOT NULL,
                    winner_id TEXT,
                    winner_name TEXT,
                    won_at_round INTEGER,
                    setter_score INTEGER NOT NULL,
                    guesser_score INTEGER,
                    all_scores_json TEXT
                );
            """)
            con.commit()
        finally:
            con.close()

    def _write_round(
        self,
        session_id: str,
        round_num: int,
        setter_id: str,
        setter_name: str,
        answer: str,
        clues_json: str,
        winner_id: str | None,
        winner_name: str | None,
        won_at_round: int | None,
        setter_score: int,
        guesser_score_val: int | None,
        all_scores_json: str | None,
    ) -> None:
        con = sqlite3.connect(self._db_path)
        try:
            con.execute(
                """
                INSERT INTO busted_rounds (
                    session_id, round_num, setter_id, setter_name, answer,
                    clues_json, winner_id, winner_name, won_at_round,
                    setter_score, guesser_score, all_scores_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id, round_num, setter_id, setter_name, answer,
                    clues_json, winner_id, winner_name, won_at_round,
                    setter_score, guesser_score_val, all_scores_json,
                ),
            )
            con.commit()
        finally:
            con.close()

    def _write_session(
        self,
        session_id: str,
        guild_id: int,
        started_at: float,
        ended_at: float,
        players_json: str,
        final_scores_json: str | None,
    ) -> None:
        con = sqlite3.connect(self._db_path)
        try:
            con.execute(
                """
                INSERT OR REPLACE INTO busted_sessions (
                    session_id, guild_id, started_at, ended_at,
                    players_json, final_scores_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (session_id, guild_id, started_at, ended_at, players_json, final_scores_json),
            )
            con.commit()
        finally:
            con.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _notify(self) -> None:
        await self._on_state_change(self.session)

    def _get_player(self, user_id: str) -> PlayerState | None:
        for p in self.session.players:
            if p.user_id == user_id:
                return p
        return None

    def _setter_player(self) -> PlayerState | None:
        if self.session.current_setter_id is None:
            return None
        return self._get_player(self.session.current_setter_id)

    async def _judge_answer(self, answer: str, guess: str) -> bool:
        """Return True if guess is semantically correct."""
        if self._judge_fn is not None:
            return await self._judge_fn(answer, guess)
        # Fallback: case-insensitive equality
        return answer.strip().lower() == guess.strip().lower()

    def _next_setter(self) -> str | None:
        """Pop and return the next setter from the queue, or None if empty."""
        if not self.session.remaining_setters:
            return None
        setter_id = self.session.remaining_setters.pop(0)
        return setter_id

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def add_player(self, user_id: str, display_name: str) -> bool:
        """
        Add a player to the session during the JOINING phase.

        Returns False if the game is full (max 5 humans) or the player is
        already in the session.
        """
        async with self._lock:
            # Allow during IDLE or JOINING
            if self.session.state not in (GameState.IDLE, GameState.JOINING):
                return False
            human_players = [p for p in self.session.players if p.user_id != "marvin"]
            if len(human_players) >= MAX_HUMAN_PLAYERS:
                return False
            if self._get_player(user_id) is not None:
                return False
            self.session.players.append(PlayerState(user_id=user_id, display_name=display_name))
            self.session.remaining_setters.append(user_id)
            if self.session.state == GameState.IDLE:
                self.session.state = GameState.JOINING
            await self._notify()
            return True

    async def start_game(self) -> None:
        """
        Shuffle the setter queue, pick the first setter, and transition JOINING -> SPINNING.
        Sets started_at on the session.
        """
        async with self._lock:
            random.shuffle(self.session.remaining_setters)
            self.session.current_setter_id = self._next_setter()  # pop first setter
            self.session.started_at = time.time()
            self.session.state = GameState.SPINNING
            await self._notify()

    async def set_answer(self, answer: str) -> None:
        """
        Called when the current setter submits their secret answer.
        Transitions SETTER_INPUT -> CLUE_ACTIVE and triggers the first clue.
        """
        async with self._lock:
            self.session.current_answer = answer
            self.session.current_clues = []
            self.session.current_round = 1
            self.session.buzz_locked_until = 0.0
            self.session.buzz_holder_id = None
            self.session.wrong_guesses = []
            self._round5_scores.clear()
            self.session.state = GameState.CLUE_ACTIVE
            await self._notify()
            if self._clue_fn is not None:
                asyncio.get_running_loop().create_task(self._clue_fn(self.session))

    async def buzz_in(self, user_id: str) -> bool:
        """
        Attempt to buzz in.

        Returns False if:
        - global buzz is locked (buzz_locked_until in the future)
        - player has a personal cooldown active
        - player is the current setter

        On success transitions to BUZZ_LOCKED, sets buzz_holder_id.
        """
        async with self._lock:
            if self.session.state != GameState.CLUE_ACTIVE:
                return False
            if user_id == self.session.current_setter_id:
                return False
            now = time.time()
            if now < self.session.buzz_locked_until:
                return False
            player = self._get_player(user_id)
            if player is None:
                return False
            if now < player.buzz_cooldown_until:
                return False
            # Grant the buzz
            self.session.buzz_holder_id = user_id
            self.session.buzz_locked_until = now + BUZZ_LOCK_SECONDS
            self.session.state = GameState.BUZZ_LOCKED
            await self._notify()
            return True

    async def submit_answer(self, user_id: str, text: str) -> dict[str, Any]:
        """
        Validate a buzzer-holder's answer.

        Returns {"correct": bool, "score": int, "setter_score": int}.
        For rounds 1-4, uses the injected judge_fn (or fallback equality).
        Round 5 always uses partial_score (delegates to submit_round5_answer).
        """
        async with self._lock:
            if self.session.state != GameState.BUZZ_LOCKED:
                return {"correct": False, "score": 0, "setter_score": 0}
            if self.session.buzz_holder_id != user_id:
                return {"correct": False, "score": 0, "setter_score": 0}

            clue_round = self.session.current_round
            answer = self.session.current_answer or ""

            if clue_round >= 5:
                # Partial score path — release lock first so _partial_score can re-enter
                partial = scoring.partial_score(answer, text)
                player = self._get_player(user_id)
                if player:
                    player.score += partial
                self.session.state = GameState.CLUE_ACTIVE
                self.session.buzz_holder_id = None
                await self._notify()
                return {"correct": False, "score": partial, "setter_score": 0}

        # Release lock while awaiting LLM judge (may take time)
        correct = await self._judge_answer(answer, text)

        async with self._lock:
            # Re-validate state hasn't changed
            if self.session.state != GameState.BUZZ_LOCKED or self.session.buzz_holder_id != user_id:
                return {"correct": False, "score": 0, "setter_score": 0}

            clue_round = self.session.current_round
            answer = self.session.current_answer or ""
            guesser_pts = 0
            setter_pts = 0

            if correct:
                guesser_pts = scoring.guesser_score(clue_round)
                setter_pts = scoring.setter_score_if_guessed(clue_round)

                player = self._get_player(user_id)
                setter = self._setter_player()
                if player:
                    player.score += guesser_pts
                if setter:
                    setter.score += setter_pts

                self.session.state = GameState.ROUND_RESULT
                self.session.buzz_holder_id = None
                await self._notify()

                # Persist round asynchronously
                all_scores = {p.user_id: p.score for p in self.session.players}
                winner_name = player.display_name if player else user_id
                setter_name = setter.display_name if setter else (self.session.current_setter_id or "")
                asyncio.get_running_loop().run_in_executor(
                    None,
                    self._write_round,
                    self.session.session_id,
                    self.session.round_num,
                    self.session.current_setter_id or "",
                    setter_name,
                    answer,
                    json.dumps(self.session.current_clues),
                    user_id,
                    winner_name,
                    clue_round,
                    setter_pts,
                    guesser_pts,
                    json.dumps(all_scores),
                )
            else:
                # Wrong answer — apply personal cooldown and release buzz
                player = self._get_player(user_id)
                if player:
                    player.buzz_cooldown_until = time.time() + BUZZ_COOLDOWN_SECONDS
                if text and text not in self.session.wrong_guesses:
                    self.session.wrong_guesses.append(text)
                self.session.buzz_locked_until = 0.0
                self.session.buzz_holder_id = None
                self.session.state = GameState.CLUE_ACTIVE
                await self._notify()

            return {"correct": correct, "score": guesser_pts, "setter_score": setter_pts}

    async def submit_round5_answer(self, user_id: str, text: str) -> int:
        """
        Submit a final-round (round 5) partial-score answer from the modal.
        Returns the partial score earned by this player.
        Only valid when current_round == 5 and state == CLUE_ACTIVE.
        """
        async with self._lock:
            if self.session.state != GameState.CLUE_ACTIVE or self.session.current_round < 5:
                return 0
            if user_id == self.session.current_setter_id:
                return 0
            if user_id in self._round5_scores:
                return 0  # already submitted
            answer = self.session.current_answer or ""
            pts = scoring.partial_score(answer, text)
            self._round5_scores[user_id] = pts
            player = self._get_player(user_id)
            if player:
                player.score += pts
            await self._notify()
            return pts

    async def advance_clue(self) -> None:
        """
        Advance to the next clue. Called by the background timer task in game_cog every 15s.

        When current_round reaches 5, switches to final-round partial-scoring mode.
        When the timer fires again with current_round == 5 (i.e. round 5 window has closed),
        finalises the round: setter gets 100 if anyone scored, -100 if no one did.
        """
        async with self._lock:
            if self.session.state != GameState.CLUE_ACTIVE:
                return

            if self.session.current_round >= 5:
                # Round 5 window has expired — finalise the round
                setter = self._setter_player()
                any_scored = any(v > 0 for v in self._round5_scores.values())
                setter_pts = 100 if any_scored else scoring.setter_penalty()
                if setter:
                    setter.score += setter_pts
                self.session.state = GameState.ROUND_RESULT
                await self._notify()

                answer = self.session.current_answer or ""
                setter_name = setter.display_name if setter else (self.session.current_setter_id or "")
                all_scores = {p.user_id: p.score for p in self.session.players}
                asyncio.get_running_loop().run_in_executor(
                    None,
                    self._write_round,
                    self.session.session_id,
                    self.session.round_num,
                    self.session.current_setter_id or "",
                    setter_name,
                    answer,
                    json.dumps(self.session.current_clues),
                    None,
                    None,
                    None,
                    setter_pts,
                    None,
                    json.dumps(all_scores),
                )
                return

            self.session.current_round += 1
            await self._notify()
            if self._clue_fn is not None:
                asyncio.get_running_loop().create_task(self._clue_fn(self.session))

    async def begin_theme_select(self, themes: list[str]) -> None:
        """Present candidate themes from chat memory. Transitions SPINNING → THEME_SELECT."""
        async with self._lock:
            if self.session.state != GameState.SPINNING:
                return
            self.session.candidate_themes = list(themes)
            self.session.current_theme = None
            self.session.state = GameState.THEME_SELECT
            await self._notify()

    async def select_theme(self, theme: str) -> bool:
        """Record the setter's chosen theme. Transitions THEME_SELECT → SETTER_INPUT.
        Returns False if the theme is not in candidates or state is wrong."""
        async with self._lock:
            if self.session.state != GameState.THEME_SELECT:
                return False
            if theme not in self.session.candidate_themes:
                return False
            self.session.current_theme = theme
            self.session.state = GameState.SETTER_INPUT
            await self._notify()
            return True

    async def begin_setter_input(self) -> None:
        """Called by game_cog after the spinner animation completes (no theme phase)."""
        async with self._lock:
            if self.session.state != GameState.SPINNING:
                return
            self.session.state = GameState.SETTER_INPUT
            await self._notify()

    async def expire_buzz(self) -> None:
        """Called by game_cog when the answer window expires without a reply."""
        async with self._lock:
            if self.session.state != GameState.BUZZ_LOCKED:
                return
            holder = self._get_player(self.session.buzz_holder_id or "")
            if holder:
                holder.buzz_cooldown_until = time.time() + BUZZ_COOLDOWN_SECONDS
            self.session.buzz_locked_until = 0.0
            self.session.buzz_holder_id = None
            self.session.state = GameState.CLUE_ACTIVE
            await self._notify()

    async def next_round(self) -> bool:
        """
        Advance from ROUND_RESULT to the next setter (SPINNING) or end the game (GAME_OVER).
        Called by game_cog after showing the round result screen.
        Returns True if there are more rounds, False if the game has ended.
        """
        async with self._lock:
            if self.session.state != GameState.ROUND_RESULT:
                return False
            setter = self._get_player(self.session.current_setter_id or "")
            if setter:
                setter.has_been_setter = True
            # Reset per-round state
            self.session.current_answer = None
            self.session.current_clues = []
            self.session.current_round = 1
            self.session.buzz_holder_id = None
            self.session.buzz_locked_until = 0.0
            self.session.current_theme = None
            self.session.candidate_themes = []
            self._round5_scores.clear()

            next_setter = self._next_setter()
            if next_setter is None:
                # All players have been setter — game over
                final_scores = {p.user_id: p.score for p in self.session.players}
                asyncio.get_running_loop().run_in_executor(
                    None,
                    self._write_session,
                    self.session.session_id,
                    self.session.guild_id,
                    self.session.started_at,
                    time.time(),
                    json.dumps([{"user_id": p.user_id, "display_name": p.display_name, "score": p.score}
                                for p in self.session.players]),
                    json.dumps(final_scores),
                )
                self.session.state = GameState.GAME_OVER
                await self._notify()
                return False

            self.session.current_setter_id = next_setter
            self.session.round_num += 1
            self.session.state = GameState.SPINNING
            await self._notify()
            return True

    async def skip_setter_timeout(self) -> None:
        """
        Called when the setter fails to submit within the time limit.
        Applies SETTER_TIMEOUT_PENALTY, marks them as having been setter (no re-draw),
        and advances to the next setter (SPINNING) or ends the game (GAME_OVER).
        """
        async with self._lock:
            if self.session.state != GameState.SETTER_INPUT:
                return
            setter = self._setter_player()
            if setter:
                setter.score += SETTER_TIMEOUT_PENALTY
                setter.has_been_setter = True
            next_setter = self._next_setter()
            if next_setter is None:
                self.session.state = GameState.GAME_OVER
            else:
                self.session.current_setter_id = next_setter
                self.session.round_num += 1
                self.session.state = GameState.SPINNING
            await self._notify()

    async def receive_voice_answer(self, user_id: int, text: str) -> dict | bool:
        """
        Called by the STT pipeline.

        Returns False if the state is wrong or this user isn't the buzz holder.
        Returns the submit_answer result dict {"correct", "score", "setter_score"}
        if the text was consumed (regardless of whether the answer was correct).
        """
        str_id = str(user_id)
        async with self._lock:
            if self.session.state != GameState.BUZZ_LOCKED:
                return False
            if self.session.buzz_holder_id != str_id:
                return False

        return await self.submit_answer(str_id, text)

    async def remove_player(self, user_id: str) -> dict:
        """
        Remove a player from an in-progress game (e.g. voice-channel disconnect).

        Returns {"action": <str>}:
          "not_found"      — user was not in the game
          "game_over"      — last human left; GAME_OVER emitted via notify
          "expire_buzz"    — buzz holder left; released back to CLUE_ACTIVE via notify
          "setter_skipped" — setter left during SETTER_INPUT; auto-advanced via notify
          "removed"        — normal removal; caller must refresh embed (no notify emitted)
        """
        async with self._lock:
            player = self._get_player(user_id)
            if player is None:
                return {"action": "not_found"}

            self.session.players = [p for p in self.session.players if p.user_id != user_id]
            self.session.remaining_setters = [
                s for s in self.session.remaining_setters if s != user_id
            ]

            humans = [p for p in self.session.players if p.user_id != "marvin"]
            if not humans:
                self.session.state = GameState.GAME_OVER
                await self._notify()
                return {"action": "game_over"}

            if (
                self.session.state == GameState.BUZZ_LOCKED
                and self.session.buzz_holder_id == user_id
            ):
                self.session.buzz_locked_until = 0.0
                self.session.buzz_holder_id = None
                self.session.state = GameState.CLUE_ACTIVE
                await self._notify()
                return {"action": "expire_buzz"}

            if (
                self.session.state == GameState.SETTER_INPUT
                and self.session.current_setter_id == user_id
            ):
                next_setter = self._next_setter()
                if next_setter is None:
                    self.session.state = GameState.GAME_OVER
                    await self._notify()
                    return {"action": "game_over"}
                self.session.current_setter_id = next_setter
                self.session.round_num += 1
                self.session.state = GameState.SPINNING
                await self._notify()
                return {"action": "setter_skipped"}

            # Normal removal — no notify to avoid clue-loop side-effects; caller refreshes embed
            return {"action": "removed"}

    async def add_player_midgame(self, user_id: str, display_name: str) -> bool:
        """
        Add a latecomer during an active game (SETTER_INPUT, CLUE_ACTIVE, ROUND_RESULT).
        They join with score 0 and get a setter turn appended to the end of the queue.
        Returns False if the game is full, player is already in, or state is wrong.
        No notify emitted — caller refreshes the embed to avoid loop side-effects.
        """
        async with self._lock:
            if self.session.state in (
                GameState.IDLE, GameState.JOINING, GameState.SPINNING,
                GameState.BUZZ_LOCKED, GameState.GAME_OVER,
            ):
                return False
            human_players = [p for p in self.session.players if p.user_id != "marvin"]
            if len(human_players) >= MAX_HUMAN_PLAYERS:
                return False
            if self._get_player(user_id) is not None:
                return False
            self.session.players.append(PlayerState(user_id=user_id, display_name=display_name))
            self.session.remaining_setters.append(user_id)
            return True

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    async def _persist_round(
        self,
        winner_id: str | None,
        winner_name: str | None,
        won_at_round: int | None,
        setter_score: int,
        guesser_score_val: int | None,
        all_scores: dict[str, int] | None = None,
    ) -> None:
        """Write the current round result to busted_rounds."""
        session = self.session
        setter = self._setter_player()
        setter_name = setter.display_name if setter else (session.current_setter_id or "")
        all_scores_json = json.dumps(all_scores) if all_scores is not None else None

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            self._write_round,
            session.session_id,
            session.round_num,
            session.current_setter_id or "",
            setter_name,
            session.current_answer or "",
            json.dumps(session.current_clues),
            winner_id,
            winner_name,
            won_at_round,
            setter_score,
            guesser_score_val,
            all_scores_json,
        )

    async def _persist_session(self) -> None:
        """Write the finished session to busted_sessions."""
        session = self.session
        players_json = json.dumps(
            [{"user_id": p.user_id, "display_name": p.display_name, "score": p.score}
             for p in session.players]
        )
        final_scores_json = json.dumps({p.user_id: p.score for p in session.players})
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            self._write_session,
            session.session_id,
            session.guild_id,
            session.started_at,
            time.time(),
            players_json,
            final_scores_json,
        )
        session.state = GameState.GAME_OVER
        await self._notify()
