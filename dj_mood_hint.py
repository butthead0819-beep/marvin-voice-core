"""DJ 口白讀空氣：日記「氣氛」行 → 只給 LLM 調說話方式的內心提示 + 防露餡檢查。

使用者 2026-10-18 定：Marvin 讀得到氣氛，但不能講出來、不能暗示知道——像人類一樣
不露餡地體會他人情緒。表現只透過語助詞、口語程度、句子長短、要不要鬧。
氣氛行格式（ambient_diary prompt）：「基調｜情緒事件」，事件選填。
"""
from __future__ import annotations

import re
import time

MOOD_MAX_AGE_S = 25 * 60   # 日記每 10 分鐘一筆；超過 25 分鐘的氣氛視為過期
LEAK_NGRAM = 4             # 口白與氣氛提示有 ≥4 字連續相同 → 視為講破
# 口白與氣氛提示「都」出現這些詞 → 視為講破（2 字詞抓 4-gram 抓不到的情緒字眼）
EMOTION_WORDS = ("不爽", "生氣", "火大", "焦慮", "緊張", "難過", "傷心", "低落", "沮喪",
                 "煩躁", "鬱悶", "委屈", "抱怨", "崩潰", "無奈", "失落", "疲憊", "疲累",
                 "心情", "情緒", "氣氛")
MOOD_HINT_HEADER = "【現場氣氛——只給你調整說話方式用，口白裡絕對不准提到、轉述或暗示你知道這些】"
MOOD_HINT_INSTRUCTION = "依這個氣氛調整語助詞多寡、口語程度、句子長短、要不要跟著鬧；有人情緒不好時，不拿他的事開玩笑。"


def latest_mood(entries, now: float, max_age_s: float = MOOD_MAX_AGE_S) -> tuple[str, list[str]] | None:
    """從 entries 最後一筆往前找第一筆有 mood 的 entry；太舊就當沒有（不再往前找更舊的）。"""
    for e in reversed(list(entries)):
        mood = getattr(e, "mood", "")
        if not isinstance(mood, str) or not mood:
            continue
        ts_str = getattr(e, "ts_str", "")
        try:
            ts = time.mktime(time.strptime(ts_str, "%Y-%m-%d %H:%M:%S"))
        except (ValueError, TypeError):
            continue
        if now - ts > max_age_s:
            return None
        speakers = getattr(e, "speakers", [])
        if not isinstance(speakers, list):
            speakers = []
        return mood, speakers
    return None


def build_mood_hint(mood: str, speakers: list[str], present) -> str:
    """把日記氣氛行濾成「只給 LLM 內心調語氣」的提示，event 主角不在場就丟事件。"""
    present_set = set(present) if present else set()

    sep = "｜" if "｜" in mood else ("|" if "|" in mood else None)
    if sep is not None:
        tone, _, event = mood.partition(sep)
        tone, event = tone.strip(), event.strip()
    else:
        tone, event = mood.strip(), ""

    if tone in ("", "-", "—", "無"):
        tone = ""

    if speakers and not (set(speakers) & present_set):
        return ""

    if event and not any(name and name in event for name in present_set):
        event = ""

    if tone and event:
        return f"{tone}｜{event}"
    return tone or event


def mood_hint_ctx_lines(hint: str) -> list[str]:
    if not hint:
        return []
    return [f"{MOOD_HINT_HEADER}{hint}", MOOD_HINT_INSTRUCTION]


def _normalize(s: str, names) -> str:
    for name in sorted((n for n in names if n), key=len, reverse=True):
        s = s.replace(name, "")
    return re.sub(r"[^\w]", "", s)


def leaks_mood(text: str, hint: str, names) -> bool:
    """口白是否講破氣氛提示：連續 4 字重疊，或雙方都出現同一個情緒字眼。"""
    if not text or not hint:
        return False

    names = names or ()
    norm_text = _normalize(text, names)
    norm_hint = _normalize(hint, names)
    for i in range(len(norm_hint) - LEAK_NGRAM + 1):
        gram = norm_hint[i:i + LEAK_NGRAM]
        if gram and gram in norm_text:
            return True

    for word in EMOTION_WORDS:
        if word in hint and word in text:
            return True

    return False
