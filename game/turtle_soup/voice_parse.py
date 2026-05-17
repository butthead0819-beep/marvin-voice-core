"""海龜湯語音意圖分類 — 純 regex，不走 LLM。

STT 轉錄出來的文字 → 5 類意圖：
  question / surrender / final_answer / discussion / ignore

discussion 是給「玩家之間互相討論」用的：沒有「請問」開頭的句子不送 LLM，
也不播 SFX/TTS。這是為了避免推理討論被誤判成問題、避免 LLM 成本被討論吃光。
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

# 必須出現在開頭，才視為對 Marvin 的提問；否則歸 discussion 忽略
# ⚠ 順序：長的前綴在前（避免「我問你 X」被「我問」誤吃成「你 X」）
_QUESTION_PREFIXES = [
    "我可以問",
    "我想問",
    "問題是",
    "問一下",
    "我問你",
    "問你",
    "我問",
    "請問",
]

_FILLER_WORDS = {"嗯", "啊", "好", "對啊", "OK", "ok", "嗯啊", "對", "嗯嗯"}

_MIN_LEN = 3

# question prefix 後若 payload 含這些關鍵詞 → 升級為 hint_request
_HINT_KEYWORDS = ["提示", "線索"]


def classify_intent(text: str) -> dict:
    """STT 文字 → 意圖。

    回傳 {
      "intent": "question" | "surrender" | "final_answer" | "discussion" | "ignore",
      "payload": str,
    }

    優先順序（高 → 低）：
      1. ignore（太短 / 純語助詞）
      2. surrender（任意位置出現 surrender keyword）
      3. final_answer（開頭 prefix）
      4. question（開頭含「請問」「我想問」等明確問句前綴）
      5. discussion（其他，包含玩家之間討論、自言自語）

    cog 收到 discussion → 靜默忽略，不送 LLM、不播 SFX/TTS。
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

    for prefix in _QUESTION_PREFIXES:
        if text.startswith(prefix):
            # 去掉前綴讓 LLM judge 看乾淨的問題
            payload = text[len(prefix):].strip().lstrip("，,。.?？！! ")
            # 含「提示」/「線索」等關鍵詞 → 升級為 hint_request
            if any(kw in payload for kw in _HINT_KEYWORDS):
                return {"intent": "hint_request", "payload": payload}
            return {"intent": "question", "payload": payload}

    # 沒有問題前綴 → 視為玩家間討論，忽略
    return {"intent": "discussion", "payload": text}
