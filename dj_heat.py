"""DJ 串場熱度判斷 + 話題庫：聊天室熱烈時素材最多、但沒人在聽 DJ；
應該熱聊時少講（只報歌名），熱度降下來時把剛剛聊的話題拿出來延續（2026-09-30 使用者定）。

純函式 + 小狀態物件，無 IO。
"""
from __future__ import annotations

from dj_social_affinity import _ACTIVE_CHAT_WINDOW_S

# 2026-10-02：原本借用 social_affinity 的 4 句，群聊中位數 16 句/3 分鐘 → 75% 串場只報歌名。
# 20 句 ≈ 只有聊得最兇的三成時段才省話。
DJ_HOT_UTTERANCE_COUNT = 20

BANK_MAX_LINES = 8
BANK_MAX_AGE_S = 900.0
LINE_MAX_CHARS = 25


def _human_entries(entries, now: float, window_s: float) -> list:
    """ConversationBuffer.history 形狀的 entries → 篩出近 window_s 秒內的真人發言。"""
    if not isinstance(entries, list):
        return []
    out = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        speaker = e.get("speaker")
        if not speaker or str(speaker).startswith("Marvin"):
            continue
        text = e.get("text")
        if not text or not str(text).strip():
            continue
        ts = e.get("timestamp")
        if not isinstance(ts, (int, float)):
            continue
        if now - ts > window_s:
            continue
        out.append(e)
    return out


def is_hot(entries, n_online: int, now: float) -> bool:
    """播出前一刻判斷現場是否熱聊：至少 2 人在線，且近 3 分鐘真人發言達 DJ_HOT_UTTERANCE_COUNT 門檻。"""
    if n_online < 2:
        return False
    return len(_human_entries(entries, now, _ACTIVE_CHAT_WINDOW_S)) >= DJ_HOT_UTTERANCE_COUNT


class TopicBank:
    """熱聊時存一份聊天快照，等降溫時取出接回話題（一次性，取出即清空）。"""

    def __init__(self) -> None:
        self._lines: list[str] | None = None
        self._ts: float = 0.0

    def snapshot(self, entries, now: float) -> None:
        humans = _human_entries(entries, now, _ACTIVE_CHAT_WINDOW_S)
        if not humans:
            return  # 沒有可用句子時不覆蓋舊的
        tail = humans[-BANK_MAX_LINES:]
        self._lines = [
            f"{e['speaker']}：「{str(e['text']).strip()[:LINE_MAX_CHARS]}」"
            for e in tail
        ]
        self._ts = now

    def take(self, now: float) -> list[str]:
        if self._lines is None:
            return []
        lines, ts = self._lines, self._ts
        self._lines, self._ts = None, 0.0
        if now - ts > BANK_MAX_AGE_S:
            return []
        return lines
