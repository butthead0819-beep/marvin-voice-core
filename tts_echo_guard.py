"""TTS 台詞複誦 prompt 護欄。

Why：LLM 偶爾會把 system prompt/instruction 整段複誦當作要唸的台詞回傳，
直接送 TTS 會唸出一長串不像人話的指令文字。跟 itunes_cover._similarity
同款 Jaccard/SequenceMatcher 相似度量測，超過門檻視為複誦、擋掉這句 TTS。
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

_TOKENS = re.compile(r"[0-9a-z一-鿿぀-ヿ]+")


def _norm(s: str) -> str:
    return " ".join(_TOKENS.findall((s or "").lower()))


def is_prompt_echo(prompt: str, response: str, threshold: float = 0.5) -> bool:
    """response 與 prompt 相似度超過 threshold → True（該擋掉這句 TTS）。"""
    np, nr = _norm(prompt), _norm(response)
    if not np or not nr:
        return False
    tp, tr = set(np.split()), set(nr.split())
    jaccard = len(tp & tr) / len(tp | tr) if (tp | tr) else 0.0
    similarity = max(jaccard, SequenceMatcher(None, np, nr).ratio())
    return similarity > threshold
