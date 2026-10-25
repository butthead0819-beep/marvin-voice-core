"""DJ 串場熱度判斷 + 已用對話原句紀錄：聊天室熱烈時素材最多、但沒人在聽 DJ，
熱聊中這輪不講話直接 crossfade 進下一首（2026-10-11 使用者定：拔掉接回話題的
revival，改成熱聊時整段不講）。
2026-10-12：一個人講很多（例如講公事電話）也算需要安靜，不再要求 2 人以上才算熱。

純函式 + 小狀態物件，無 IO。
"""
from __future__ import annotations

import re

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


_CJK_RE = re.compile(r"[一-鿿]")


def usable_conv_text(text: str) -> bool:
    """這句 STT 能不能當 DJ 對話素材：濾掉網址、對 Marvin 下的指令、中文太少的糊字（如 "o see so don notet's come"）。
    只管 DJ 素材，熱度計數不用它。"""
    t = (text or "").strip()
    if re.search(r"https?://|www\.", t, re.I):
        return False
    if re.match(r"(馬文|瑪文|marvin)", t, re.I):
        return False
    compact = re.sub(r"\s", "", t)
    n_cjk = len(_CJK_RE.findall(compact))
    if n_cjk < 4 or n_cjk / max(len(compact), 1) < 0.5:
        return False
    return True


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


CROWDED_OTHERS_WINDOW_S = 60.0   # 看近 60 秒
CROWDED_OTHERS_MIN = 2           # 除了 target 以外至少這麼多位真人在講


def room_crowded(entries, n_online: int, target: str, now: float) -> bool:
    """多人熱聊：熱聊（is_hot）且近 60 秒除 target 外 ≥2 位真人講話。
    對談窗據此不接免喚醒句（2026-10-24 五人熱聊 Marvin 連回 9 句事故）。
    一對一熱聊（只有 target + 偶爾一人）不算。"""
    if not is_hot(entries, n_online, now):
        return False
    others = {e.get("speaker") for e in _human_entries(entries, now, CROWDED_OTHERS_WINDOW_S)
              if e.get("speaker") != target}
    return len(others) >= CROWDED_OTHERS_MIN


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
