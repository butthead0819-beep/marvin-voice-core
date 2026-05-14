from __future__ import annotations

import asyncio
import json
import random
import re
import sqlite3
import time
from typing import Any, Callable, Awaitable

from game.busted99.session import Busted99Session, Busted99State, Player99State
from game.busted99.scoring import score_for_space
from game.player_score_db import add_scores, init_table as init_scores_table
from game.game_memory_db import init_table as init_memory_table, write_event

MAX_HUMAN_PLAYERS = 5
GUESS_TIMEOUT_SECONDS = 15.0

# Chinese number parsing
_CHINESE_DIGITS = {
    "零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CHINESE_TENS = {
    "十": 10, "二十": 20, "三十": 30, "四十": 40, "五十": 50,
    "六十": 60, "七十": 70, "八十": 80, "九十": 90,
}


def parse_number(text: str) -> int | None:
    """
    Parse a number from text (Arabic or Chinese).

    Supports:
    - Arabic digits: "42", "99", "1", "我猜 57"
    - Chinese numbers: "四十二", "七", "十二", "九十九"

    Returns None if:
    - Cannot parse a number
    - Number is out of range (< 1 or > 99)
    """
    text = text.strip()
    if not text:
        return None

    # Try Arabic digits first
    arabic_match = re.search(r"\d+", text)
    if arabic_match:
        n = int(arabic_match.group())
        if 1 <= n <= 99:
            return n
        return None

    # Try Chinese number parsing — only if text looks like Chinese numerals
    # Avoid matching partial Chinese words that aren't numbers (e.g., "一百" = 100)
    n = _parse_chinese_number(text)
    if n is not None:
        if 1 <= n <= 99:
            return n
        return None

    return None


def _parse_chinese_number(text: str) -> int | None:
    """Parse Chinese number string to integer."""
    text = text.strip()
    if not text:
        return None

    # If text contains higher-magnitude characters (百/千/萬…), the number
    # is at least 100 and therefore out of the 1-99 range — return None so
    # the caller's range check handles it correctly.
    if any(c in text for c in "百千萬億"):
        return None

    # Strip any non-Chinese-digit content, find Chinese numeral patterns
    # Pattern: optional tens prefix + 十 + optional units
    # e.g., 四十二 = 4*10 + 2 = 42, 十二 = 10 + 2 = 12, 七 = 7

    # Direct single digit
    if len(text) == 1 and text in _CHINESE_DIGITS:
        return _CHINESE_DIGITS[text]

    # Try to find a Chinese number pattern in the text
    # Match patterns like: [digit]十[digit], 十[digit], [digit]十, [digit]
    pattern = re.search(r"([一二三四五六七八九])?十([一二三四五六七八九])?|[一二三四五六七八九]", text)
    if not pattern:
        # Try 零 for zero explicitly
        if "零" in text:
            return 0
        return None

    matched = pattern.group(0)

    # Check if matched is a single digit
    if matched in _CHINESE_DIGITS:
        return _CHINESE_DIGITS[matched]

    # Parse tens pattern
    if "十" in matched:
        tens_char = pattern.group(1)
        units_char = pattern.group(2)
        tens = _CHINESE_DIGITS.get(tens_char, 1) * 10  # 十 alone = 10
        units = _CHINESE_DIGITS.get(units_char, 0)
        return tens + units

    return None


