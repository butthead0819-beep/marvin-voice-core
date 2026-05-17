"""海龜湯 session 狀態 — 純 dataclass，不含邏輯。"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum


class TurtleSoupState(Enum):
    IDLE = "idle"
    JOINING = "joining"
    PRESENTING = "presenting"   # Marvin 念湯面
    ASKING = "asking"           # 玩家自由問是非題
    GAME_OVER = "game_over"


class EndReason(Enum):
    WIN = "win"                 # 最終猜答正確
    SURRENDER = "surrender"     # 玩家投降
    EXHAUSTED = "exhausted"     # 50 題用完未猜中
    CANCELLED = "cancelled"     # /turtle_soup_stop


@dataclass
class TurtleSoupPlayer:
    user_id: str
    display_name: str


@dataclass
class AskedQuestion:
    """一次問答紀錄。"""
    asker_id: str
    asker_name: str
    question: str
    verdict: str    # yes / no / irrelevant
    narration: str
    provider: str   # Cerebras / Groq / Gemini / fallback
    timestamp: float


@dataclass
class TurtleSoupSession:
    session_id: str
    guild_id: int
    channel_id: int
    puzzle_id: str = ""
    players: list[TurtleSoupPlayer] = field(default_factory=list)
    state: TurtleSoupState = TurtleSoupState.IDLE
    asked_questions: list[AskedQuestion] = field(default_factory=list)
    game_message_id: int | None = None
    started_at: float = 0.0
    end_reason: EndReason | None = None
    end_narration: str = ""              # GAME_OVER 時 Marvin 公布湯底前的台詞
    max_questions: int = 50              # 硬上限
    hints_given: int = 0                 # 已給出的提示數（玩家主動 + idle timer）

    @property
    def questions_count(self) -> int:
        return len(self.asked_questions)

    @property
    def questions_remaining(self) -> int:
        return max(0, self.max_questions - self.questions_count)

    @property
    def recent_question_texts(self) -> list[str]:
        """最近 10 個問題的文字，給 LLM judge 當 history。"""
        return [q.question for q in self.asked_questions[-10:]]
