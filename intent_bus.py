"""
IntentBus — wake 之後的意圖路由 (Phase 1)。

把原本 _process_queued_query 的 if/elif fast-track chain 轉成顯式廣播：
  1. 每個 IntentAgent 看 IntentContext，回 Bid(confidence, handler) 或 None
  2. Bus collect 所有 bids → 取最高 confidence
  3. 若最高 < MIN_CONFIDENCE → 沒人接，caller 自處
  4. 否則 await winner.handler() 執行

設計刻意：
- bid() sync + fast (≤5ms)：禁止 LLM 呼叫 / I/O；昂貴判斷放 handler 內
- IntentContext frozen：agent 不能誤改 state
- agent 例外不傳染：一個 agent 炸，其他繼續 bid
- handler 例外往上拋：由 caller 決定要不要兜底
- 同分穩定排序：取第一個註冊的，方便 debug
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Awaitable, Callable, Protocol

# 用 cogs.voice_controller 的子 logger，繼承 main_discord.py 設的 INFO level；
# 否則 root logger 是 WARNING，INFO 級的 dispatch log 會全部被吞掉。
logger = logging.getLogger("cogs.voice_controller.intent_bus")


@dataclass(frozen=True)
class IntentContext:
    """Wake event 的全部 context，傳給每個 agent 看。

    Frozen 是防呆 — agent 不能在 bid() 內 mutate state。

    `mode` 表示當前 bot 模式（normal / game / stream）。Agent 用
    `DeclarativeIntentAgent.mode_compatible` 自我聲明能在哪些模式下出價，
    base class 的 gate() 會自動依 `ctx.mode` 早退。Phase-2 之前 game_mode
    bool + stream_active bool 同時存在；mode 字串是新統一介面，預設 "normal"
    保持後向相容。
    """
    speaker: str
    raw_text: str
    query: str
    original_raw: str | None
    wake_intent: float | None
    stream_active: bool
    game_mode: bool
    is_owner: bool
    now: float
    mode: str = "normal"
    # vector intent re-dispatch 鏈深度；bus 在 missing_slots resolve 後注入 depth+1。
    # resolver 自己用 depth>=MAX_REWRITE_DEPTH 守無窮迴圈，這裡只負責往下傳。
    depth: int = 0


@dataclass
class Bid:
    name: str
    confidence: float           # 0.0–1.0
    handler: Callable[[], Awaitable[None]]
    reason: str = ""
    # Alexa CanFulfillIntent 概念：agent 知道自己缺哪些 slot 還能出價但需要 follow-up。
    # 由 agent 自行填；空 list 表示 self-contained 不需追問。observability + handler 路由依據。
    missing_slots: list[str] = field(default_factory=list)


class IntentAgent(Protocol):
    name: str
    def bid(self, ctx: IntentContext) -> Bid | None: ...


class IntentBus:
    MIN_CONFIDENCE = 0.30
    # bid() 預算：> 5ms 觸發 WARNING，守住 sync ≤5ms 契約不漂移（5/18 NotebookLM review）
    _BID_BUDGET_MS = 5.0

    def __init__(self, agents: list[IntentAgent], *,
                 resolver=None, profile_provider=None, llm_fallback=None,
                 recommendation_sink=None, direct_probe=None):
        self.agents = list(agents)
        self.logger = logger
        # vector intent 接線（全 optional，現有 prod bus 只傳 agents 不受影響）：
        # - resolver: SemanticResolver，補 missing_slots 的 song_choice / directional_resolution
        # - profile_provider: speaker → SpeakerProfile（cache lookup；缺則建最小 profile）
        # - llm_fallback: async (ctx) → ...，resolver 放棄時的 Marvin 兜底
        # - recommendation_sink: (slot, ctx, resolved) → ...，resolve 成功時記推薦事件
        #   （offline feedback batch 用）。同步 callback，bus try/except 包好不斷 wake path。
        # - direct_probe: async (query) → truthy/falsy，song_choice 缺槽時的 yt-dlp 直查捷徑。
        #   命中（truthy）就跳過 resolver 直接 winner.handler()；falsy / 例外 → 走原 resolver 路徑。
        #   只對 song_choice 生效——directional_resolution 是 user 明確要 LLM 解析，不該被短路。
        self.resolver = resolver
        self.profile_provider = profile_provider
        self.llm_fallback = llm_fallback
        self.recommendation_sink = recommendation_sink
        self.direct_probe = direct_probe
        # build_intent_manifest() 的 per-day cache；intent gap classifier 用。
        # invalidate key = ISO date string；agent list 一天內不變更（service restart 才會）。
        self._manifest_cache: dict | None = None

    def build_intent_manifest(self, today: str | None = None) -> dict:
        """蒐集所有 DeclarativeIntentAgent 的能力地圖，給 gap classifier 看。

        排除規則（不入 manifest）：
        - 沒 declare_intents() method（NemoClawAgent 等裸 class）
        - declare_intents() 回 []（state-checking agent，如 busted / turtle）
        - declare_intents() 拋例外（對齊 bus dispatch：一個炸不影響其他）

        Cache：同 ISO date 重用同一 dict（每日 invalidate）。
        """
        today = today or date.today().isoformat()
        if self._manifest_cache is not None and self._manifest_cache["version"] == today:
            return self._manifest_cache

        agents_entries: list[dict] = []
        for agent in self.agents:
            if not hasattr(agent, "declare_intents"):
                continue
            try:
                schemas = agent.declare_intents()
            except Exception as exc:
                self.logger.warning(
                    f"⚠️ [IntentBus] {getattr(agent, 'name', '?')} declare_intents() "
                    f"炸了，manifest 跳過: {exc}"
                )
                continue
            if not schemas:
                continue
            agents_entries.append({
                "name": getattr(agent, "name", "?"),
                "intents": [
                    {
                        "name": s.name,
                        "required_slots": list(s.required_slots),
                        "reason_template": s.reason_template,
                    }
                    for s in schemas
                ],
            })

        self._manifest_cache = {"version": today, "agents": agents_entries}
        return self._manifest_cache

    async def dispatch(self, ctx: IntentContext) -> Bid | None:
        """收 bids、選 winner、await handler。回傳 winner Bid（or None 如果沒人 above threshold）。"""
        bids: list[Bid] = []
        for agent in self.agents:
            t0 = time.perf_counter()
            try:
                b = agent.bid(ctx)
            except Exception as exc:
                self.logger.warning(
                    f"⚠️ [IntentBus] {getattr(agent, 'name', '?')} bid() 炸了，跳過: {exc}"
                )
                continue
            elapsed_ms = (time.perf_counter() - t0) * 1000
            if elapsed_ms > self._BID_BUDGET_MS:
                self.logger.warning(
                    f"⚠️ [IntentBus] {getattr(agent, 'name', '?')} bid() took {elapsed_ms:.1f}ms "
                    f"(>{self._BID_BUDGET_MS:.0f}ms 預算) — 違反 sync 契約，檢查是否摸到 I/O"
                )
            if b is not None:
                bids.append(b)

        def _fmt(b: Bid) -> str:
            tail = f" missing={'+'.join(b.missing_slots)}" if b.missing_slots else ""
            return f"{b.name}={b.confidence:.2f}({b.reason}){tail}"
        bid_summary = ", ".join(_fmt(b) for b in bids) or "no_bids"

        if not bids:
            self.logger.info(
                f"📡 [IntentBus] speaker={ctx.speaker} query='{ctx.query[:50]}' "
                f"wake_intent={ctx.wake_intent} bids: {bid_summary} winner=none"
            )
            return None

        # 同分取第一個（list.sort 是 stable）— 從 max() 改用 sort 確保穩定
        bids.sort(key=lambda b: b.confidence, reverse=True)
        winner = bids[0]

        # tie collision warning：winner 與第二名同分 → 曝光隱式註冊順序 tie-break
        # （5/18 NotebookLM review；目前靠註冊順序贏，但這是隱式行為，未來加 agent 易踩）
        if len(bids) > 1 and bids[1].confidence == winner.confidence:
            colliders = [b for b in bids if b.confidence == winner.confidence]
            collider_names = ", ".join(b.name for b in colliders)
            self.logger.warning(
                f"⚠️ [IntentBus] tie collision @ conf={winner.confidence:.2f} "
                f"between [{collider_names}]; picked {winner.name} (註冊順序)"
            )

        if winner.confidence < self.MIN_CONFIDENCE:
            self.logger.info(
                f"📡 [IntentBus] speaker={ctx.speaker} query='{ctx.query[:50]}' "
                f"wake_intent={ctx.wake_intent} bids: {bid_summary} "
                f"winner=none (max={winner.confidence:.2f}<{self.MIN_CONFIDENCE})"
            )
            return None

        self.logger.info(
            f"📡 [IntentBus] speaker={ctx.speaker} query='{ctx.query[:50]}' "
            f"wake_intent={ctx.wake_intent} bids: {bid_summary} winner={winner.name}"
        )

        # ── Vector intent：winner 缺 resolver 認得的 slot → 解析後帶 depth+1 重投 ──
        # 不認得的 slot（如 song_title）或無 missing → 走原 handler（保留 _ask / 直接播）。
        slot = winner.missing_slots[0] if winner.missing_slots else None
        if slot and self.resolver is not None and self.resolver.handles(slot):
            # song_choice 短路：yt-dlp 直查命中就跳過 curation，避免「播放七里香」被 LLM
            # 誤當歌手解析。directional_resolution（抽象修飾）保留原路徑，不短路。
            if slot == "song_choice" and self.direct_probe is not None:
                try:
                    hit = await self.direct_probe(ctx.query)
                except Exception as exc:
                    self.logger.warning(
                        f"⚠️ [IntentBus] direct_probe 炸了，fall through 到 resolver: {exc}"
                    )
                    hit = None
                if hit:
                    self.logger.info(
                        f"📡 [IntentBus] direct_probe hit '{ctx.query[:30]}' → "
                        f"跳過 curation，直接 handler"
                    )
                    await winner.handler()
                    return winner
            return await self._resolve_and_redispatch(slot, ctx)

        await winner.handler()
        return winner

    async def _resolve_and_redispatch(self, slot: str, ctx: IntentContext) -> Bid | None:
        """Resolve missing slot → 帶 depth+1 重投 bus；resolver 放棄則 Marvin 兜底。"""
        from intent_agents.semantic_resolver import SpeakerProfile
        from dataclasses import replace

        profile = (self.profile_provider(ctx.speaker) if self.profile_provider
                   else SpeakerProfile(speaker=ctx.speaker))
        resolved = await self.resolver.resolve(slot, ctx.query, profile, depth=ctx.depth)

        if resolved is not None:
            new_ctx = replace(ctx, query=resolved.rewritten_query, depth=resolved.depth)
            self.logger.info(
                f"📡 [IntentBus] resolve {slot}: '{ctx.query[:30]}' → "
                f"'{resolved.rewritten_query[:30]}' depth={resolved.depth}"
            )
            # 記推薦事件供 offline feedback batch；同步、永不斷 wake path
            if self.recommendation_sink is not None:
                try:
                    self.recommendation_sink(slot, ctx, resolved)
                except Exception as exc:
                    self.logger.warning(f"⚠️ [IntentBus] recommendation_sink 炸了，略過: {exc}")
            return await self.dispatch(new_ctx)

        # resolver 放棄（depth≥MAX / 失敗 / 無 client）→ Marvin LLM 兜底
        self.logger.info(
            f"📡 [IntentBus] resolve {slot} 回 None（depth={ctx.depth}）→ "
            f"{'Marvin 兜底' if self.llm_fallback else '無 fallback，drop'}"
        )
        if self.llm_fallback is not None:
            await self.llm_fallback(ctx)
        return None
