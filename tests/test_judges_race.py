"""Parallel judges race coordinator — unit tests with fake judges.

Race 規則（測試裡同時也是規範）：
  - 多 judge 並行跑
  - 第一個回 Bid.confidence ≥ 自己 threshold 的 → winner，cancel 其他
  - 全部回完都沒人 fast-path → 取所有完成的 Bid 中 confidence 最高的
  - timeout → 回 timeout 前完成的最高 confidence；沒人完成 → dense zero
  - judge 例外不汙染其他 judge
  - 空 specs → dense zero
  - 被 cancel 的 judge 必須收得到 CancelledError（race 不能漏 task）

完全跟 bus 解耦，judges 是 fake coroutine。
"""
from __future__ import annotations

import asyncio

import pytest

from intent_bus import Bid, IntentContext
from intent_judges.race import JudgeSpec, race

pytestmark = pytest.mark.asyncio


def _ctx(query: str = "hi") -> IntentContext:
    return IntentContext(
        speaker="alice",
        raw_text=query,
        query=query,
        original_raw=query,
        wake_intent=0.9,
        stream_active=False,
        game_mode=False,
        is_owner=False,
        now=0.0,
        mode="normal",
    )


async def _async_noop() -> None:
    pass


def _bid(name: str, conf: float, reason: str = "ok") -> Bid:
    return Bid(name=name, confidence=conf, handler=_async_noop, reason=reason)


def _judge_factory(name: str, bid_to_return: Bid, delay_ms: int,
                   trace: list | None = None):
    """產生 fake judge：等 delay_ms 後回 bid_to_return；
    被 cancel 時把 ("cancelled", name) 推入 trace。"""

    async def _judge(ctx):
        try:
            await asyncio.sleep(delay_ms / 1000)
        except asyncio.CancelledError:
            if trace is not None:
                trace.append(("cancelled", name))
            raise
        if trace is not None:
            trace.append(("done", name))
        return bid_to_return

    return _judge


def _exploding_judge(name: str, delay_ms: int = 0, trace: list | None = None):
    async def _judge(ctx):
        await asyncio.sleep(delay_ms / 1000)
        if trace is not None:
            trace.append(("raised", name))
        raise RuntimeError(f"{name} broke")

    return _judge


def _hanging_judge(name: str, trace: list | None = None):
    async def _judge(ctx):
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            if trace is not None:
                trace.append(("cancelled", name))
            raise

    return _judge


# ── fast-path / winner selection ──────────────────────────────────────────


async def test_race_first_judge_above_threshold_wins():
    trace: list = []
    winner = await race(
        _ctx(),
        [
            JudgeSpec(_judge_factory("J1", _bid("J1", 0.95), 5, trace), threshold=0.90),
            JudgeSpec(_judge_factory("J2", _bid("J2", 0.85), 200, trace), threshold=0.80),
        ],
    )
    assert winner.name == "J1"
    assert winner.confidence == 0.95


async def test_race_cancels_pending_judges_when_winner_emerges():
    trace: list = []
    await race(
        _ctx(),
        [
            JudgeSpec(_judge_factory("J1", _bid("J1", 0.95), 5, trace), threshold=0.90),
            JudgeSpec(_judge_factory("J2", _bid("J2", 0.85), 500, trace), threshold=0.80),
            JudgeSpec(_judge_factory("J3", _bid("J3", 0.70), 800, trace), threshold=0.30),
        ],
    )
    # J1 done, J2/J3 cancelled (race 必須等 cancellation 真的傳達)
    assert ("done", "J1") in trace
    assert ("cancelled", "J2") in trace
    assert ("cancelled", "J3") in trace


async def test_race_waits_for_next_judge_when_first_below_threshold():
    trace: list = []
    winner = await race(
        _ctx(),
        [
            JudgeSpec(_judge_factory("J1", _bid("J1", 0.50), 5, trace), threshold=0.90),
            JudgeSpec(_judge_factory("J2", _bid("J2", 0.85), 30, trace), threshold=0.80),
        ],
    )
    assert winner.name == "J2"
    assert ("done", "J1") in trace  # J1 完成，只是沒 fast-path
    assert ("done", "J2") in trace


