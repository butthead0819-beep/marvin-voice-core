"""Parallel judges race coordinator — unit tests with fake judges.

Race 規則（測試裡同時也是規範）：
  - 多 judge 並行跑
  - 第一個回 Bid.confidence ≥ 自己 threshold 的 → winner，cancel 其他
  - 全部回完都沒人 fast-path → 取所有完成的 Bid 中 confidence 最高的
  - timeout → 回 timeout 前完成的最高 confidence；沒人完成 → dense zero
  - judge 例外不汙染其他 judge
  - 空 specs → dense zero
  - 被 cancel 的 judge 必須收得到 CancelledError（race 不能漏 task）

回 RaceResult，含 outcomes 給 telemetry 用：每個 judge 的 status/latency/bid/error。

完全跟 bus 解耦，judges 是 fake coroutine。
"""
from __future__ import annotations

import asyncio

import pytest

from intent_bus import Bid, IntentContext
from intent_judges.race import JudgeOutcome, JudgeSpec, RaceResult, race

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


def _outcome_by_name(result: RaceResult, name: str) -> JudgeOutcome:
    for o in result.outcomes:
        if o.name == name:
            return o
    raise AssertionError(f"no outcome named {name!r}; got {[o.name for o in result.outcomes]}")


# ── fast-path / winner selection ──────────────────────────────────────────


async def test_race_first_judge_above_threshold_wins():
    result = await race(
        _ctx(),
        [
            JudgeSpec(_judge_factory("J1", _bid("J1", 0.95), 5), threshold=0.90, name="J1"),
            JudgeSpec(_judge_factory("J2", _bid("J2", 0.85), 200), threshold=0.80, name="J2"),
        ],
    )
    assert result.winner.name == "J1"
    assert result.winner.confidence == 0.95
    assert result.winning_judge == "J1"


async def test_race_cancels_pending_judges_when_winner_emerges():
    trace: list = []
    await race(
        _ctx(),
        [
            JudgeSpec(_judge_factory("J1", _bid("J1", 0.95), 5, trace), threshold=0.90, name="J1"),
            JudgeSpec(_judge_factory("J2", _bid("J2", 0.85), 500, trace), threshold=0.80, name="J2"),
            JudgeSpec(_judge_factory("J3", _bid("J3", 0.70), 800, trace), threshold=0.30, name="J3"),
        ],
    )
    assert ("done", "J1") in trace
    assert ("cancelled", "J2") in trace
    assert ("cancelled", "J3") in trace


async def test_race_waits_for_next_judge_when_first_below_threshold():
    trace: list = []
    result = await race(
        _ctx(),
        [
            JudgeSpec(_judge_factory("J1", _bid("J1", 0.50), 5, trace), threshold=0.90, name="J1"),
            JudgeSpec(_judge_factory("J2", _bid("J2", 0.85), 30, trace), threshold=0.80, name="J2"),
        ],
    )
    assert result.winner.name == "J2"
    assert result.winning_judge == "J2"
    assert ("done", "J1") in trace
    assert ("done", "J2") in trace


# ── fallback when no fast-path ────────────────────────────────────────────


async def test_race_falls_back_to_highest_confidence_when_no_judge_above_threshold():
    result = await race(
        _ctx(),
        [
            JudgeSpec(_judge_factory("J1", _bid("J1", 0.50), 5), threshold=0.90, name="J1"),
            JudgeSpec(_judge_factory("J2", _bid("J2", 0.70), 20), threshold=0.80, name="J2"),
            JudgeSpec(_judge_factory("J3", _bid("J3", 0.20), 50), threshold=0.30, name="J3"),
        ],
    )
    assert result.winner.name == "J2"
    assert result.winner.confidence == 0.70
    assert result.winning_judge == "J2"


# ── edge: empty / single ──────────────────────────────────────────────────


async def test_race_returns_dense_zero_when_no_judges_provided():
    result = await race(_ctx(), [])
    assert result.winner.confidence == 0.0
    assert result.winner.reason
    assert result.winning_judge is None
    assert result.outcomes == []


async def test_race_with_single_judge_returns_its_bid():
    result = await race(
        _ctx(),
        [JudgeSpec(_judge_factory("only", _bid("only", 0.95), 5), threshold=0.90, name="only")],
    )
    assert result.winner.name == "only"


# ── exception isolation ──────────────────────────────────────────────────


async def test_race_isolates_judge_exception():
    trace: list = []
    result = await race(
        _ctx(),
        [
            JudgeSpec(_exploding_judge("boom", delay_ms=5, trace=trace), threshold=0.90, name="boom"),
            JudgeSpec(_judge_factory("good", _bid("good", 0.95), 30, trace), threshold=0.90, name="good"),
        ],
    )
    assert result.winner.name == "good"
    assert ("raised", "boom") in trace


# ── timeout ───────────────────────────────────────────────────────────────


async def test_race_timeout_returns_best_so_far():
    trace: list = []
    result = await race(
        _ctx(),
        [
            JudgeSpec(_judge_factory("J1", _bid("J1", 0.50), 10, trace), threshold=0.90, name="J1"),
            JudgeSpec(_hanging_judge("J2", trace=trace), threshold=0.80, name="J2"),
        ],
        timeout_s=0.1,
    )
    assert result.winner.name == "J1"
    assert result.winner.confidence == 0.50
    assert ("cancelled", "J2") in trace


