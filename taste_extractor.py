"""即時明示偏好偵測（taste Phase C，確定性，零 LLM）。

P1 修好 DB/JSON 同步後，daily review 的 LLM 興趣抽取已能進 bot runtime；C 不再重做即時
LLM 抽取（與 slow-learning 原則衝突，見記憶 feedback_slow_learning_via_recommendations）。
改用 regex side-channel（仿 voice_controller 的 Farewell detector）只抓**明示**偏好句，
給小分（< LIKE_THRESHOLD）入「曾提及」，需跨場景累積才升 confirmed。隱性興趣仍交 offline daily。

唯一公開入口 extract_taste_signals(text) → [(item, signed_delta), ...]。
caller（VoiceController）對每筆呼叫 memory.record_taste_signal(speaker, item, delta)。
"""
from __future__ import annotations

import re

# 單次明示偏好的分量。< LIKE_THRESHOLD(3.0) → 只入「曾提及」，跨場景重複才 confirmed。
REALTIME_TASTE_DELTA = 1.0

# 句中任意位置的「我<程度?><動詞>X」。先比對討厭（含否定）再比對喜歡，避免「我不喜歡」誤判成喜歡。
# 排除集含「我」：run-on（我喜歡A我討厭B）下一個「我」是新子句開頭，幾乎不出現在興趣項目內。
_OBJ = r"([^，。！？、…\s,.!?；;：:我]{1,12})"
_DISLIKE_RE = re.compile(
    r"我(?:真的|超|很|好|最|非常)?(?:超級)?(?:討厭|不喜歡|不愛|受不了|厭惡|恨死|很煩)" + _OBJ
)
_LIKE_RE = re.compile(
    r"我(?:真的|超|很|好|最|非常|蠻|還蠻|挺)?(?:超級)?(?:喜歡|喜愛|熱愛|愛上|迷上|沉迷|愛)" + _OBJ
)

# 抓到的 item 若只是代名詞/疑問詞 → 非興趣項目，丟棄。
_PRONOUN_STOP = {"你", "他", "她", "它", "我", "我們", "你們", "他們", "誰", "什麼", "這", "那"}
# 語尾粒子（句末）→ 清掉，避免「爬山啦」這種尾巴。標點已由 _OBJ 排除。
_TRAILING_PARTICLES = "啦呢喔耶欸啊嘛喲哦了吧嗎哈"


def _clean_item(raw: str) -> str:
    item = raw.strip().lstrip("的")
    item = item.rstrip(_TRAILING_PARTICLES)
    return item.strip()


def extract_taste_signals(text: str) -> list[tuple[str, float]]:
    """從一句話抽出明示偏好訊號。回傳 [(item, signed_delta)]，正=喜歡、負=討厭。

    同一 item 在一句內只記一次（dedup）。討厭優先比對，已被討厭吃掉的不再當喜歡。
    """
    if not text or not isinstance(text, str):
        return []

    signals: list[tuple[str, float]] = []
    seen: set[str] = set()

    for raw in _DISLIKE_RE.findall(text):
        item = _clean_item(raw)
        if item and item not in _PRONOUN_STOP and item not in seen:
            seen.add(item)
            signals.append((item, -REALTIME_TASTE_DELTA))

    for raw in _LIKE_RE.findall(text):
        item = _clean_item(raw)
        if item and item not in _PRONOUN_STOP and item not in seen:
            seen.add(item)
            signals.append((item, REALTIME_TASTE_DELTA))

    return signals
