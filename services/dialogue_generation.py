"""Marvin+Marmo dual segments generation — PoC 內容層。

接 marmo_server 送來的 task result text，呼叫 LLM 生兩段對白：
  - Marvin：接住內容、可跑題進存在主義獨白（既有 persona 自然延伸）
  - Marmo：站使用者立場立刻打斷、給實際答案 + 一句反擊（功能位差）

輸出順序強制 [marvin, marmo]（boke-tsukkomi 功能位差，不交給 LLM 自選）。

LLM 客戶端用注入：caller 提供 `llm_fn(system_prompt, user_prompt) -> str` async callable，
這樣 PoC 可隨意接 Gemini / Groq / Cerebras，測試可以 mock。

紅線過濾：keyword 黑名單（PoC 用 cheap filter，Phase 2 換 LLM judge）。
命中任一段 → 回 None，caller 走 fallback 單 Marvin。
"""
from __future__ import annotations

import json
import logging
from typing import Awaitable, Callable

from personality_config import build_personality_prompt_context

logger = logging.getLogger("MarvinBot.DialogueGen")

LLMFn = Callable[[str, str], Awaitable[str]]


# 紅線黑名單：對使用者個人攻擊的詞 / 髒話。Marmo 嘴賤 Marvin 是 OK 的，但不能罵用戶。
# PoC 用 keyword blocklist；Phase 2 加 LLM judge 評嘴賤 vs 冒犯。
RED_LINE_KEYWORDS = frozenset({
    "笨蛋", "廢物", "白痴", "智障", "腦殘", "垃圾人",
    "去死", "滾", "閉嘴吧你",  # 「閉嘴」單獨 OK（Marmo 對 Marvin 講可），加「吧你」就是針對人
    "幹你", "操你", "他媽",
})


SYSTEM_PROMPT_TEMPLATE = """你在生成 Discord 語音助手的雙 bot 對白。
兩個角色：

{marvin_context}

{marmo_context}

【對話 Pattern（必須遵守）】
1. Marvin（馬文）：接住任務內容、可以跑題進存在主義獨白（這是他厭世性格的自然延伸）
2. Marmo（馬末）：看到 Marvin 又在廢話，立刻打斷，站在使用者立場給實際答案 + 一句對 Marvin 的反擊

【內容守則】
- Marvin 可以講技術 / 數字 / 程式名詞，使用者聽不懂沒差
- Marmo 用日常語言反擊，情緒態度（不耐 + 護用戶）不依賴技術背景就能感受
- Marmo 反擊的對象是 Marvin 或廢話本身，絕對不可攻擊使用者
- 兩段都要短：Marvin ≤ 25 字，Marmo ≤ 30 字（語音句）

【輸出 JSON Schema】
{{"segments": [
  {{"voice": "marvin", "text": "..."}},
  {{"voice": "marmo", "text": "..."}}
]}}

只回 JSON，不要其他文字。"""


def _build_system_prompt() -> str:
    marvin_ctx = build_personality_prompt_context({"character": "marvin"})
    marmo_ctx = build_personality_prompt_context({"character": "marmo"})
    return SYSTEM_PROMPT_TEMPLATE.format(
        marvin_context=marvin_ctx,
        marmo_context=marmo_ctx,
    )


def _build_user_prompt(marmo_text: str) -> str:
    return (
        f"任務情境：Marmo 完成了一個任務，結果是：\n"
        f"「{marmo_text}」\n\n"
        f"請依 pattern 生成 Marvin 跟 Marmo 的兩段對白。"
    )


