"""
NemoClawAgent — owner 用「龍蝦」喚醒詞直送 NemoClaw 子系統。

Confidence 規約：
  0.95 — owner + 龍蝦 regex 命中 (high-confidence direct trigger)
  None — 非 owner / low confidence wake / 無龍蝦詞

Smart router 路徑（LLM 判斷 query 是否該走 NemoClaw）不在 bid() 內處理
（需要 async LLM call，違反 bid sync 規則）。改由 MarvinAgent 在 handler
內自己決定要不要 fallback 到 NemoClaw。
"""
from __future__ import annotations

import re

from intent_bus import Bid, IntentContext


# 跟 cogs/voice_controller.py:88 _NEMOCLAW_RE 同步維護
# (重複定義避免 controller import 循環依賴；Phase 1 接受 drift 風險，
#  未來抽到共用 module)
_LOBSTER_RE = re.compile(r'龍蝦|lobster', re.IGNORECASE)


class NemoClawAgent:
    name = "nemoclaw"
    # owner 講「龍蝦」在遊戲中不該觸發 NemoClaw，讓 game agent 接答案 → gate 掉 game。
    mode_compatible = frozenset({"normal", "stream"})
    # 0.65 對齊 LLM veto 閾值；NemoClawAgent 額外有 owner-only + 龍蝦 regex
    # 兩道防誤觸發，0.80 過於保守（owner 講 「龍蝦」 wake_intent 0.7 不該被擋）。
    LOW_WAKE_THRESHOLD = 0.65

    def __init__(self, controller):
        self.ctrl = controller

    def bid(self, ctx: IntentContext) -> Bid | None:
        if ctx.mode not in self.mode_compatible:
            return None  # 遊戲模式不接管
        if not ctx.is_owner:
            return None
        if ctx.wake_intent is not None and ctx.wake_intent < self.LOW_WAKE_THRESHOLD:
            return None

        # 先看 original_raw (保留喚醒詞)，沒就退到 query
        text = ctx.original_raw or ctx.query
        if not _LOBSTER_RE.search(text):
            return None

        return Bid(
            name=self.name,
            confidence=0.95,
            handler=lambda: self.ctrl._handle_nemoclaw_query(ctx.speaker, ctx.query),
            reason="lobster_trigger",
        )