async def test_race_timeout_returns_dense_zero_when_no_judge_completed():
    trace: list = []
    result = await race(
        _ctx(),
        [
            JudgeSpec(_hanging_judge("J1", trace=trace), threshold=0.90, name="J1"),
            JudgeSpec(_hanging_judge("J2", trace=trace), threshold=0.80, name="J2"),
        ],
        timeout_s=0.05,
    )
    assert result.winner.confidence == 0.0
    assert result.winning_judge is None
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

    result = await race(
        _ctx(),
        [JudgeSpec(_judge, threshold=0.90, name="J1")],
    )
    assert callable(result.winner.handler)
    await result.winner.handler()
    assert sentinel["called"] is True


# ── no task leak ──────────────────────────────────────────────────────────


async def test_race_does_not_leak_pending_tasks():
    trace: list = []
    await race(
        _ctx(),
        [
            JudgeSpec(_judge_factory("fast", _bid("fast", 0.95), 5, trace), threshold=0.90, name="fast"),
            JudgeSpec(_hanging_judge("slow", trace=trace), threshold=0.80, name="slow"),
        ],
    )
    assert ("cancelled", "slow") in trace


# ─── Instrumentation tests（Step 5a 新增）──────────────────────────────────


async def test_race_outcomes_recorded_for_every_judge():
    result = await race(
        _ctx(),
        [
            JudgeSpec(_judge_factory("J1", _bid("J1", 0.50), 5), threshold=0.90, name="J1"),
            JudgeSpec(_judge_factory("J2", _bid("J2", 0.70), 20), threshold=0.80, name="J2"),
        ],
    )
    assert len(result.outcomes) == 2
    assert {o.name for o in result.outcomes} == {"J1", "J2"}


async def test_race_outcome_status_completed_when_judge_returned_bid():
    result = await race(
        _ctx(),
        [JudgeSpec(_judge_factory("J1", _bid("J1", 0.95), 5), threshold=0.90, name="J1")],
    )
    o = _outcome_by_name(result, "J1")
    assert o.status == "completed"
    assert o.bid is not None
    assert o.bid.confidence == 0.95
    assert o.error is None


async def test_race_outcome_status_cancelled_when_winner_emerges_first():
    result = await race(
        _ctx(),
        [
            JudgeSpec(_judge_factory("fast", _bid("fast", 0.95), 5), threshold=0.90, name="fast"),
            JudgeSpec(_hanging_judge("slow"), threshold=0.80, name="slow"),
        ],
    )
    o = _outcome_by_name(result, "slow")
    assert o.status == "cancelled"
    assert o.bid is None


async def test_race_outcome_status_exception_when_judge_raises():
    result = await race(
        _ctx(),
        [
            JudgeSpec(_exploding_judge("boom", delay_ms=5), threshold=0.90, name="boom"),
            JudgeSpec(_judge_factory("good", _bid("good", 0.95), 30), threshold=0.90, name="good"),
        ],
    )
    o = _outcome_by_name(result, "boom")
    assert o.status == "exception"
    assert o.error == "RuntimeError"
    assert o.bid is None


async def test_race_outcome_latency_ms_is_positive_for_completed_judge():
    result = await race(
        _ctx(),
        [JudgeSpec(_judge_factory("J1", _bid("J1", 0.95), 20), threshold=0.90, name="J1")],
    )
    o = _outcome_by_name(result, "J1")
    assert o.latency_ms >= 15  # ≥ 20ms sleep ± 5ms slack


async def test_race_default_name_falls_back_to_judge_index_when_unspecified():
    """spec.name 留空 → outcome.name = "judge_<i>"。"""
    result = await race(
        _ctx(),
        [
            JudgeSpec(_judge_factory("a", _bid("a", 0.95), 5), threshold=0.90),
            JudgeSpec(_judge_factory("b", _bid("b", 0.50), 10), threshold=0.90),
        ],
    )
    names = {o.name for o in result.outcomes}
    assert names == {"judge_0", "judge_1"}


async def test_race_total_ms_reflects_actual_elapsed_time():
    result = await race(
        _ctx(),
        [JudgeSpec(_judge_factory("J1", _bid("J1", 0.95), 30), threshold=0.90, name="J1")],
    )
    assert result.total_ms >= 25  # ≥ 30ms ± slack


async def test_race_winning_judge_none_when_dense_zero_result():
    result = await race(_ctx(), [])
    assert result.winning_judge is None


async def test_race_outcomes_preserve_spec_order():
    result = await race(
        _ctx(),
        [
            JudgeSpec(_judge_factory("J1", _bid("J1", 0.50), 30), threshold=0.90, name="J1"),
            JudgeSpec(_judge_factory("J2", _bid("J2", 0.70), 5), threshold=0.80, name="J2"),
        ],
    )
    # J2 雖然先完成且贏，outcomes 仍照 spec 註冊順序 (J1, J2)
    assert [o.name for o in result.outcomes] == ["J1", "J2"]