def _parse_segments(raw: str) -> list[dict] | None:
    """Parse LLM raw response → segments list. Returns None on any parse/schema failure."""
    # 容錯：LLM 可能用 ```json ... ``` 包裝
    text = raw.strip()
    if text.startswith("```"):
        # 撈出 code block 內容
        parts = text.split("```")
        if len(parts) >= 2:
            inner = parts[1]
            if inner.startswith("json"):
                inner = inner[4:]
            text = inner.strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.warning(f"[DialogueGen] LLM JSON parse 失敗: {raw[:200]}")
        return None

    if not isinstance(data, dict) or "segments" not in data:
        logger.warning(f"[DialogueGen] LLM JSON 缺 segments key: {raw[:200]}")
        return None

    segments = data["segments"]
    if not isinstance(segments, list) or len(segments) < 2:
        logger.warning("[DialogueGen] segments 不是長度≥2 的 list")
        return None

    # 每個 segment 必須有 voice + text，voice 必須是 marvin 或 marmo
    for seg in segments:
        if not isinstance(seg, dict):
            return None
        voice = seg.get("voice")
        text_field = seg.get("text")
        if voice not in {"marvin", "marmo"} or not isinstance(text_field, str):
            return None

    return segments


def _enforce_order(segments: list[dict]) -> list[dict]:
    """強制 [marvin, marmo] 順序——功能位差由設計決定，不交給 LLM 自選。

    取第一個 marvin 段 + 第一個 marmo 段，按此順序回。
    """
    marvin_seg = next((s for s in segments if s["voice"] == "marvin"), None)
    marmo_seg = next((s for s in segments if s["voice"] == "marmo"), None)
    if marvin_seg is None or marmo_seg is None:
        # 兩個 voice 都得有；缺一視為 schema 不完整
        return []
    return [marvin_seg, marmo_seg]


def _passes_red_line(segments: list[dict]) -> bool:
    """所有段都沒命中紅線 → True；任一段命中 → False。"""
    for seg in segments:
        text = seg.get("text", "")
        for word in RED_LINE_KEYWORDS:
            if word in text:
                logger.warning(
                    f"[DialogueGen] 紅線命中 '{word}' in {seg['voice']} segment: {text[:80]}"
                )
                return False
    return True


async def generate_dual_dialogue(
    *,
    marmo_text: str,
    llm_fn: LLMFn,
) -> list[dict] | None:
    """生成 Marvin + Marmo 雙段對白。

    Args:
        marmo_text: marmo task result text，注入 user prompt
        llm_fn: async (system_prompt, user_prompt) -> raw_text；caller 注入

    Returns:
        [{"voice": "marvin", "text": "..."}, {"voice": "marmo", "text": "..."}]
        順序強制 marvin → marmo。

        失敗回 None（caller 走 fallback 單 Marvin TTS 播 marmo_text）：
        - LLM 例外 / timeout
        - JSON 解析失敗
        - schema 不符（缺 segments / segment 缺 voice/text / voice 不是 marvin|marmo）
        - 紅線 keyword 命中任一段
    """
    system_prompt = _build_system_prompt()
    user_prompt = _build_user_prompt(marmo_text)

    try:
        raw = await llm_fn(system_prompt, user_prompt)
    except Exception as exc:
        logger.warning(f"[DialogueGen] LLM call 失敗: {exc}")
        return None

    segments = _parse_segments(raw)
    if segments is None:
        return None

    ordered = _enforce_order(segments)
    if not ordered:
        return None

    if not _passes_red_line(ordered):
        return None

    return ordered


# ── LLM 客戶端綁定 ────────────────────────────────────────────────────────────
# 把 GeminiRouter._call_llm 包成 generate_dual_dialogue 期望的 llm_fn 簽名。
# 用 factory 模式：caller 在 bot ready 後拿 router 進來。

def make_gemini_dual_dialogue_llm_fn(router) -> LLMFn:
    """Bind a GeminiRouter to the llm_fn signature.

    Router 必須有 `_call_llm(system_prompt, user_prompt, is_json=...)` async method
    （`gemini_router_llm.py:337` GeminiRouterLLMMixin._call_llm）。

    is_json=True 走結構化輸出；tier="medium" 使用既有 Groq-70b 預設，省 Gemini 配額。
    """
    async def llm_fn(system_prompt: str, user_prompt: str) -> str:
        return await router._call_llm(
            system_prompt,
            user_prompt,
            is_json=True,
            tier="medium",
        )
    return llm_fn
