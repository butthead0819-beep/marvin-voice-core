"""Parallel judges race coordinator.

多 judge 並行跑，第一個 Bid.confidence ≥ 自己 threshold 的 winner，cancel 其他。
全部完成都沒人 fast-path → 取整體最高 confidence。timeout → 回 timeout 前最高分；
沒人完成 → dense zero。

判 winner 的 threshold 是 per-judge（J1 規範 0.90、J2 0.80、J3 0.30 之類）。
race 本身不知道 judge 名字代表什麼，只負責協調。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable

from intent_bus import Bid, IntentContext

_RACE_NAME = "judges_race"


@dataclass(frozen=True)
class JudgeSpec:
    """一個 judge 的 race 規格：coroutine + fast-path threshold。"""
    judge: Callable[[IntentContext], Awaitable[Bid]]
    threshold: float


async def _async_noop() -> None:
    pass


def _dense_zero(reason: str) -> Bid:
    return Bid(name=_RACE_NAME, confidence=0.0, handler=_async_noop, reason=reason)


async def _drain_pending(pending: set[asyncio.Task]) -> None:
    """Cancel pending tasks 並 await 它們 settle（不漏 task）。"""
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


async def race(
    ctx: IntentContext,
    specs: list[JudgeSpec],
    *,
    timeout_s: float = 5.0,
) -> Bid:
    if not specs:
        return _dense_zero("no_judges")

    tasks: dict[asyncio.Task, JudgeSpec] = {}
    for spec in specs:
        task = asyncio.create_task(spec.judge(ctx))
        tasks[task] = spec

    completed_bids: list[Bid] = []
    winner: Bid | None = None
    pending: set[asyncio.Task] = set(tasks.keys())
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_s

    while pending and winner is None:
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        done, pending = await asyncio.wait(
            pending, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
        )
        if not done:
            # timeout this round
            break
        for task in done:
            spec = tasks[task]
            try:
                bid = task.result()
            except Exception:
                continue
            if bid is None:
                continue
            completed_bids.append(bid)
            if bid.confidence >= spec.threshold and winner is None:
                winner = bid

    await _drain_pending(pending)

    if winner is not None:
        return winner
    if completed_bids:
        return max(completed_bids, key=lambda b: b.confidence)
    return _dense_zero("timeout_no_judges_completed")
