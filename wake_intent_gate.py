"""Wake-after intent presence gate（pure-code，不打 LLM）。

IntentBus 沒接到 winner → fall through 到 Marvin 主 LLM 前過這層。
raw 有實質指令訊號才放行；只有 filler/短應答 → silent，省下一次 LLM call 且避免錯時機亂回答。

設計原則：保守放行（寧可多打 LLM 也不要漏接真正的指令）。filler/短應答 list 是
hard-blocklist；其餘有問號 / 指令動詞 / 長度 ≥ 4 字一律放行。
"""
from __future__ import annotations

import re

# 純 wake 詞（cleaner 注入後可能剩這些）— 沒接其他內容就不算指令
_WAKE_WORDS = frozenset({"馬文", "瑪文", "媽文", "麻文", "馬汶", "marvin", "marvy"})

# Filler / 短應答 hard-blocklist（剝標點後完全等於這些 → 擋）
_FILLERS = frozenset({
    # 純語氣詞
    "嗯", "啊", "喔", "欸", "呃", "誒", "唉", "哎",
    "嗯嗯", "啊啊", "喔喔", "欸欸",
    # 短應答
    "對", "對啊", "對對", "對對對",
    "好", "好啊", "好的", "好喔",
    "沒事", "沒有", "沒",
    "是", "是啊", "是的",
    "ok", "okay",
})

# 問句標誌（任一命中即放行）
_QUESTION_MARKERS = ("?", "？", "嗎", "呢", "什麼", "怎麼", "為什麼",
                     "為何", "為甚麼", "為啥", "誰", "哪", "幾",
                     "甚麼", "麼樣")

# 指令動詞（任一命中即放行）
_IMPERATIVE_VERBS = ("幫", "教", "告訴", "查", "找", "算", "翻譯",
                     "解釋", "介紹", "說明", "搜", "請", "想知道",
                     "能不能", "可不可以", "可以幫", "麻煩")

# 標點清理（剝後比對 filler 用）
_PUNCT_RE = re.compile(r"[。，,、！!？?\.\s]+")


def _strip(text: str) -> str:
    """剝標點 + 小寫 + trim。"""
    return _PUNCT_RE.sub("", (text or "").strip()).lower()


def has_intent_signal(query: str) -> bool:
    """True → 值得打 Marvin LLM；False → silent（filler/短應答，沒實質指令）。

    決策階梯：
      1. 空 / 全標點 → False
      2. 剝標點後完全是 filler / 短應答 / 純 wake 詞 → False
      3. 含問號或問句標誌 → True
      4. 含指令動詞 → True
      5. 字數 ≥ 4 → True（陳述句也可能是對話內容，放行）
      6. 其它（3 字以內非問句非指令）→ False（保守擋）
    """
    if not query:
        return False
    raw = query.strip()
    if not raw:
        return False

    stripped = _strip(raw)
    if not stripped:
        return False  # 全標點

    # 顯式問號 / 問句標誌優先於 filler 檢查（「好?」是問句不是填詞）
    if any(m in raw for m in _QUESTION_MARKERS):
        return True

    # 指令動詞
    if any(v in raw for v in _IMPERATIVE_VERBS):
        return True

    if stripped in _FILLERS:
        return False
    if stripped in _WAKE_WORDS:
        return False

    # 長度啟發：≥ 4 字（含中文）→ 視為有實質內容
    # 用 stripped 算才不被「對對對。」這種拉長 filler 騙到
    if len(stripped) >= 4:
        return True

    return False
