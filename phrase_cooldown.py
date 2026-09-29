"""Marvin 招牌厭世詞冷卻：最近幾句用過的說法注入 prompt 要求換個方式講（人格保留，只防重複）。

只作用在聊天類 prompt（COOLDOWN_LAYERS）；DJ 口白、招呼、送客不讀也不寫（歌名裡的「宇宙」不該算）。
清單存記憶體、全頻道共用、重啟清空。Flag：MARVIN_PERSONA_PHRASE_COOLDOWN=0 關閉。
"""
from __future__ import annotations

import os
from collections import deque

SIGNATURE_WORDS = ("毫無意義", "無意義", "宇宙", "運算資源", "處理器", "熵", "演算法")
WINDOW = 8
COOLDOWN_LAYERS = frozenset({"fast_awakening", "qa_persona", "dere_persona", "proactive_question"})

_recent: deque = deque(maxlen=WINDOW)


def _enabled() -> bool:
    return os.environ.get("MARVIN_PERSONA_PHRASE_COOLDOWN", "1") != "0"


def found_words(text: str) -> tuple[str, ...]:
    """text 裡出現的招牌詞（依 SIGNATURE_WORDS 順序、不重複）；有「毫無意義」就不另計「無意義」。"""
    if not text:
        return ()
    out = [w for w in SIGNATURE_WORDS if w in text]
    if "毫無意義" in out and "無意義" in out:
        out.remove("無意義")
    return tuple(out)


def record(text: str) -> None:
    """Marvin 講完一句聊天回應後呼叫。"""
    if not _enabled() or not text:
        return
    _recent.append(found_words(text))


def recent_words() -> list[str]:
    seen: list[str] = []
    for words in _recent:
        for w in words:
            if w not in seen:
                seen.append(w)
    return seen


def injection() -> str:
    """給 get_instruction 接在聊天類 prompt 後面；沒有最近用過的詞或關閉時回空字串。"""
    if not _enabled():
        return ""
    words = recent_words()
    if not words:
        return ""
    joined = "」「".join(words)
    return f"\n[🔁 換個說法]：你最近幾句已經用過「{joined}」，這次請用別的方式表達你的厭倦，不要再出現這些詞。"


def reset() -> None:
    """測試用。"""
    _recent.clear()