class Busted99Engine:
    """
    Core state machine for the Busted99 game.

    All Discord UI is delegated via callbacks injected at construction —
    this class never imports discord.

    Parameters
    ----------
    session:
        The Busted99Session this engine manages.
    on_state_change:
        Async callable(session: Busted99Session) — called after every state transition.
    db_path:
        Path to the SQLite database file (default: "marvin.db").
    """

    def __init__(
        self,
        session: Busted99Session,
        *,
        on_state_change: Callable[[Busted99Session], Awaitable[None]],
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
            # No running loop (e.g., in sync context)
            self._init_db()

    # ------------------------------------------------------------------
    # DB helpers (run in thread via run_in_executor)
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        con = sqlite3.connect(self._db_path)
        try:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS busted99_sessions (
                    session_id TEXT PRIMARY KEY,
                    guild_id INTEGER NOT NULL,
                    started_at REAL NOT NULL,
                    ended_at REAL,
                    players_json TEXT NOT NULL,
                    answer INTEGER,
                    final_scores_json TEXT
                );

                CREATE TABLE IF NOT EXISTS busted99_guesses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    round_num INTEGER NOT NULL,
                    guesser_id TEXT NOT NULL,
                    guesser_name TEXT NOT NULL,
                    guess INTEGER,
                    result TEXT NOT NULL,
                    low_before INTEGER NOT NULL,
                    high_before INTEGER NOT NULL,
                    score_change INTEGER NOT NULL DEFAULT 0,
                    all_scores_json TEXT
                );
            """)
            init_scores_table(con)
            init_memory_table(con)
            con.commit()
        finally:
            con.close()

    def _save_guess(
        self,
        session_id: str,
        round_num: int,
        guesser_id: str,
        guesser_name: str,
        guess: int | None,
        result: str,
        low_before: int,
        high_before: int,
        score_change: int,
    ) -> None:
        con = sqlite3.connect(self._db_path)
        try:
            con.execute(
                """
                INSERT INTO busted99_guesses
                    (session_id, round_num, guesser_id, guesser_name, guess, result,
                     low_before, high_before, score_change, all_scores_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id, round_num, guesser_id, guesser_name, guess, result,
                    low_before, high_before, score_change,
                    json.dumps({p.display_name: p.score for p in self.session.players}),
                ),
            )
            con.commit()
        finally:
            con.close()

    def _save_session_end(self) -> None:
        con = sqlite3.connect(self._db_path)
        try:
            final_scores = {p.display_name: p.score for p in self.session.players}
            con.execute(
                """
                INSERT OR REPLACE INTO busted99_sessions
                    (session_id, guild_id, started_at, ended_at, players_json, answer, final_scores_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.session.session_id,
                    self.session.guild_id,
                    self.session.started_at,
                    time.time(),
                    json.dumps([{"id": p.user_id, "name": p.display_name} for p in self.session.players]),
                    self.session.answer,
                    json.dumps(final_scores),
                ),
            )
            add_scores(con, [(p.user_id, p.display_name, p.score) for p in self.session.players])
            sorted_players = sorted(self.session.players, key=lambda p: p.score, reverse=True)
            scores_text = "、".join(
                f"{p.display_name} {p.score}分" for p in sorted_players if p.score > 0
            ) or "無人得分"
            result_label = self.session.last_guess_result or "結束"
            write_event(con, f"【終極密碼 結算】答案 {self.session.answer}，{result_label}。{scores_text}")
            con.commit()
        finally:
            con.close()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def add_player(self, user_id: str, display_name: str) -> bool:
        """
        Add a player to the session.

        Returns True if added successfully, False if:
        - Game is not in IDLE or JOINING state
        - Player already joined
        - Too many human players (> MAX_HUMAN_PLAYERS, excluding Marvin)
        """
        async with self._lock:
            state = self.session.state
            if state not in (Busted99State.IDLE, Busted99State.JOINING):
                return False

            # Check duplicate
            if any(p.user_id == user_id for p in self.session.players):
                return False

            # Count human players (not Marvin)
            human_count = sum(
                1 for p in self.session.players if p.user_id != "marvin"
            )
            if user_id != "marvin" and human_count >= MAX_HUMAN_PLAYERS:
                return False

            self.session.players.append(Player99State(user_id=user_id, display_name=display_name))
            self.session.state = Busted99State.JOINING
            return True

    async def start_game(self) -> None:
        """
        Start the game: JOINING → SETTER_PICKING.
        Randomly selects a setter from all players.
        """
        async with self._lock:
            if not self.session.players:
                return
            self.session.setter_id = random.choice(self.session.players).user_id
            self.session.started_at = time.time()
            self.session.state = Busted99State.SETTER_PICKING

        await self._on_state_change(self.session)

    async def set_answer(self, setter_id: str, number: int) -> bool:
        """
        Setter sets the secret number.

        Returns True if successful, False if:
        - Not in SETTER_PICKING state
        - setter_id doesn't match session.setter_id
        - number is out of range (not 1-99)
        """
        async with self._lock:
            if self.session.state != Busted99State.SETTER_PICKING:
                return False
            if self.session.setter_id != setter_id:
                return False
            if not (1 <= number <= 99):
                return False

            self.session.answer = number
            self.session.low_bound = 1
            self.session.high_bound = 99

            # Build guessing queue: all non-setter players, randomly ordered
            non_setters = [p.user_id for p in self.session.players if p.user_id != setter_id]
            random.shuffle(non_setters)
            self.session.round_num = 1

            if not non_setters:
                # 沒有猜題人（例如只剩 Marvin 一人且他是 setter）
                # 直接結束遊戲，避免無限 timeout loop
                self.session.current_guesser_id = None
                self.session.guessing_queue = []
                self.session.state = Busted99State.GAME_OVER
            else:
                self.session.guessing_queue = non_setters[1:]  # rest of queue
                self.session.current_guesser_id = non_setters[0]
                self.session.state = Busted99State.GUESSING

        await self._on_state_change(self.session)
        return True

    async def submit_guess(self, guesser_id: str, number: int) -> dict[str, Any]:
        """
        Submit a guess.

        Returns dict with keys:
        - result: "bust" | "wrong_low" | "wrong_high" | "out_of_range" | "boundary" | "last_bust" | "last_wrong" | "invalid_state"
        - score_change: int
        - new_low: int
        - new_high: int
        - space: int
        """
        async with self._lock:
            if self.session.state != Busted99State.GUESSING:
                return {"result": "invalid_state", "score_change": 0, "new_low": self.session.low_bound, "new_high": self.session.high_bound, "space": self.session.high_bound - self.session.low_bound + 1}
            if self.session.current_guesser_id != guesser_id:
                return {"result": "invalid_guesser", "score_change": 0, "new_low": self.session.low_bound, "new_high": self.session.high_bound, "space": self.session.high_bound - self.session.low_bound + 1}

            low = self.session.low_bound
            high = self.session.high_bound
            space = high - low + 1

            # Out of range check
            if not (low <= number <= high):
                return {"result": "out_of_range", "score_change": 0, "new_low": low, "new_high": high, "space": space}

            # 終極密碼：space > 2 時禁猜邊界（低/高限），不消耗回合
            if space > 2 and (number == low or number == high):
                return {"result": "boundary", "score_change": 0, "new_low": low, "new_high": high, "space": space}

            low_before = low
            high_before = high
            answer = self.session.answer
            assert answer is not None

            self.session.last_guess = number

            # Check if we're in last-chance mode (space ≤ 2)
            is_last_chance = space <= 2

            if number == answer:
                # Correct guess!
                if is_last_chance:
                    result_str = "last_bust"
                    # Setter gets 100, others get score_for_space(space), guesser gets 0
                    for p in self.session.players:
                        if p.user_id == self.session.setter_id:
                            p.score += 100
                        elif p.user_id != guesser_id:
                            p.score += score_for_space(space)
                        # guesser gets 0 (no change)
                    score_change = 0
                else:
                    result_str = "bust"
                    pts = score_for_space(space)
                    for p in self.session.players:
                        if p.user_id != guesser_id:
                            p.score += pts
                        # guesser gets 0 (busted)
                    score_change = pts

                self.session.last_guess_result = result_str
                self.session.state = Busted99State.GAME_OVER

                loop = asyncio.get_running_loop()
                loop.run_in_executor(None, self._save_session_end)
                loop.run_in_executor(
                    None, self._save_guess,
                    self.session.session_id, self.session.round_num,
                    guesser_id,
                    next(p.display_name for p in self.session.players if p.user_id == guesser_id),
                    number, result_str, low_before, high_before, 0,
                )

                result = {
                    "result": result_str,
                    "score_change": score_change,
                    "new_low": low,
                    "new_high": high,
                    "space": space,
                }

            elif number < answer:
                # Too low
                if is_last_chance:
                    result_str = "last_wrong"
                    # Guesser gets 100, game over
                    guesser = next(p for p in self.session.players if p.user_id == guesser_id)
                    guesser.score += 100
                    self.session.state = Busted99State.GAME_OVER
                    self.session.last_guess_result = result_str

                    loop = asyncio.get_running_loop()
                    loop.run_in_executor(None, self._save_session_end)
                    loop.run_in_executor(
                        None, self._save_guess,
                        self.session.session_id, self.session.round_num,
                        guesser_id,
                        next(p.display_name for p in self.session.players if p.user_id == guesser_id),
                        number, result_str, low_before, high_before, 100,
                    )

                    result = {
                        "result": result_str,
                        "score_change": 100,
                        "new_low": low,
                        "new_high": high,
                        "space": space,
                    }
                else:
                    result_str = "wrong_low"
                    self.session.low_bound = number + 1
                    self.session.last_guess_result = result_str

                    loop = asyncio.get_running_loop()
                    loop.run_in_executor(
                        None, self._save_guess,
                        self.session.session_id, self.session.round_num,
                        guesser_id,
                        next(p.display_name for p in self.session.players if p.user_id == guesser_id),
                        number, result_str, low_before, high_before, 0,
                    )

                    self._advance_guesser()
                    result = {
                        "result": result_str,
                        "score_change": 0,
                        "new_low": self.session.low_bound,
                        "new_high": self.session.high_bound,
                        "space": self.session.high_bound - self.session.low_bound + 1,
                    }

            else:
                # Too high
                if is_last_chance:
                    result_str = "last_wrong"
                    # Guesser gets 100, game over
                    guesser = next(p for p in self.session.players if p.user_id == guesser_id)
                    guesser.score += 100
                    self.session.state = Busted99State.GAME_OVER
                    self.session.last_guess_result = result_str

                    loop = asyncio.get_running_loop()
                    loop.run_in_executor(None, self._save_session_end)
                    loop.run_in_executor(
                        None, self._save_guess,
                        self.session.session_id, self.session.round_num,
                        guesser_id,
                        next(p.display_name for p in self.session.players if p.user_id == guesser_id),
                        number, result_str, low_before, high_before, 100,
                    )

                    result = {
                        "result": result_str,
                        "score_change": 100,
                        "new_low": low,
                        "new_high": high,
                        "space": space,
                    }
                else:
                    result_str = "wrong_high"
                    self.session.high_bound = number - 1
                    self.session.last_guess_result = result_str

                    loop = asyncio.get_running_loop()
                    loop.run_in_executor(
                        None, self._save_guess,
                        self.session.session_id, self.session.round_num,
                        guesser_id,
                        next(p.display_name for p in self.session.players if p.user_id == guesser_id),
                        number, result_str, low_before, high_before, 0,
                    )

                    self._advance_guesser()
                    result = {
                        "result": result_str,
                        "score_change": 0,
                        "new_low": self.session.low_bound,
                        "new_high": self.session.high_bound,
                        "space": self.session.high_bound - self.session.low_bound + 1,
                    }

        await self._on_state_change(self.session)
        return result

    async def timeout_guesser(self) -> dict[str, Any]:
        """
        Handle timeout for current guesser.

        Deducts score_for_space(current_space) from guesser.
        Advances to next guesser without narrowing range.

        Returns {"deducted": int, "next_guesser_id": str | None}
        """
        async with self._lock:
            if self.session.state != Busted99State.GUESSING:
                return {"deducted": 0, "next_guesser_id": None}

            guesser_id = self.session.current_guesser_id
            space = self.session.high_bound - self.session.low_bound + 1
            deduction = score_for_space(space)

            # Deduct score
            guesser = next((p for p in self.session.players if p.user_id == guesser_id), None)
            if guesser:
                guesser.score = max(0, guesser.score - deduction)

            self.session.last_guess_result = "timeout"

            timed_out_name = next(
                (p.display_name for p in self.session.players if p.user_id == guesser_id), "unknown"
            )
            loop = asyncio.get_running_loop()
            loop.run_in_executor(
                None, self._save_guess,
                self.session.session_id, self.session.round_num,
                guesser_id or "unknown",
                timed_out_name,
                None, "timeout",
                self.session.low_bound, self.session.high_bound,
                -deduction,
            )

            self._advance_guesser()
            next_guesser = self.session.current_guesser_id

        await self._on_state_change(self.session)
        return {
            "deducted": deduction,
            "next_guesser_id": next_guesser,
            "timed_out_guesser_id": guesser_id,
            "timed_out_name": timed_out_name,
        }

    async def receive_voice_guess(self, user_id: int, text: str) -> dict[str, Any] | None:
        """
        Process a voice guess from user.

        Returns None if:
        - Not in GUESSING state
        - user_id is not current guesser
        - Text cannot be parsed as a number

        Otherwise returns submit_guess result.
        """
        if self.session.state != Busted99State.GUESSING:
            return None

        user_id_str = str(user_id)
        if self.session.current_guesser_id != user_id_str:
            return None

        number = parse_number(text)
        if number is None:
            return None

        return await self.submit_guess(user_id_str, number)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _advance_guesser(self) -> None:
        """
        Pop the next guesser from guessing_queue.
        If queue is empty, rebuild it (new round) and pop again.
        """
        if self.session.guessing_queue:
            self.session.current_guesser_id = self.session.guessing_queue.pop(0)
        else:
            # All guessers used — start new round
            non_setters = [p.user_id for p in self.session.players if p.user_id != self.session.setter_id]
            random.shuffle(non_setters)
            self.session.round_num += 1
            if non_setters:
                self.session.current_guesser_id = non_setters[0]
                self.session.guessing_queue = non_setters[1:]
            else:
                self.session.current_guesser_id = None
                self.session.guessing_queue = []
