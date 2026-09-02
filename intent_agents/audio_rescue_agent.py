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

付費鐵則（feedback_paid_calls_must_record）：比照 stt_cleaner.py::_try_paid() 的
pattern——呼叫前 RPM 視窗守門 + PaidUsageGuard.allow() 估價守門，成功後
guard.record() 記帳（優先用 response.usage_metadata 的真實 token 數，缺才退回估算）。
不硬塞 LLMBus：LLMBus 目前沒有 Gemini agent、也沒有音訊/tool-calling 先例，其
F3/F4 多 provider 比價機制對「只有一家能接」的音訊呼叫沒有實質效益。

範圍守門（8/22 加、8/22 放寬）：單次呼叫成本量到只有 $0.00002-0.00003（音訊短句
換算 token 極少），估算過在正常量級下完全不會頂到 daily/monthly cap，所以刻意放寬
守門到接近不擋——保留的只是「完全空字串/沒錄到音」這種真的沒東西可送的情況，跟一個
較高的 RPM/cap 天花板防真正失控的迴圈，不是拿來擋一般語意。
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from typing import Any, Awaitable, Callable

from google.genai import types

from intent_bus import IntentContext
from intent_agents.audio_rescue_tools import (
    ABSTAIN_FUNCTION_DECLARATION,
    ABSTAIN_TOOL_NAME,
    READONLY_FUNCTION_DECLARATIONS,
    READONLY_TOOL_NAMES,
    intent_to_agent_map,
    manifest_to_function_declarations,
    parse_tool_call,
)
from llm_paid import PaidUsageGuard, estimate_cost

logger = logging.getLogger("cogs.voice_controller.intent_bus.audio_rescue")

# 單次呼叫成本 ~$0.00002-0.00003（見 8/22 估算）——放寬到跟 GCP 帳戶本身的
# spending cap（$10，llm_paid.py 註解）同量級，而非文字版沿用的保守 0.5/4.0，
# 那組數字是給「隨時可能跑很多次」的背景任務用，這裡是偶發、低頻的 rescue。
_DAILY_CAP_USD = 2.0
_MONTHLY_CAP_USD = 10.0

# 16kHz mono 16-bit PCM wav（跟 discord_voice_engine.py 寫出的暫存 wav 格式一致）
# 粗估音訊秒數 → token 數：Gemini 音訊輸入約 32 token/秒（官方文件量級，非精算）。
_PCM_BYTES_PER_SECOND = 16000 * 2
_AUDIO_TOKENS_PER_SECOND = 32
# 單輪 function-calling 輸出短（一組 tool call），保守估 30 token。
_ESTIMATED_OUTPUT_TOKENS = 30


_PROMPT = (
    "你是語音助理的意圖辨識器。使用者剛才說了一段話（見附帶音訊），但既有的規則式"
    "辨識沒有命中任何已知指令。請直接聽音訊判斷使用者真正想做什麼，並呼叫最符合的"
    "一個工具；如果使用者只是在問資訊（例如問現在在放什麼歌、剛剛聊了什麼），改呼叫"
    "對應的查詢工具；如果是需要查證的事實/常識問題（人事時地物、數字、定義），呼叫"
    "factual_question 並把問題整理進 topic。如果聽不出使用者想要哪個已知操作，不要"
    "呼叫任何工具。\n"
    "（STT 轉錄參考，可能有誤：「{query}」）"
)

# 唯讀 tool 執行器 contract：async (tool_name: str, args: dict, ctx: IntentContext) -> str | None
ReadonlyToolExecutor = Callable[[str, dict, IntentContext], Awaitable[str | None]]


