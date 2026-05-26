"""Parallel judges race coordinator + instrumentation result type.

多 judge 並行跑，第一個 Bid.confidence ≥ 自己 threshold 的 winner，cancel 其他。
全部完成都沒人 fast-path → 取整體最高 confidence。timeout → 回 timeout 前最高分；
沒人完成 → dense zero。

回 `RaceResult`（不是裸 Bid）—— 帶 per-judge outcomes（status / latency / bid / error）
與 winning_judge + total_ms，給 telemetry writer 用，race 本身不寫檔。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from intent_bus import Bid, IntentContext

_RACE_NAME = "judges_race"


@dataclass(frozen=True)
class JudgeSpec:
    """一個 judge 的 race 規格。`name` 給 outcome telemetry 識別；不給就用 index。"""
    judge: Callable[[IntentContext], Awaitable[Bid]]
    threshold: float
    name: str = ""


@dataclass(frozen=True)
class JudgeOutcome:
    """單一 judge 的 race 結果。status ∈ {completed, cancelled, exception}。"""
    name: str
    status: str
    bid: Bid | None
    latency_ms: float
    error: str | None = None


@dataclass(frozen=True)
class RaceResult:
    """整個 race 的結果。winner 永遠存在（無 judge 完成時為 dense zero）。"""
    winner: Bid
    winning_judge: str | None
    outcomes: list[JudgeOutcome] = field(default_factory=list)
    total_ms: float = 0.0


async def _async_noop() -> None:
    pass


def _dense_zero(reason: str) -> Bid:
    return Bid(name=_RACE_NAME, confidence=0.0, handler=_async_noop, reason=reason)


async def race(
    ctx: IntentContext,
    specs: list[JudgeSpec],
    *,
    timeout_s: float = 5.0,
    fast_path_excludes: frozenset[str] = frozenset(),
) -> RaceResult:
    loop = asyncio.get_event_loop()
    race_start = loop.time()

    if not specs:
        return RaceResult(
            winner=_dense_zero("no_judges"),
            winning_judge=None,
            outcomes=[],
            total_ms=0.0,
        )

    # 保持 spec 註冊順序，outcomes 也照順序回（debug 友善）
    task_meta: list[tuple[asyncio.Task, JudgeSpec, str, float]] = []
    for i, spec in enumerate(specs):
        name = spec.name or f"judge_{i}"
        task = asyncio.create_task(spec.judge(ctx))
        task_meta.append((task, spec, name, loop.time()))

    task_outcome: dict[asyncio.Task, JudgeOutcome] = {}
    completed: list[tuple[str, Bid]] = []
    winner: Bid | None = None
    winning_judge: str | None = None
    pending: set[asyncio.Task] = {t for t, _, _, _ in task_meta}
    meta_by_task = {t: (s, n, ts) for t, s, n, ts in task_meta}
    deadline = race_start + timeout_s

    while pending and winner is None:
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        done, pending = await asyncio.wait(
            pending, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
        )
        if not done:
            break
        for task in done:
            spec, name, t_start = meta_by_task[task]
            elapsed_ms = (loop.time() - t_start) * 1000
            try:
                bid = task.result()
            except Exception as e:
                task_outcome[task] = JudgeOutcome(
                    name=name, status="exception", bid=None,
                    latency_ms=elapsed_ms, error=type(e).__name__,
                )
                continue
            if bid is None:
                task_outcome[task] = JudgeOutcome(
                    name=name, status="exception", bid=None,
                    latency_ms=elapsed_ms, error="NoneReturned",
                )
                continue
            task_outcome[task] = JudgeOutcome(
                name=name, status="completed", bid=bid, latency_ms=elapsed_ms,
            )
            completed.append((name, bid))
            if (
                bid.confidence >= spec.threshold
                and bid.name not in fast_path_excludes
                and winner is None
            ):
                winner = bid
                winning_judge = name

    # Cancel pending and drain
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    cancel_observed = loop.time()
    for task in pending:
        spec, name, t_start = meta_by_task[task]
        task_outcome[task] = JudgeOutcome(
            name=name, status="cancelled", bid=None,
            latency_ms=(cancel_observed - t_start) * 1000,
        )

    # Fallback to best-confidence when no fast-path
    if winner is None and completed:
        best_name, best_bid = max(completed, key=lambda x: x[1].confidence)
        winner = best_bid
        winning_judge = best_name
    if winner is None:
        winner = _dense_zero("timeout_no_judges_completed")
        winning_judge = None

    outcomes = [task_outcome[t] for t, _, _, _ in task_meta]
    total_ms = (loop.time() - race_start) * 1000
    return RaceResult(
        winner=winner, winning_judge=winning_judge,
        outcomes=outcomes, total_ms=total_ms,
    )
