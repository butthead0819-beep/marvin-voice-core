"""Rescue classifier — TieredLLMRouter 的 quick tier 包裝，吐 LLMRescueAgent 吃的 dict。

職責邊界（為什麼跟 LLMRescueAgent 分開）：
- LLMRescueAgent 純 logic：信心門檻、depth+1、ctx 加料、例外不傳染。
  測試不用打 LLM。
- rescue_classifier 純 IO：呼叫 cheap LLM、要求 JSON、parse、容錯。
  測試只 mock TieredLLMRouter，仍不用打 LLM。
- 兩層串起來才會打 LLM（voice_controller 接線時）。

JSON schema (給 LLM 看的)：
  {
    "rewritten_query": "<簡短標準命令句，必填>",
    "pragmatic_signal": "positive" | "negative" | "neutral" | null,
    "pragmatic_target": "current_song" | "last_reply" | "system" | null,
    "confidence": 0.0-1.0
  }
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Protocol

logger = logging.getLogger("cogs.voice_controller.intent_bus.rescue_classifier")


_SYSTEM_PROMPT = """你是語音助理的意圖改寫器。使用者的原始語句沒有被既有 regex 接住，請改寫成可被 regex 命中的最簡標準命令句，並偵測「字面 vs 真意」是否有落差。

回傳嚴格 JSON：
{
  "rewritten_query": "<簡短台灣口語繁體中文命令句，1-10 字最佳>",
  "pragmatic_signal": "positive" | "negative" | "neutral" | null,
  "pragmatic_target": "current_song" | "last_reply" | "system" | null,
  "confidence": <0.0-1.0>
}

語言規則（強制）：
- rewritten_query 必須是「**台灣口語繁體中文**」
- 字形：繁體（影片/品質/網路），禁簡體（视频/质量/网络）— 任何簡體字直接降 confidence
- 用詞：台灣口語（弄/怎樣/可不可以），禁大陸用語（搞/咋了/行不行）

意圖規則：
- rewritten_query 是使用者「真正想觸發的動作」最簡形式
  例：「希望下次播放好聽的歌」→ "下一首"
  例：「能不能小聲一點」→ "音量小一點"
  例：「我覺得這首不太對」→ "下一首"
- pragmatic_signal 偵測字面 vs 真意落差。字面正向但暗示對當前不滿 = "negative"
  例：「希望下次播放好聽的歌」signal=negative target=current_song
  例：「下一首」signal=null（單純命令）
- 不確定就降低 confidence；< 0.7 系統會放棄改寫，不會強行 dispatch
- 完全看不懂使用者要什麼 → confidence: 0.0"""


class _Router(Protocol):
    """Minimal TieredLLMRouter interface — 只用到 quick(json=True)。"""
    async def quick(self, prompt: str, *, caller: str, system: str | None = None,
                    max_tokens: int = 200, temperature: float = 0.7,
                    json: bool = False) -> str | None: ...


def make_rescue_classifier(
    tier_router: _Router,
    *,
    caller: str = "intent_rescue",
) -> Callable[[str], Awaitable[dict[str, Any] | None]]:
    """Build an async classifier suitable for `LLMRescueAgent(llm_classifier=...)`.

    回 None 的情境（皆不該炸 caller）：
    - router 例外 / 回 None / 回空字串
    - 回應不是合法 JSON
    - JSON 缺 rewritten_query 或 rewritten_query 空白
    """
    async def _classify(text: str) -> dict[str, Any] | None:
        try:
            raw = await tier_router.quick(
                prompt=text,
                caller=caller,
                system=_SYSTEM_PROMPT,
                max_tokens=200,
                temperature=0.2,
                json=True,
            )
        except Exception as exc:
            logger.warning(f"[RescueClassifier] router call failed: {exc}")
            return None

        if not raw:
            return None

        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning(f"[RescueClassifier] JSON parse failed: {exc} (raw={raw[:80]!r})")
            return None

        if not isinstance(parsed, dict):
            return None

        rewritten = parsed.get("rewritten_query")
        if not isinstance(rewritten, str) or not rewritten.strip():
            return None

        return parsed

    return _classify


# ─────────────────────────────────────────────────────────────────────────────
# build_rescue_components — voice_controller 唯一接觸點。
# 把 env gating / classifier / agent / sink 工廠化成一個 unit-testable 函式，
# 讓 voice_controller 的 IntentBus 構造維持 3-4 行 wiring，無條件邏輯。
# ─────────────────────────────────────────────────────────────────────────────


_ENV_ENABLED = "MARVIN_INTENT_RESCUE_ENABLED"
_ENV_SHADOW = "MARVIN_INTENT_RESCUE_SHADOW"
_OUTCOME_PATH = Path("records/rescue_outcomes.jsonl")


def build_rescue_components(
    tier_router,
    *,
    env: Mapping[str, str] | None = None,
) -> tuple[Any | None, bool, Callable[[dict], None] | None]:
    """Returns (rescue_agent, shadow_mode, outcome_sink) 給 IntentBus 接線。

    回 (None, False, None) 的情境：
    - env 未開啟（MARVIN_INTENT_RESCUE_ENABLED != "1"）
    - tier_router 是 None（pool 都沒 key）
    """
    import os
    from intent_agents.llm_rescue_agent import LLMRescueAgent
    from intent_agents.rescue_outcome_logger import RescueOutcomeLogger

    if env is None:
        env = os.environ

    if env.get(_ENV_ENABLED) != "1":
        return None, False, None
    if tier_router is None:
        return None, False, None

    classifier = make_rescue_classifier(tier_router)
    agent = LLMRescueAgent(llm_classifier=classifier)
    shadow = env.get(_ENV_SHADOW, "1") != "0"
    outcome_logger = RescueOutcomeLogger(_OUTCOME_PATH)
    return agent, shadow, outcome_logger.write