class AudioRescueAgent:
    """Audio-native rescue：原始語音 → Gemini function calling → 選定 intent。"""

    name: str = "AudioRescue"
    # 非阻塞 RPM 視窗（獨立 bucket，不跟 STT cleaner 共用計數）。放寬到 20——
    # 只當「真正失控迴圈」的天花板，不是拿來擋正常對話節奏。
    _RPM_LIMIT = 20

    def __init__(
        self,
        *,
        google_client: Any,
        manifest_provider: Callable[[], dict],
        model: str = "gemini-2.5-flash-lite",  # 2.0 系列 2026-08-20 已下架（404），見 llm_pool.py
        # 音訊 + 14-tool function calling 實測（scripts/replay_audio_rescue.py，付費
        # flash-lite）：median 2.3s / p95 3.3s / max 3.6s。3.0s 會砍掉 p95 以上的
        # 成功呼叫 → 白等 3s 又掉回聊天。5.0s 給尾巴留餘裕；rescue 是 regex miss
        # 才走的低頻路徑，這個延遲只影響本來就沒被接住的邊界句。
        timeout_s: float = 5.0,
        readonly_tool_executor: ReadonlyToolExecutor | None = None,
        paid_guard: PaidUsageGuard | None = None,
    ):
        self.google_client = google_client
        self.manifest_provider = manifest_provider
        self.model = model
        self.timeout_s = timeout_s
        self.readonly_tool_executor = readonly_tool_executor
        self.paid_guard = paid_guard if paid_guard is not None else PaidUsageGuard(
            daily_cap_usd=_DAILY_CAP_USD, monthly_cap_usd=_MONTHLY_CAP_USD,
        )
        self._rpm_window: list[float] = []
        # 最近一次 synthesize 放棄的原因（供 IntentBus._maybe_rescue emit「abandoned」
        # outcome——daily ritual 靠 rescue_outcomes.jsonl 看未滿足需求，audio 版只在
        # 成功執行時才寫，放棄路徑靜默丟資料，2026-09-02 追到）。成功時清 None。
        self.last_abandon_reason: str | None = None

    def _abandon(self, reason: str) -> None:
        """記放棄原因並回 None（synthesize 各早退點共用）。"""
        self.last_abandon_reason = reason
        return None

    def _try_acquire_rpm_slot(self) -> bool:
        """非阻塞：RPM 視窗有空位就佔一格回 True，否則回 False（同 gemini_router_llm.py
        的 _try_acquire_cleaner_rpm_slot pattern，但獨立 bucket）。"""
        now = time.time()
        self._rpm_window = [t for t in self._rpm_window if now - t < 60]
        if len(self._rpm_window) < self._RPM_LIMIT:
            self._rpm_window.append(now)
            return True
        return False

    async def synthesize(self, ctx: IntentContext) -> IntentContext | None:
        self.last_abandon_reason = None
        if not ctx.audio_wav_bytes:
            return self._abandon("no_audio")

        manifest = self.manifest_provider()
        action_declarations = manifest_to_function_declarations(manifest)
        if not action_declarations:
            # 沒有任何 opt-in 的 action intent → 只剩唯讀 + 棄權 tool，rescue 無意義
            return self._abandon("no_action_declarations")
        function_declarations = (
            action_declarations
            + READONLY_FUNCTION_DECLARATIONS
            + [ABSTAIN_FUNCTION_DECLARATION]
        )

        est_in = max(1, len(ctx.audio_wav_bytes) // _PCM_BYTES_PER_SECOND * _AUDIO_TOKENS_PER_SECOND)
        if not self.paid_guard.allow(estimate_cost(self.model, est_in, _ESTIMATED_OUTPUT_TOKENS)):
            logger.warning("⚠️ [AudioRescue] 超 daily/monthly paid cap，放棄 rescue")
            return self._abandon("paid_cap")
        if not self._try_acquire_rpm_slot():
            logger.info("📡 [AudioRescue] RPM 視窗已滿，跳過本次 rescue")
            return self._abandon("rpm_full")

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
            return self._abandon("gemini_timeout")
        except Exception as exc:
            logger.warning(f"⚠️ [AudioRescue] Gemini 呼叫失敗，放棄 rescue: {exc}")
            return self._abandon(f"gemini_error:{str(exc)[:80]}")

        usage = getattr(response, "usage_metadata", None)
        in_tokens = int(getattr(usage, "prompt_token_count", 0) or est_in)
        out_tokens = int(getattr(usage, "candidates_token_count", 0) or _ESTIMATED_OUTPUT_TOKENS)
        self.paid_guard.record(
            caller="audio_rescue", model=self.model, tokens=in_tokens + out_tokens,
            est_usd=estimate_cost(self.model, in_tokens, out_tokens),
            in_tokens=in_tokens, out_tokens=out_tokens,
        )

        calls = getattr(response, "function_calls", None) or []
        if not calls:
            return self._abandon("no_tool_calls")

        intent_agents = intent_to_agent_map(manifest)  # Gemini 掉前綴時的 fallback
        resolved: tuple[str, str, dict] | None = None
        for call in calls:
            if call.name == ABSTAIN_TOOL_NAME:
                # LLM 明確判斷「以上都不是」——直接放棄，走一般聊天，不理其他 call。
                logger.info("📡 [AudioRescue] LLM 選了 just_chatting，放棄 rescue")
                return self._abandon("just_chatting")
            if call.name in READONLY_TOOL_NAMES:
                await self._execute_readonly(call, ctx)
                continue
            if resolved is not None:
                logger.info(
                    f"📡 [AudioRescue] 多個 action tool call，只取第一個，"
                    f"忽略: {call.name}"
                )
                continue
            parsed = parse_tool_call(call, intent_agents)
            if parsed is not None:
                resolved = parsed
            else:
                logger.warning(f"⚠️ [AudioRescue] tool call name 無法對應，忽略: {call.name}")

        if resolved is None:
            return self._abandon("tool_call_unresolvable")

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
