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
    mode: str = "normal"  # "normal" / "stream" / "game" / "radio"; bus 用此 gate
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
    # mode_compatible: agent 宣告可在哪些 voice mode 下發話。Bus 在 tick 時統一過濾。
    # 缺此屬性 → bus.register 立即 raise（防 silent failure，不會「忘了就永遠不發」）。
    # 一般 agent: {"normal"}；願意在背景音樂中插短句的: {"normal", "stream"}；
    # Game-only 應 {"game"} — agent 自己不再 ad-hoc if-stream/radio/game 檢查。
    mode_compatible: frozenset[str]

    async def speak_bid(self, ctx: SpeakContext) -> SpeakBid: ...


# ── bus ──────────────────────────────────────────────────────────────────────


class SpeakBus:
    MIN_CONFIDENCE = 0.30

    def __init__(self) -> None:
        self._agents: dict[str, SpeakAgent] = {}
        self._multiplier: float = 1.0
        self._multiplier_expiry: float = 0.0
        # 最近一次 tick 被 mode_mismatch 過濾的 agent 名單；voice_controller 寫 outcome
        # log 時讀這份，把「bus 跑完沒人贏」翻成 visible 訊號。
        self._last_filtered: tuple[str, ...] = ()
        # 已印過 traceback 的 (agent, 例外)：首次帶 exc_info 方便定位，重複壓縮
        # （2026-06-12：MemoryCallbackAgent 'str' object 炸 1147 次/小時只留 str(e) 無法追因）
        self._seen_bid_errors: set[tuple[str, str]] = set()

    # ── registration ─────────────────────────────────────────────────────────

    def register(self, agent: SpeakAgent) -> None:
        """同名取代，避免重複 bid。

        強制檢查 agent.mode_compatible 存在且非空（防 silent failure，不允許
        漏宣告就跑壞）。漏宣告或宣告空集合 → 啟動就 raise，不會 silently 不發。
        """
        mode_compat = getattr(agent, "mode_compatible", None)
        if mode_compat is None:
            raise TypeError(
                f"SpeakAgent {agent.name!r} 缺 mode_compatible 屬性"
                f"（必須是 frozenset[str]）；漏宣告會 silent 不發話"
            )
        if not mode_compat:
            raise ValueError(
                f"SpeakAgent {agent.name!r} 的 mode_compatible 為 empty"
                f"（agent 在任何模式都不發話 = 註冊它沒意義）"
            )
        self._agents[agent.name] = agent

    def last_filtered_by_mode(self) -> tuple[str, ...]:
        """最近一次 tick 因 mode_mismatch 被過濾的 agent 名稱。給 outcome log 寫稽核用。"""
        return self._last_filtered

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
        """收所有 agent 的 bid，套 multiplier，回最高分（≥ MIN_CONFIDENCE）。

        Mode gate：ctx.mode 不在 agent.mode_compatible 內 → 跳過 speak_bid 呼叫，
        agent 名字記到 _last_filtered 給 outcome log 寫稽核（防 silent 不發話）。
        """
        if not self._agents:
            self._last_filtered = ()
            return None

        mult = self.get_global_multiplier()
        bids: list[SpeakBid] = []
        filtered: list[str] = []
        for name, agent in list(self._agents.items()):
            if ctx.mode not in agent.mode_compatible:
                filtered.append(name)
                continue
            try:
                bid = await agent.speak_bid(ctx)
            except Exception as e:
                err_key = (name, f"{type(e).__name__}:{e}")
                first_time = err_key not in self._seen_bid_errors
                self._seen_bid_errors.add(err_key)
                logger.warning(
                    "[SpeakBus] %s.speak_bid raised: %s", name, e,
                    exc_info=first_time or None,
                )
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

        self._last_filtered = tuple(filtered)
        if not bids:
            return None
        bids.sort(key=lambda b: b.confidence, reverse=True)
        return bids[0]
