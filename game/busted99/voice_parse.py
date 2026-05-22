"""voice_parse — 從 STT 全文抽出 Busted99 猜測數字。

設計：
- 主要靠 STT 本身（Swift STT 對 1-99 中文/阿拉伯數字辨識準）+ 呼叫端 regex parse_number
- 本模組是 fallback：當 regex 抓不到複雜口語時，才用 LLM 解析
  （「差不多六十左右吧」、「ninety nine」、「七十、不對七十二」）
- 3-layer LLM fallback：Cerebras Qwen → Groq Llama-3.3-70B → Gemini Flash
- 5s timeout 每層，全部失敗回 None
"""

from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)

_CEREBRAS_BASE_URL = "https://api.cerebras.ai/v1"
_GROQ_BASE_URL = "https://api.groq.com/openai/v1"
_CEREBRAS_MODEL = os.environ.get("CEREBRAS_MODEL", "qwen-3-235b-a22b-instruct-2507")
_GROQ_MODEL = os.environ.get("GROQ_FALLBACK_MODEL", "llama-3.3-70b-versatile")
_GEMINI_MODEL = "gemini-2.5-flash"

_SYSTEM_PROMPT = """\
你是 Busted99 遊戲的語音助理，從玩家口語轉錄中抽出唯一的猜測數字。

規則：
- 數字必須是整數，且在範圍 [{low}, {high}] 內
- 接受阿拉伯數字（42）、中文數字（四十二）、英文數字（forty-two）
- 若玩家說多個數字（更正自己），取「最後一個」(例：「七十、不對七十二」→ 72)
- 若範圍外（兩百、零、負數）、無數字、或太模糊，回 null
- 不要解釋，只輸出 JSON

輸出格式：{{"number": N}} 或 {{"number": null}}
"""


def _get_cerebras():
    api_key = os.environ.get("CEREBRAS_API_KEY")
    if not api_key:
        return None
    try:
        from openai import AsyncOpenAI
        return AsyncOpenAI(api_key=api_key, base_url=_CEREBRAS_BASE_URL)
    except ImportError:
        return None


def _get_groq():
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        return None
    try:
        from openai import AsyncOpenAI
        return AsyncOpenAI(api_key=api_key, base_url=_GROQ_BASE_URL)
    except ImportError:
        return None


def _get_gemini():
    api_key = os.environ.get("GOOGLE_API_KEY", "").strip()
    if not api_key:
        return None
    try:
        from google import genai
        return genai.Client(api_key=api_key)
    except ImportError:
        return None


def _parse_response(raw: str, low: int, high: int) -> int | None:
    """parse JSON → validate range → int 或 None。"""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    n = data.get("number")
    if n is None:
        return None
    try:
        n = int(n)
    except (ValueError, TypeError):
        return None
    return n if low <= n <= high else None


async def _try_openai_compat(client, model: str, prompt: str, text: str, low: int, high: int) -> int | None:
    response = await client.chat.completions.create(
        model=model,
        max_tokens=64,
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": text},
        ],
        response_format={"type": "json_object"},
        timeout=5.0,
    )
    return _parse_response(response.choices[0].message.content, low, high)


async def _try_gemini(client, prompt: str, text: str, low: int, high: int) -> int | None:
    from google.genai import types
    response = await client.aio.models.generate_content(
        model=_GEMINI_MODEL,
        contents=text,
        config=types.GenerateContentConfig(
            system_instruction=prompt,
            response_mime_type="application/json",
            max_output_tokens=256,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        ),
    )
    raw = (response.text or "").strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        raw = raw.rsplit("```", 1)[0].strip()
    return _parse_response(raw, low, high)


async def extract_guess_via_llm(
    text: str,
    low: int,
    high: int,
    llm_client=None,
) -> int | None:
    """
    LLM fallback 抽數字 — 3-layer: Cerebras → Groq → Gemini。
    全失敗回 None。呼叫端應該已先試過 regex parse_number()。
    """
    if not text or not text.strip():
        return None

    text = text.strip()
    prompt = _SYSTEM_PROMPT.format(low=low, high=high)

    # 測試用：注入 single client
    if llm_client is not None:
        try:
            return await _try_openai_compat(llm_client, _CEREBRAS_MODEL, prompt, text, low, high)
        except Exception as e:
            logger.warning("[voice_parse] mock LLM 失敗: %s", e)
            return None

    # Layer 1: Cerebras
    client = _get_cerebras()
    if client is not None:
        try:
            r = await _try_openai_compat(client, _CEREBRAS_MODEL, prompt, text, low, high)
            if r is not None:
                return r
        except Exception as e:
            logger.warning("[voice_parse] Cerebras 失敗 → Groq: %s", e)

    # Layer 2: Groq
    client = _get_groq()
    if client is not None:
        try:
            r = await _try_openai_compat(client, _GROQ_MODEL, prompt, text, low, high)
            if r is not None:
                return r
        except Exception as e:
            logger.warning("[voice_parse] Groq 失敗 → Gemini: %s", e)

    # Layer 3: Gemini
    client = _get_gemini()
    if client is not None:
        try:
            return await _try_gemini(client, prompt, text, low, high)
        except Exception as e:
            logger.warning("[voice_parse] Gemini 失敗: %s", e)

    return None
