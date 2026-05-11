from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class GameState(Enum):
    IDLE = "idle"
    JOINING = "joining"
    SPINNING = "spinning"
    SETTER_INPUT = "setter_input"
    CLUE_ACTIVE = "clue_active"
    BUZZ_LOCKED = "buzz_locked"
    ROUND_RESULT = "round_result"
    GAME_OVER = "game_over"


@dataclass
class PlayerState:
    user_id: str                   # "marvin" for Marvin
    display_name: str
    score: int = 0
    buzz_cooldown_until: float = 0.0  # time.time() timestamp
    has_been_setter: bool = False


@dataclass
class GameSession:
    session_id: str
    guild_id: int
    channel_id: int
    players: list[PlayerState] = field(default_factory=list)          # in join order
    remaining_setters: list[str] = field(default_factory=list)        # user_id queue, popped each round
    current_setter_id: str | None = None
    state: GameState = GameState.IDLE
    current_round: int = 1             # 1-5 clue index within a round
    current_answer: str | None = None
    current_clues: list[str] = field(default_factory=list)
    buzz_locked_until: float = 0.0     # global buzz lock timestamp
    buzz_holder_id: str | None = None  # who pressed buzz
    round_num: int = 1                 # which round of the game (1-indexed)
    game_message_id: int | None = None # Discord message ID for main embed
    started_at: float = 0.0
    wrong_guesses: list[str] = field(default_factory=list)  # all wrong buzz answers this setter turn
