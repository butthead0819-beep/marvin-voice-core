"""
WakeSession —— 喚醒後「只對發起者」的免喚醒回合對話核心狀態機。

純核心、零 IO：不呼叫 time.time()，`now` 一律由呼叫端傳入，方便測試用數字手動推進。
接線邏輯（mixer/TTS/intent bus）在 cogs/voice_controller_wake_session.py。
"""
from __future__ import annotations

import logging
import re

IDLE_TIMEOUT_S = 120.0
MAX_UNANSWERED = 3
MAX_FOLLOWUP_MISSES = 3
AWAIT_REPLY_TIMEOUT_S = 30.0
MAX_QA_TURNS = 3

logger = logging.getLogger("wake_session")

# 結束語（fullmatch——只認整句就是結束語，避免「謝謝你上次推薦的歌那首叫什麼」被吃掉）。
# 允許串接（「沒事了謝謝」「好了謝啦」，2026-10-15 事故）。
# 容忍語助詞穿插（2026-10-17 STT 把「OK」聽成「嗯奧克嗯 OK」沒認到）。
_CLOSING_RE = re.compile(
    r"[\s,，、嗯恩啊阿呃欸誒唉哎]*(?:馬文|瑪文)?[\s,，、嗯恩啊阿呃欸誒唉哎]*"
    r"(?:"
    r"(?:謝謝|謝啦|感謝|多謝|3q|thank\s*you|thanks|知道了|了解|瞭解|明白了|懂了|好了|沒事了|沒事|沒問題了"
    r"|不用了|先這樣|就這樣|可以了|夠了|結束|停|okay|OK|ok|奧克|歐克|好喔|好哦)"
    r"(?:了|啦|喔|哦|囉|耶|你|妳|馬文|瑪文|嗯|恩|啊|阿|呃|欸|[\s,，。.!！~～])*"
    r")+",
    re.IGNORECASE,
)


def is_closing_phrase(text: str) -> bool:
    return bool(text) and _CLOSING_RE.fullmatch(text.strip()) is not None


class WakeSession:
    def __init__(self):
        self.target: str | None = None
        self.opened: bool = False          # 是否已收到第一次回應（False=剛 arm，還在等 Marvin 回第一句）
        self.armed_at: float = 0.0
        self.last_speech: float = 0.0
        self.unanswered: int = 0
        self.awaiting_since: float | None = None   # 最近一次「接住的句子」還沒得到回應的起點
        self.query_mode: bool = False      # session 內是否已有過 grounded 查詢 → 非音樂追問黏著 grounded
        self.followup_misses: int = 0
        self.qa_turns: list[tuple[str, str]] = []

    def _end(self, reason: str) -> None:
        logger.info(f"🗣️ [WakeSession] 結束 target={self.target} reason={reason}")
        self.target = None
        self.opened = False
        self.awaiting_since = None
        self.unanswered = 0
        self.query_mode = False
        self.followup_misses = 0
        self.qa_turns = []

    def on_wake_accepted(self, speaker: str, now: float) -> None:
        if self.target == speaker:
            return
        self.target = speaker
        self.opened = False
        self.armed_at = now
        self.last_speech = now
        self.unanswered = 0
        self.awaiting_since = now
        self.query_mode = False
        self.followup_misses = 0
        self.qa_turns = []
        logger.info(f"🗣️ [WakeSession] arm target={speaker}")

    def on_replied(self, now: float) -> bool:
        if self.target is None:
            return False
        if not self.opened and now - self.armed_at > AWAIT_REPLY_TIMEOUT_S:
            self._end("stale_arm")
            return False
        if self.opened and now - self.last_speech > IDLE_TIMEOUT_S:
            self._end("idle")
            return False
        was_waiting = (not self.opened) or (self.awaiting_since is not None)
        self.opened = True
        self.awaiting_since = None
        self.unanswered = 0
        self.last_speech = max(self.last_speech, now)
        if was_waiting:
            logger.info(f"🗣️ [WakeSession] 聆聽中 target={self.target}")
        return was_waiting

    def on_ignored(self, speaker: str, now: float) -> None:
        if speaker != self.target:
            return
        if not self.opened:
            self._end("first_reply_rejected")
            return
        self.awaiting_since = None
        self.unanswered += 1
        if self.unanswered >= MAX_UNANSWERED:
            self._end("ignored")

    def on_utterance(self, speaker: str, now: float, *, marvin_speaking: bool, text: str = "") -> bool:
        if self.target is None or speaker != self.target:
            return False
        if not self.opened:
            return False
        if is_closing_phrase(text):
            self._end("closing")
            return False
        if now - self.last_speech > IDLE_TIMEOUT_S:
            self._end("idle")
            return False
        self.last_speech = now
        if marvin_speaking:
            return False
        if self.awaiting_since is not None:
            if now - self.awaiting_since < AWAIT_REPLY_TIMEOUT_S:
                return False   # 上一句還在處理
            self.unanswered += 1                                                 # 上一句逾時沒回應
            self.awaiting_since = None
            if self.unanswered >= MAX_UNANSWERED:
                self._end("ignored")
                return False
        self.awaiting_since = now
        logger.info(f"🗣️ [WakeSession] 接住 {speaker} 免喚醒句（unanswered={self.unanswered}）")
        return True

    def on_music_action(self, now: float) -> None:
        """討論轉成點播/下一首/重播被執行 → 歌曲對話以指令收尾，session 結束。"""
        if self.target is None:
            return
        self._end("music_action")

    def note_grounded(self, speaker: str) -> None:
        """session 內對 target 做過一次 grounded 查詢 → 之後非音樂追問黏著 grounded。"""
        if speaker == self.target:
            self.query_mode = True

    def wants_grounded_followup(self, speaker: str) -> bool:
        return self.target is not None and speaker == self.target and self.opened and self.query_mode

    def on_followup_result(self, speaker: str, hit: bool) -> None:
        """追問黏著（session_followup）的 grounded 查詢結果：查到歸零；連續查不到
        MAX_FOLLOWUP_MISSES 次 → 結束。「查不到」仍會走 on_replied（不動那條），
        所以必須獨立計數，不能用 unanswered。"""
        if self.target is None or speaker != self.target:
            return
        if hit:
            self.followup_misses = 0
            return
        self.followup_misses += 1
        if self.followup_misses >= MAX_FOLLOWUP_MISSES:
            self._end("followup_misses")

    def note_qa_turn(self, speaker: str, question: str, answer: str) -> None:
        """session 內 target 的一輪查詢問答（含 Marvin 自己的回答）→ 下一輪查詢當上下文。只留最近 MAX_QA_TURNS 輪。"""
        if self.target is None or speaker != self.target:
            return
        self.qa_turns.append((question, answer))
        self.qa_turns = self.qa_turns[-MAX_QA_TURNS:]

    def qa_history(self, speaker: str) -> list[tuple[str, str]]:
        if self.target is None or speaker != self.target:
            return []
        return list(self.qa_turns)
