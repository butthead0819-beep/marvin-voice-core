"""SpeakBus — proactive bid 架構（主動發話）。

平行於 IntentBus（reactive，被動派發 intent）。SpeakBus 由 timer / event 觸發，
讓 agent 主動爭取「該不該開口、開什麼口」。

設計來源：docs/social_catalyst_plan.md（Week 1 基建）。

呼叫者：
  - voice_controller idle loop（每 5s）
  - 一句話講完後 2s（給 BridgeAgent callback 機會）
  - mood transition（MoodAgent 觸發其他 agent 重新 bid）

不變式：
  - DuckingAgent 不繼承 SpeakAgent，它透過 set_global_multiplier 壓制
  - 抓任何 agent.speak_bid() 例外，不傳播
  - 同名 register 取代（不重複 bid）
  - 得標 bid 的 confidence 已套 multiplier（caller 看到的是 effective 值）
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


# ── data types ───────────────────────────────────────────────────────────────


@dataclass
class SpeakContext:
    channel_id: int
    guild_id: int
    silence_seconds: float
    present_speakers: list[str]
    room_mood: object | None  # RoomMoodState (forward decl 避免循環 import)
    recent_utterances: list[dict]
    trigger: str  # "idle_tick" / "post_utterance" / "mood_transition"
    last_speaker: str | None = None
    last_text: str | None = None


@dataclass
class SpeakBid:
    agent_name: str
    confidence: float
    handler: Callable[[], Awaitable[None]]
    reason: str
    ttl_s: float = 30.0


@runtime_checkable
class SpeakAgent(Protocol):
    name: str

    async def speak_bid(self, ctx: SpeakContext) -> SpeakBid: ...


# ── bus ──────────────────────────────────────────────────────────────────────


class SpeakBus:
    MIN_CONFIDENCE = 0.30

    def __init__(self) -> None:
        self._agents: dict[str, SpeakAgent] = {}
        self._multiplier: float = 1.0
        self._multiplier_expiry: float = 0.0

    # ── registration ─────────────────────────────────────────────────────────

    def register(self, agent: SpeakAgent) -> None:
        """同名取代，避免重複 bid。"""
        self._agents[agent.name] = agent

    def unregister(self, name: str) -> None:
        self._agents.pop(name, None)

    def agents(self) -> list[str]:
        return list(self._agents.keys())

    # ── multiplier (DuckingAgent 控制) ───────────────────────────────────────

    def set_global_multiplier(self, m: float, ttl_s: float = 60.0) -> None:
        """壓制所有 SpeakBid 信心。0.0 = 全關，1.0 = 原狀。TTL 過後回 1.0。"""
        self._multiplier = max(0.0, min(1.0, float(m)))
        self._multiplier_expiry = time.time() + max(0.0, float(ttl_s))

    def get_global_multiplier(self) -> float:
        if time.time() >= self._multiplier_expiry:
            self._multiplier = 1.0
            self._multiplier_expiry = 0.0
        return self._multiplier

    # ── tick ─────────────────────────────────────────────────────────────────

    async def tick(self, ctx: SpeakContext) -> SpeakBid | None:
        """收所有 agent 的 bid，套 multiplier，回最高分（≥ MIN_CONFIDENCE）。"""
        if not self._agents:
            return None

        mult = self.get_global_multiplier()
        bids: list[SpeakBid] = []
        for name, agent in list(self._agents.items()):
            try:
                bid = await agent.speak_bid(ctx)
            except Exception as e:
                logger.warning("[SpeakBus] %s.speak_bid raised: %s", name, e)
                continue
            if bid is None:
                continue
            effective = bid.confidence * mult
            if effective < self.MIN_CONFIDENCE:
                continue
            bids.append(SpeakBid(
                agent_name=bid.agent_name,
                confidence=effective,
                handler=bid.handler,
                reason=bid.reason,
                ttl_s=bid.ttl_s,
            ))

        if not bids:
            return None
        bids.sort(key=lambda b: b.confidence, reverse=True)
        return bids[0]
