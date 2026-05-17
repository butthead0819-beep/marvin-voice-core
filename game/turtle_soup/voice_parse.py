"""海龜湯語音意圖分類 — 純 regex，不走 LLM。

STT 轉錄出來的文字 → 4 類意圖：question / surrender / final_answer / ignore。
"""
from __future__ import annotations
import re


_SURRENDER_PATTERNS = [
    r"投降",
    r"不玩了",
    r"放棄",
    r"認輸",
    r"棄權",
]

# 必須出現在開頭（玩家直覺：「答案是 XXX」「我認為答案是 XXX」）
_FINAL_ANSWER_PREFIXES = [
    "我認為答案是",
    "我覺得答案是",
    "答案是",
    "我認為是",
    "我覺得是",
    "我猜是",
]

_FILLER_WORDS = {"嗯", "啊", "好", "對啊", "OK", "ok", "嗯啊", "對", "嗯嗯"}

_MIN_LEN = 3


def classify_intent(text: str) -> dict:
    """STT 文字 → 意圖。

    回傳 {
      "intent": "question" | "surrender" | "final_answer" | "ignore",
      "payload": str,
    }

    優先順序（高 → 低）：
      1. ignore（太短 / 純語助詞）
      2. surrender（任意位置出現 surrender keyword）
      3. final_answer（開頭 prefix）
      4. question（其他）
    """
    text = text.strip()

    if len(text) < _MIN_LEN or text in _FILLER_WORDS:
        return {"intent": "ignore", "payload": text}

    for pattern in _SURRENDER_PATTERNS:
        if re.search(pattern, text):
            return {"intent": "surrender", "payload": text}

    for prefix in _FINAL_ANSWER_PREFIXES:
        if text.startswith(prefix):
            payload = text[len(prefix):].strip()
            return {"intent": "final_answer", "payload": payload}

    return {"intent": "question", "payload": text}
