from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class DetectiveState(Enum):
    IDLE       = "idle"
    JOINING    = "joining"
    DECLARING  = "declaring"   # 陳述者輸入三句話
    VOTING     = "voting"      # 投票階段（40 秒）
    REVEALING  = "revealing"   # 揭曉結果（10 秒後自動推進）
    GAME_OVER  = "game_over"


@dataclass
class StatementSet:
    a: str
    b: str
    c: str
    lie_index: int   # 0=A / 1=B / 2=C


@dataclass
class PlayerDState:
    user_id: str
    display_name: str
    score: int = 0
    has_declared: bool = False
    vote: int | None = None   # 0=A/1=B/2=C, None=未投


@dataclass
class DetectiveSession:
    session_id: str
    guild_id: int
    channel_id: int
    players: list[PlayerDState] = field(default_factory=list)
    state: DetectiveState = field(default=DetectiveState.IDLE)
    declarer_queue: list[str] = field(default_factory=list)   # 還沒當過陳述者的 user_id
    current_declarer_id: str | None = None
    current_statements: StatementSet | None = None
    game_message_id: int | None = None
    round_num: int = 1
    started_at: float = 0.0
