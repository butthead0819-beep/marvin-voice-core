"""J1 RegexJudge — pure sync regex schema match over declarative agents.

跑 agents 的 bid() 在 raw STT 文字上（沒過 cleaner），回最高 confidence 的 Bid。
race coordinator 用 confidence 閾值判要不要直接 dispatch（≥0.95 → 跳過 J2/J3）。

bid() 自帶 mode_compatible / gate / post_match_filter，這裡完全重用，不另寫 regex 邏輯。
單 agent exception 不汙染其他 agent（race 必須對所有 judge 都 robust）。
"""
from __future__ import annotations

from intent_bus import Bid, IntentAgent, IntentContext

_JUDGE_NAME = "regex_judge"


async def _noop() -> None:
    pass


def _miss(reason: str) -> Bid:
    return Bid(name=_JUDGE_NAME, confidence=0.0, handler=_noop, reason=reason)


def regex_judge(ctx: IntentContext, agents: list[IntentAgent]) -> Bid:
    """跑所有 agents.bid(ctx)，回最高 confidence 的 winning Bid。

    Miss / empty / 全 dense-zero → 回 dense-zero Bid(confidence=0.0)，
    race coordinator 看 confidence 決定要不要等下一路 judge。
    """
    if not (ctx.query or "").strip():
        return _miss("empty_query")

    best: Bid | None = None
    for agent in agents:
        try:
            bid = agent.bid(ctx)
        except Exception:
            continue
        if bid is None:
            continue
        if best is None or bid.confidence > best.confidence:
            best = bid

    if best is None or best.confidence == 0.0:
        return _miss("no_agent_matched")
    return best
