"""AudioRescueAgent — Audio Rescue v2：原始語音 + Gemini function calling 版 rescue。

跟 LLMRescueAgent（llm_rescue_agent.py，文字版）共用同一個
`synthesize(ctx) -> IntentContext | None` 入口 contract，但輸入是原始語音
wav bytes 而非清洗過的文字，輸出的 ctx 帶 `dispatch_source="llm_rescue_audio"`
+ resolved_agent/resolved_intent/resolved_slots，而非改寫過的 query。
IntentBus._maybe_rescue() 依 dispatch_source 分流到不同執行路徑（見 intent_bus.py）。

單輪 function calling：一次 Gemini 呼叫，LLM 可能平行回多個 tool call，依序
處理，不做多輪 agent loop（不把執行結果餵回 LLM 讓它決定下一步）——唯讀 tool
（get_now_playing/get_recent_history）因此看不到彼此、也看不到 action tool
的結果，只能定位成「LLM 判斷使用者在問資訊」時的替代終點，不是真正的多輪推理。

失敗降級：任何逾時/API 例外/沒選中 tool 一律乾淨 return None，交由
IntentBus._maybe_rescue()（已有 try/except 包一層）與 caller 走既有「無 fallback，
走 Marvin 一般聊天」路徑，不拋例外往上炸，不製造新單點故障。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import Any, Awaitable, Callable

from google.genai import types

from intent_bus import IntentContext
from intent_agents.audio_rescue_tools import (
    READONLY_FUNCTION_DECLARATIONS,
    READONLY_TOOL_NAMES,
    manifest_to_function_declarations,
    parse_tool_call,
)

logger = logging.getLogger("cogs.voice_controller.intent_bus.audio_rescue")


_PROMPT = (
    "你是語音助理的意圖辨識器。使用者剛才說了一段話（見附帶音訊），但既有的規則式"
    "辨識沒有命中任何已知指令。請直接聽音訊判斷使用者真正想做什麼，並呼叫最符合的"
    "一個工具；如果使用者只是在問資訊（例如問現在在放什麼歌、剛剛聊了什麼），改呼叫"
    "對應的查詢工具。如果聽不出使用者想要哪個已知操作，不要呼叫任何工具。\n"
    "（STT 轉錄參考，可能有誤：「{query}」）"
)

# 唯讀 tool 執行器 contract：async (tool_name: str, args: dict, ctx: IntentContext) -> str | None
ReadonlyToolExecutor = Callable[[str, dict, IntentContext], Awaitable[str | None]]


class AudioRescueAgent:
    """Audio-native rescue：原始語音 → Gemini function calling → 選定 intent。"""

    name: str = "AudioRescue"

    def __init__(
        self,
        *,
        google_client: Any,
        manifest_provider: Callable[[], dict],
        model: str = "gemini-2.0-flash-lite",
        timeout_s: float = 3.0,
        readonly_tool_executor: ReadonlyToolExecutor | None = None,
    ):
        self.google_client = google_client
        self.manifest_provider = manifest_provider
        self.model = model
        self.timeout_s = timeout_s
        self.readonly_tool_executor = readonly_tool_executor

    async def synthesize(self, ctx: IntentContext) -> IntentContext | None:
        if not ctx.audio_wav_bytes:
            return None

        manifest = self.manifest_provider()
        function_declarations = (
            manifest_to_function_declarations(manifest) + READONLY_FUNCTION_DECLARATIONS
        )
        if not function_declarations:
            return None

        try:
            audio_part = types.Part.from_bytes(
                data=bytes(ctx.audio_wav_bytes), mime_type="audio/wav"
            )
            response = await asyncio.wait_for(
                self.google_client.aio.models.generate_content(
                    model=self.model,
                    contents=[audio_part, _PROMPT.format(query=ctx.query or "")],
                    config=types.GenerateContentConfig(
                        tools=[types.Tool(function_declarations=function_declarations)],
                        temperature=0.0,
                    ),
                ),
                timeout=self.timeout_s,
            )
        except asyncio.TimeoutError:
            logger.warning("⚠️ [AudioRescue] Gemini 呼叫逾時，放棄 rescue")
            return None
        except Exception as exc:
            logger.warning(f"⚠️ [AudioRescue] Gemini 呼叫失敗，放棄 rescue: {exc}")
            return None

        calls = getattr(response, "function_calls", None) or []
        if not calls:
            return None

        resolved: tuple[str, str, dict] | None = None
        for call in calls:
            if call.name in READONLY_TOOL_NAMES:
                await self._execute_readonly(call, ctx)
                continue
            if resolved is not None:
                logger.info(
                    f"📡 [AudioRescue] 多個 action tool call，只取第一個，"
                    f"忽略: {call.name}"
                )
                continue
            parsed = parse_tool_call(call)
            if parsed is not None:
                resolved = parsed
            else:
                logger.warning(f"⚠️ [AudioRescue] tool call name 格式異常，忽略: {call.name}")

        if resolved is None:
            return None

        agent_name, intent_name, args = resolved
        return replace(
            ctx,
            dispatch_source="llm_rescue_audio",
            depth=ctx.depth + 1,
            resolved_agent=agent_name,
            resolved_intent=intent_name,
            resolved_slots=args,
        )

    async def _execute_readonly(self, call, ctx: IntentContext) -> None:
        if self.readonly_tool_executor is None:
            return
        args = dict(getattr(call, "args", None) or {})
        try:
            await self.readonly_tool_executor(call.name, args, ctx)
        except Exception as exc:
            logger.warning(f"⚠️ [AudioRescue] readonly tool {call.name} 執行失敗: {exc}")
