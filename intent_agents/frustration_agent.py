"""FrustrationAgent — 捕捉使用者挫折、重複嘗試與不滿信號，直通 Audio LLM 救援。

設計背景（2026-08-25 實機踩到）：
使用者多次點播因併發或 VAD 造成語句重疊污染（如「把文文播放馬文播放張宇的文播放張宇的傘下」），
導致既有文字 Regex 與 Cleaner 雙雙判 0.00 造成 Music Drop 靜默，引發使用者強烈挫折。
此 Agent 在偵測到挫折字眼或口吃重試特徵時，直接將原始音訊 WAV 導流給 Audio LLM
（Gemini Audio Function Calling）解讀真實語音意圖並執行。
"""
from __future__ import annotations

import logging
import re
from dataclasses import replace
from typing import Any

from intent_bus import Bid, IntentContext

logger = logging.getLogger("cogs.voice_controller.intent_agents.frustration")

# 挫折 / 質疑 / 抱怨關鍵字
_FRUSTRATION_RE = re.compile(
    r'(?:到底.*(?:聽|播|換|停|說|叫|有沒|有沒有|壞了|死機|要講|講幾遍)'
    r'|有沒有在聽|聽不懂|你在幹嘛|叫你幾次|到底要講幾遍|有沒有聽到'
    r'|不對.*(?:這首|這個|放錯|播錯)|不是這首|放錯了|播錯了|不是這個'
    r'|換掉啦|搞什麼|閉嘴啦)',
    re.IGNORECASE,
)

# 口吃重複特徵（同一句出現多次點播/喚醒標記）
_STUTTER_WAKE_WORDS = ("播放", "馬文", "把文", "毛文", "放歌", "麻文")


async def _noop() -> None:
    pass


class FrustrationAgent:
    """使用者挫折與口吃重試意圖救援 Agent。"""

    name: str = "frustration"

    def __init__(
        self,
        controller: Any = None,
        audio_rescue_agent: Any = None,
        intent_bus: Any = None,
    ):
        self.ctrl = controller
        self._audio_rescue_agent = audio_rescue_agent
        self._intent_bus = intent_bus

    @property
    def rescue_agent(self) -> Any | None:
        if self._audio_rescue_agent is not None:
            return self._audio_rescue_agent
        if self.ctrl and hasattr(self.ctrl, "_rescue_agent") and self.ctrl._rescue_agent is not None:
            return self.ctrl._rescue_agent
        # 如果 controller 沒有建立全域 rescue_agent，但有 google_client，動態建立一個
        if self.ctrl and hasattr(self.ctrl, "bot") and hasattr(self.ctrl.bot, "router"):
            gclient = getattr(self.ctrl.bot.router, "google_client", None)
            if gclient is not None and self.bus is not None:
                from intent_agents.audio_rescue_agent import AudioRescueAgent
                self._audio_rescue_agent = AudioRescueAgent(
                    google_client=gclient,
                    manifest_provider=self.bus.build_intent_manifest,
                )
                return self._audio_rescue_agent
        return None

    @property
    def bus(self) -> Any | None:
        if self._intent_bus is not None:
            return self._intent_bus
        if self.ctrl and hasattr(self.ctrl, "_intent_bus"):
            return self.ctrl._intent_bus
        return None

    def bid(self, ctx: IntentContext) -> Bid:
        """Sync ≤5ms 競標：無 I/O，偵測挫折詞或口吃重試特徵。"""
        # 沒有原始音訊 → 無法進行 Audio LLM 救援，放棄
        if not ctx.audio_wav_bytes:
            return Bid(name=self.name, confidence=0.0, handler=_noop, reason="no_audio")

        query = ctx.query or ""

        # 1. 顯式挫折/質疑/抱怨 pattern
        # 挫折句本身（如「到底要講幾遍」）通常不含歌名/意圖線索——真正該救援的
        # 是「挫折產生之前」那輪失敗嘗試的音訊，而非這句抱怨自己的音訊。有
        # prev_turn_audio_wav_bytes 快照就優先用它，沒有才退回當輪音訊。
        if _FRUSTRATION_RE.search(query):
            return Bid(
                name=self.name,
                confidence=0.92,
                handler=lambda: self._handle_rescue(
                    replace(ctx, audio_wav_bytes=ctx.prev_turn_audio_wav_bytes or ctx.audio_wav_bytes)
                ),
                reason="frustration_pattern",
            )

        # 2. 口吃/連喊重試特徵（例如「把文文播放馬文播放...」）
        stutter_count = sum(query.count(w) for w in _STUTTER_WAKE_WORDS)
        if stutter_count >= 2 or query.count("播放") >= 2 or query.count("馬文") >= 2:
            return Bid(
                name=self.name,
                confidence=0.91,
                handler=lambda: self._handle_rescue(ctx),
                reason="stutter_repetition",
            )

        return Bid(name=self.name, confidence=0.0, handler=_noop, reason="no_frustration")

    async def _handle_rescue(self, ctx: IntentContext) -> None:
        """將原始語音送 Audio LLM 解讀，並執行解讀後的目標 Agent handler。"""
        logger.info(f"🚨 [FrustrationAgent] 觸發原始語音救援: query='{ctx.query[:60]}'")

        agent = self.rescue_agent
        if agent is None:
            logger.warning("⚠️ [FrustrationAgent] audio_rescue_agent 未能解析或未就緒，跳過")
            return

        try:
            rescued_ctx = await agent.synthesize(ctx)
        except Exception as exc:
            logger.error(f"❌ [FrustrationAgent] audio_rescue_agent 執行失敗: {exc}", exc_info=True)
            return

        if rescued_ctx is None or not rescued_ctx.resolved_agent or not rescued_ctx.resolved_intent:
            logger.info("📡 [FrustrationAgent] Audio LLM 未能解析出已知意圖")
            if self.ctrl and hasattr(self.ctrl, "play_tts"):
                try:
                    await self.ctrl.play_tts("抱歉剛才沒聽清楚，可以再說一次嗎？")
                except Exception:
                    pass
            return

        # 找到對應的 Target Agent 並執行
        bus = self.bus
        target_agent = None
        if bus and hasattr(bus, "agents"):
            target_agent = next(
                (a for a in bus.agents if getattr(a, "name", None) == rescued_ctx.resolved_agent),
                None,
            )

        if target_agent is None or not hasattr(target_agent, "resolve_intent"):
            logger.warning(
                f"⚠️ [FrustrationAgent] 目標 Agent={rescued_ctx.resolved_agent} 找不到或不支援 resolve_intent"
            )
            return

        try:
            bid = target_agent.resolve_intent(
                rescued_ctx.resolved_intent,
                rescued_ctx.resolved_slots or {},
                rescued_ctx,
            )
        except Exception as exc:
            logger.error(f"❌ [FrustrationAgent] resolve_intent 執行失敗: {exc}", exc_info=True)
            return

        if bid and bid.handler:
            logger.info(
                f"✅ [FrustrationAgent] 成功挽回意圖: {rescued_ctx.resolved_agent}.{rescued_ctx.resolved_intent} "
                f"slots={rescued_ctx.resolved_slots}"
            )
            await bid.handler()
