"""DJ 串場熱度判斷 + 已用對話原句紀錄：聊天室熱烈時素材最多、但沒人在聽 DJ，
熱聊中這輪不講話直接 crossfade 進下一首（2026-10-11 使用者定：拔掉接回話題的
revival，改成熱聊時整段不講）。
2026-10-12：一個人講很多（例如講公事電話）也算需要安靜，不再要求 2 人以上才算熱。

純函式 + 小狀態物件，無 IO。
"""
from __future__ import annotations

from dj_social_affinity import _ACTIVE_CHAT_WINDOW_S

# 2026-10-02：原本借用 social_affinity 的 4 句，群聊中位數 16 句/3 分鐘 → 75% 串場只報歌名。
# 20 句 ≈ 只有聊得最兇的三成時段才省話。
DJ_HOT_UTTERANCE_COUNT = 20

# 一個人時只收得到單邊講話，門檻減半；2026-10-12 使用者定，待觀察 log 調整。
DJ_HOT_UTTERANCE_COUNT_SOLO = 10

# 已用過的原句紀錄保留秒數（避免 _consumed 無限長大）。
CONSUMED_MAX_AGE_S = 900.0


def utterance_key(e: dict) -> tuple:
    """原句的識別鍵：同一人同一時戳視為同一句。"""
    return (e.get("speaker"), e.get("timestamp"))


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


def heat_snapshot(entries, n_online: int, now: float) -> tuple[int, int, bool]:
    """回 (近 3 分鐘真人發言數, 套用的門檻, 是否熱聊)。n_online<=0 → 門檻 0、不熱。"""
    count = len(_human_entries(entries, now, _ACTIVE_CHAT_WINDOW_S))
    if n_online >= 2:
        threshold = DJ_HOT_UTTERANCE_COUNT
    elif n_online == 1:
        threshold = DJ_HOT_UTTERANCE_COUNT_SOLO
    else:
        return count, 0, False
    return count, threshold, count >= threshold


def is_hot(entries, n_online: int, now: float) -> bool:
    """播出前一刻判斷現場是否熱聊：近 3 分鐘真人發言達門檻（一人門檻 10、兩人以上門檻 20）。"""
    return heat_snapshot(entries, n_online, now)[2]


class TopicBank:
    """熱度判斷用的輔助狀態：記錄 conversation mode 已用過的對話原句，避免重複用同一句。"""

    def __init__(self) -> None:
        self._consumed: dict[tuple, float] = {}

    def mark_consumed(self, entries, now: float) -> None:
        for e in entries:
            if not isinstance(e, dict):
                continue
            ts = e.get("timestamp")
            if not isinstance(ts, (int, float)):
                continue
            self._consumed[utterance_key(e)] = ts
        for key, ts in list(self._consumed.items()):
            if now - ts > CONSUMED_MAX_AGE_S:
                del self._consumed[key]

    def is_consumed(self, e) -> bool:
        if not isinstance(e, dict):
            return False
        return utterance_key(e) in self._consumed
