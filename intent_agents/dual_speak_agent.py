"""DualSpeakAgent — Marmo 一搭一唱 PoC 入口 agent（Template B state-checking）。

Trigger source: marmo_server HTTP webhook 透過 IntentBus.dispatch(ctx) 注入，
ctx.dispatch_source == "marmo_inject" + ctx.payload 帶 marmo_text。

Pattern：Marvin 跑題 + Marmo 代用戶打斷（design doc § Pattern 雛形）。
順序 [marvin, marmo] 強制 in services/dialogue_generation.py，agent 不管。

Backpressure：tts_queue_duration > 10s 直接 dense 0.0 不入隊（CLAUDE.md 風暴保護）。

Failure fallback：generate_dual_dialogue 回 None（LLM 例外 / parse 失敗 / 紅線命中）
→ handler 改播單 Marvin TTS 原 marmo_text（preserve 現有 marmo_server 體驗連續性）。
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from intent_agents.base import DeclarativeIntentAgent, IntentSchema
from intent_bus import Bid, IntentContext
from services.dialogue_generation import generate_dual_dialogue

logger = logging.getLogger("MarvinBot.DualSpeakAgent")

# Backpressure 閾值：> 10s 拒接（嚴格大於；== 10 仍接，符合「TTS queue 滿了就 drop」常識）
_TTS_QUEUE_OVERLOAD_S = 10.0

LLMFn = Callable[[str, str], Awaitable[str]]


class DualSpeakAgent(DeclarativeIntentAgent):
    """Bids 0.95 when marmo_server injects a dual_speak intent and TTS queue not in storm."""

    name = "dual_speak"
    mode_compatible = frozenset({"normal", "stream"})

    def __init__(self, *, bot, llm_fn: LLMFn):
        self.bot = bot
        self.llm_fn = llm_fn

    # Template B：state-checking agent，不走 declarative schema
    def declare_intents(self) -> list[IntentSchema]:
        return []

    def _get_vc(self):
        if self.bot is None:
            return None
        return self.bot.cogs.get("VoiceController")

    # ── Custom bid (Template B：override，不走 base.bid 的 regex 路徑) ──────
    def bid(self, ctx: IntentContext) -> Bid:
        # 1. Mode gate（override bid 必須手動檢，因 base.bid 才做這件事）
        if ctx.mode not in self.mode_compatible:
            return self._dense_zero(f"mode_mismatch:{ctx.mode}")

        # 2. Source gate：只接 marmo_server 注入
        if ctx.dispatch_source != "marmo_inject":
            return self._dense_zero("not_marmo_inject")

        # 3. Payload gate
        payload = ctx.payload or {}
        marmo_text = (payload.get("text") or "").strip()
        if not marmo_text:
            return self._dense_zero("missing_payload")

        # 4. VC gate
        vc = self._get_vc()
        if vc is None:
            return self._dense_zero("vc_not_loaded")

        # 5. Backpressure gate（守 CLAUDE.md TTS storm 規則）
        queue_dur = getattr(vc, "tts_queue_duration", 0.0)
        if queue_dur > _TTS_QUEUE_OVERLOAD_S:
            return self._dense_zero("backpressure_tts_storm")

        # pattern：webhook 預設 marmo_lead；payload 可帶 "pattern" override（測試/debug
        # 後門，讓 Case B marvin_lead 也能用 webhook POST 聽到）。非法值 fallback marmo_lead。
        pattern = payload.get("pattern")
        if pattern not in ("marvin_lead", "marmo_lead"):
            pattern = "marmo_lead"

        # interject：payload 帶 interject=true → 打岔疊播（Plan12 mixer），手動測用
        interject = bool(payload.get("interject"))
        # raw_segments：payload 帶現成 segments → 跳過 LLM 生成、直接播（測播放/打岔不用重生成）
        raw_segments = payload.get("segments")

        # ── Happy path：build handler closure ─────────────────────────────
        async def _handler():
            await self._handle(vc=vc, marmo_text=marmo_text, pattern=pattern,
                               interject=interject, raw_segments=raw_segments)

        return Bid(
            name=self.name,
            confidence=0.95,
            handler=_handler,
            reason=f"dual_speak:job_id={payload.get('job_id', '?')[:12]}:{pattern}",
        )

    async def _handle(self, *, vc, marmo_text: str, pattern: str = "marmo_lead",
                      interject: bool = False, raw_segments=None) -> None:
        """Handler 內：呼叫 LLM 生對白、成功播雙段、失敗 fallback 單 Marvin。

        webhook 預設 = Marmo 主動報事 → marmo_lead [marmo, marvin]。
        payload 帶 pattern="marvin_lead" → Case B [marvin, marmo]（測試後門）。
        raw_segments 有值 → 跳過 LLM 生成、直接播這組（測播放/打岔用，不重生成）。
        """
        if raw_segments:
            segments = raw_segments
            logger.info(f"[DualSpeak] 用現成 segments 播放（跳過生成），{len(raw_segments)} 段")
        else:
            try:
                segments = await generate_dual_dialogue(
                    content_text=marmo_text,
                    llm_fn=self.llm_fn,
                    pattern=pattern,
                )
            except Exception as exc:
                # 防禦性：generate_dual_dialogue 已 catch 內部例外回 None，但保險
                logger.warning(f"[DualSpeak] generate_dual_dialogue 拋例外: {exc}")
                segments = None

        if segments is None:
            # Fallback：drop dual、原 marmo_text 走單 Marvin（preserve marmo_server 體驗連續性）
            logger.info(f"[DualSpeak] fallback 單 Marvin TTS: '{marmo_text[:50]}'")
            try:
                await vc.play_tts(marmo_text, already_in_channel=True)
            except Exception as exc:
                logger.warning(f"[DualSpeak] fallback play_tts 失敗: {exc}")
            return

        # Happy path：播雙段
        try:
            await vc.play_dual_dialogue(segments, interject=interject)
        except Exception as exc:
            logger.warning(f"[DualSpeak] play_dual_dialogue 失敗: {exc}")