# ── fallback when no fast-path ────────────────────────────────────────────


async def test_race_falls_back_to_highest_confidence_when_no_judge_above_threshold():
    """所有 judge 都完成但都沒過自己門檻 → 取整體最高 confidence。"""
    winner = await race(
        _ctx(),
        [
            JudgeSpec(_judge_factory("J1", _bid("J1", 0.50), 5), threshold=0.90),
            JudgeSpec(_judge_factory("J2", _bid("J2", 0.70), 20), threshold=0.80),
            JudgeSpec(_judge_factory("J3", _bid("J3", 0.20), 50), threshold=0.30),
        ],
    )
    assert winner.name == "J2"
    assert winner.confidence == 0.70


# ── edge: empty / single ──────────────────────────────────────────────────


async def test_race_returns_dense_zero_when_no_judges_provided():
    winner = await race(_ctx(), [])
    assert winner.confidence == 0.0
    assert winner.reason


async def test_race_with_single_judge_returns_its_bid():
    winner = await race(
        _ctx(),
        [JudgeSpec(_judge_factory("only", _bid("only", 0.95), 5), threshold=0.90)],
    )
    assert winner.name == "only"


# ── exception isolation ──────────────────────────────────────────────────


async def test_race_isolates_judge_exception():
    """一個 judge raise，其他 judges 還是要算數。"""
    trace: list = []
    winner = await race(
        _ctx(),
        [
            JudgeSpec(_exploding_judge("boom", delay_ms=5, trace=trace), threshold=0.90),
            JudgeSpec(_judge_factory("good", _bid("good", 0.95), 30, trace), threshold=0.90),
        ],
    )
    assert winner.name == "good"
    assert ("raised", "boom") in trace


# ── timeout ───────────────────────────────────────────────────────────────


async def test_race_timeout_returns_best_so_far():
    """timeout 內 J1 完成但低分、J2 hang → 回 J1 (best completed)。"""
    trace: list = []
    winner = await race(
        _ctx(),
        [
            JudgeSpec(_judge_factory("J1", _bid("J1", 0.50), 10, trace), threshold=0.90),
            JudgeSpec(_hanging_judge("J2", trace=trace), threshold=0.80),
        ],
        timeout_s=0.1,
    )
    assert winner.name == "J1"
    assert winner.confidence == 0.50
    assert ("cancelled", "J2") in trace


async def test_race_timeout_returns_dense_zero_when_no_judge_completed():
    """全部 hang → timeout → dense zero。"""
    trace: list = []
    winner = await race(
        _ctx(),
        [
            JudgeSpec(_hanging_judge("J1", trace=trace), threshold=0.90),
            JudgeSpec(_hanging_judge("J2", trace=trace), threshold=0.80),
        ],
        timeout_s=0.05,
    )
    assert winner.confidence == 0.0
    assert ("cancelled", "J1") in trace
    assert ("cancelled", "J2") in trace


# ── handler passthrough ──────────────────────────────────────────────────


async def test_race_winner_carries_handler_for_dispatch():
    sentinel = {"called": False}

    async def _handler():
        sentinel["called"] = True

    winning_bid = Bid(name="J1", confidence=0.95, handler=_handler, reason="ok")

    async def _judge(ctx):
        await asyncio.sleep(0.005)
        return winning_bid

    winner = await race(
        _ctx(),
        [JudgeSpec(_judge, threshold=0.90)],
    )
    assert callable(winner.handler)
    await winner.handler()
    assert sentinel["called"] is True


# ── no task leak（pending 必須 await 完成 cancellation）─────────────────────


async def test_race_does_not_leak_pending_tasks():
    """winner 出來後，pending judges 必須在 race return 前真的結束。
    否則 pytest async runner 會在 test fixture teardown 時警告。"""
    trace: list = []
    await race(
        _ctx(),
        [
            JudgeSpec(_judge_factory("fast", _bid("fast", 0.95), 5, trace), threshold=0.90),
            JudgeSpec(_hanging_judge("slow", trace=trace), threshold=0.80),
        ],
    )
    # race 必須 await pending 的 cancellation，不能直接 return
    assert ("cancelled", "slow") in trace
