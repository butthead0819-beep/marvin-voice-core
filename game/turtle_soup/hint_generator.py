"""海龜湯 hint generator — LLM 產生 1D/2D/3D 三層遞進提示。

設計理念：聯想維度（cognitive distance from the answer）
  1D 直接關聯：指向湯底某個單一面向（身體 / 物品 / 時間…）。不揭露內容，只指類別。
  2D 二維關聯：連結湯底中兩個元素的對比、因果、先後。
  3D 三維關聯：描述真相背後的「機制」或「依賴條件」，但不直接說出。

用途：題目作者在設計新題目時跑這個工具產出 3 條 hint 候選，人工審核後寫入
puzzles.py。runtime 不調用（v0 已預生成、v4 UGC 時再考慮 lazy 生成）。

3-layer fallback（與 judge 共用 Cerebras / Groq / Gemini）。
"""
from __future__ import annotations
import json
import logging
from typing import Any

from game.llm_clients import (
    get_cerebras_client,
    get_groq_client,
    get_gemini_client,
    CEREBRAS_MODEL,
    GROQ_MODEL,
    GEMINI_MODEL,
)

logger = logging.getLogger(__name__)


SYSTEM = """你是海龜湯題目的提示設計師。給定一道題目的湯面、湯底、key_facts、leak_keywords，
你要產出 3 條依「聯想維度」遞進的提示，由弱到強：

【1D 直接關聯】Direct
指向湯底真相中的某個單一面向（身體 / 物品 / 職業 / 時間 / 地點 / 情緒…）。
不揭露具體內容，只指向類別。讓玩家知道「該往哪個方向想」。
範例：「想想他的身體有什麼特別」「注意時間點」「這個物品有特殊用途」

【2D 二維關聯】Relational
連結湯底中兩個元素的對比、因果、先後或差異。
讓玩家發現「為什麼 A 可以、B 不行」或「同樣是 X 為什麼結果不同」。
範例：「為什麼他早上能做 A，晚上不能做 B？」「兩次行為的差別在哪？」

【3D 三維關聯】Conceptual
描述真相背後的「機制」「依賴條件」或「環境約束」，但不直接說出名稱。
最接近答案，幾乎可推導出湯底但仍要玩家自己拼起來。
範例：「有人在場時可以，獨自時不行 — 這背後是什麼限制？」

# 鐵律（違反視為失敗，工具會棄用此次生成）
1. 不可包含 leak_keywords 列表中任何詞
2. 不可直接寫出湯底中的具體名詞（即使該詞不在 leak_keywords）
3. 每條提示 15-35 字，自然口語
4. 三條必須遞進：1D 最弱、3D 最強。順序錯誤視為失敗
5. Marvin 主持人語氣：簡潔、有引導力、不雞湯不冗長

# 輸出（嚴格 JSON）
{
  "direct": "<1D 提示>",
  "two_dimensional": "<2D 提示>",
  "three_dimensional": "<3D 提示>"
}
"""


def _build_user_msg(surface: str, truth: str, key_facts: list[str], leak_keywords: list[str]) -> str:
    return json.dumps({
        "湯面": surface,
        "湯底": truth,
        "key_facts": key_facts,
        "leak_keywords": leak_keywords,
    }, ensure_ascii=False)


REQUIRED_KEYS = {"direct", "two_dimensional", "three_dimensional"}
_TIMEOUT = 8.0


def _validate(raw: Any) -> dict | None:
    if not isinstance(raw, dict):
        return None
    if not REQUIRED_KEYS.issubset(raw):
        return None
    for k in REQUIRED_KEYS:
        v = raw.get(k)
        if not isinstance(v, str) or not v.strip():
            return None
    return {k: raw[k].strip() for k in REQUIRED_KEYS}


async def _call_cerebras(user_msg: str) -> dict | None:
    client = get_cerebras_client()
    if client is None:
        return None
    try:
        resp = await client.chat.completions.create(
            model=CEREBRAS_MODEL,
            max_tokens=512,
            messages=[
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            response_format={"type": "json_object"},
            timeout=_TIMEOUT,
        )
        return _validate(json.loads(resp.choices[0].message.content))
    except Exception as e:
        logger.debug(f"[hint_gen] Cerebras 失敗: {type(e).__name__}: {e}")
        return None


async def _call_groq(user_msg: str) -> dict | None:
    client = get_groq_client()
    if client is None:
        return None
    try:
        resp = await client.chat.completions.create(
            model=GROQ_MODEL,
            max_tokens=512,
            messages=[
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            response_format={"type": "json_object"},
            timeout=_TIMEOUT,
        )
        return _validate(json.loads(resp.choices[0].message.content))
    except Exception as e:
        logger.debug(f"[hint_gen] Groq 失敗: {type(e).__name__}: {e}")
        return None


async def _call_gemini(user_msg: str) -> dict | None:
    client = get_gemini_client()
    if client is None:
        return None
    try:
        from google.genai import types
        resp = await client.aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=user_msg,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM,
                response_mime_type="application/json",
                max_output_tokens=768,
                temperature=0.7,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
        return _validate(json.loads(resp.text))
    except Exception as e:
        logger.debug(f"[hint_gen] Gemini 失敗: {type(e).__name__}: {e}")
        return None


async def generate_hint_tiers(
    surface: str,
    truth: str,
    key_facts: list[str],
    leak_keywords: list[str],
) -> dict:
    """3-layer fallback 產生三維 hints。

    回傳 {
      "direct": str,
      "two_dimensional": str,
      "three_dimensional": str,
      "_provider": "Cerebras" | "Groq" | "Gemini" | "fallback",
    }

    fallback 情況下三個欄位皆為空字串，呼叫方應人工填寫。
    """
    user_msg = _build_user_msg(surface, truth, key_facts, leak_keywords)
    for fn, name in (
        (_call_cerebras, "Cerebras"),
        (_call_groq, "Groq"),
        (_call_gemini, "Gemini"),
    ):
        result = await fn(user_msg)
        if result:
            # 後處理：剔除任何洩底關鍵詞（保險網）
            filtered = _filter_leaks(result, leak_keywords)
            return {**filtered, "_provider": name}
    return {
        "direct": "",
        "two_dimensional": "",
        "three_dimensional": "",
        "_provider": "fallback",
    }


def _filter_leaks(hints: dict, leak_keywords: list[str]) -> dict:
    """若任何 hint 含洩底詞 → 加 ⚠ 標記，讓人工審核時注意。

    不直接改寫（hint generation 是離線工具，作者應親自決定保留 / 重生 / 改寫）。
    """
    result = dict(hints)
    for key in REQUIRED_KEYS:
        hint = result.get(key, "")
        for kw in leak_keywords:
            if kw and kw in hint:
                result[key] = f"⚠[LEAK:{kw}] {hint}"
                break
    return result
