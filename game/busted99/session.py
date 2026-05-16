from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum


class Busted99State(Enum):
    IDLE = "idle"
    JOINING = "joining"
    SETTER_PICKING = "setter_picking"  # 出題人選數字
    GUESSING = "guessing"              # 當前猜題人在答題（15s window）
    GAME_OVER = "game_over"


@dataclass
class Player99State:
    user_id: str           # "marvin" for Marvin
    display_name: str
    score: int = 0


@dataclass
class Busted99Session:
    session_id: str
    guild_id: int
    channel_id: int
    players: list[Player99State] = field(default_factory=list)
    state: Busted99State = Busted99State.IDLE
    setter_id: str | None = None
    answer: int | None = None          # 秘密數字 1-99
    low_bound: int = 1
    high_bound: int = 99
    current_guesser_id: str | None = None
    guesser_order: list[str] = field(default_factory=list)   # 抽完 setter 後固定的猜題順序
    guessing_queue: list[str] = field(default_factory=list)  # 本輪待猜玩家 user_id
    round_num: int = 1                  # 第幾輪（所有人輪過一次算一輪）
    game_message_id: int | None = None
    started_at: float = 0.0
    last_guess: int | None = None
    last_guess_result: str | None = None  # 'bust','wrong_high','wrong_low','timeout','last_wrong','last_bust'
    guess_log: list[dict] = field(default_factory=list)  # [{guesser, guess, result, low, high, round}]
