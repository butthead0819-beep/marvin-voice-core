"""Groq chat classifier adapter — TieredLLMRouter → ChatClassifierCall.

把 quick tier (Groq 8B / Cerebras 8B / SambaNova ... fallback chain) 包成
ChatClassifierCall signature，給 chat_classifier_judge / j1_with_veto 用。

設計：
  - router.quick(json=True) 啟用 JSON mode（Groq/Cerebras OpenAI-compatible 都支援）
  - caller="chat_classifier" 給 per-agent token attribution
  - temperature=0.0 — 分類任務要穩定
  - pool 全冷卻 (router.quick 回 None) → 安全 default：is_chat=False, confidence=0.0
    避免誤殺 J1 正向 intent
  - JSON 解析失敗 → raise（chat_classifier_judge 的 exception path 接住 → 安全 default）
"""
from __future__ import annotations

import json
from typing import Awaitable, Callable

ChatClassifierCall = Callable[[str, str], Awaitable[dict]]

# Groq 8B 約 150ms / 80 token；keep prompt 簡短。
_SYSTEM_PROMPT = """你是 Discord 語音 bot 的 chat-vs-intent 分類器。
判斷使用者說的話是「真指令」還是「閒聊/反問/描述」。

範例：
- raw="下一首" intent=skip → {"is_chat": false, "confidence": 0.95, "reason": "strong_keyword"}
- raw="應該下一首就是" intent=skip → {"is_chat": true, "confidence": 0.90, "reason": "modal:應該"}
- raw="為什麼你下一首" intent=skip → {"is_chat": true, "confidence": 0.92, "reason": "question_word:為什麼"}
- raw="麻煩幫我找到這個線上網站" intent=music → {"is_chat": true, "confidence": 0.88, "reason": "non_music_target:網站"}
- raw="播放周杰倫" intent=music → {"is_chat": false, "confidence": 0.95, "reason": "strong_keyword"}

輸出 JSON only：{"is_chat": bool, "confidence": float, "reason": str}"""

_SAFE_DEFAULT = {"is_chat": False, "confidence": 0.0, "reason": "pool_exhausted"}


def make_groq_chat_classifier(router) -> ChatClassifierCall:
    """Build a ChatClassifierCall bound to the given TieredLLMRouter's quick tier."""

    async def _call(raw_text: str, intent_name: str) -> dict:
        response = await router.quick(
            prompt=f'raw="{raw_text}" intent="{intent_name}"',
            caller="chat_classifier",
            system=_SYSTEM_PROMPT,
            max_tokens=80,
            temperature=0.0,
            json=True,
        )
        if response is None:
            return dict(_SAFE_DEFAULT)
        return json.loads(response)

    return _call
