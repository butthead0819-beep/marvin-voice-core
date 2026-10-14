"""J1 regex 短路門檻 per-agent 覆寫（intent_bus.py _J1_SHORT_CIRCUIT_MIN）。

背景（2026-10-14 撈 records/judge_outcomes.jsonl）：J1 落在 [0.5, 0.85) 時，
grounded_qa 18/18 cleaner 從未改判；music 同區間 6/78 被改判成 frustration →
grounded_qa 的短路門檻放寬到 0.5，其餘 agent（含 music）維持全域 0.85。

照 tests/test_cleaner_race.py 的 fixture 寫法建 IntentBus。
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

from intent_agents.base import DeclarativeIntentAgent, IntentSchema
from intent_bus import IntentBus, IntentContext

pytestmark = pytest.mark.asyncio


class _StubAgent(DeclarativeIntentAgent):
    def __init__(self, name: str, patterns: list[tuple[str, float]]):
        self.name = name
        self.mode_compatible = frozenset({"normal"})
        self._schemas = [
            IntentSchema(f"{name}_intent_{i}", conf, [pat])
            for i, (pat, conf) in enumerate(patterns)
        ]

    def declare_intents(self):
        return self._schemas


def _ctx(query: str) -> IntentContext:
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
        dispatch_source="regex",
    )


async def test_grounded_qa_bid_075_short_circuits_without_cleaner(tmp_path):
    agent = _StubAgent("grounded_qa", [("查周杰倫新歌", 0.75)])
    cleaner_call = AsyncMock(return_value="查周杰倫新歌")
    bus = IntentBus(agents=[agent], cleaner_call=cleaner_call,
                     outcome_path=str(tmp_path / "outcomes.jsonl"))

    winner = await bus.dispatch(_ctx("查周杰倫新歌"))

    assert winner is not None
    assert winner.name == "grounded_qa"
    cleaner_call.assert_not_awaited()


async def test_grounded_qa_bid_05_still_short_circuits(tmp_path):
    agent = _StubAgent("grounded_qa", [("查周杰倫新歌", 0.5)])
    cleaner_call = AsyncMock(return_value="查周杰倫新歌")
    bus = IntentBus(agents=[agent], cleaner_call=cleaner_call,
                     outcome_path=str(tmp_path / "outcomes.jsonl"))

    winner = await bus.dispatch(_ctx("查周杰倫新歌"))

    assert winner is not None
    assert winner.name == "grounded_qa"
    cleaner_call.assert_not_awaited()


async def test_music_bid_08_does_not_short_circuit(tmp_path):
    """music 維持全域 0.85 門檻：0.8 不夠，保留 cleaner 改判 frustration 的空間。"""
    agent = _StubAgent("music", [("打開 YouTube", 0.8)])
    cleaner_call = AsyncMock(return_value="打開 YouTube")
    bus = IntentBus(agents=[agent], cleaner_call=cleaner_call,
                     outcome_path=str(tmp_path / "outcomes.jsonl"))

    await bus.dispatch(_ctx("打開 YouTube"))

    cleaner_call.assert_awaited()


async def test_grounded_qa_bid_04_does_not_short_circuit(tmp_path):
    agent = _StubAgent("grounded_qa", [("查周杰倫新歌", 0.4)])
    cleaner_call = AsyncMock(return_value="查周杰倫新歌")
    bus = IntentBus(agents=[agent], cleaner_call=cleaner_call,
                     outcome_path=str(tmp_path / "outcomes.jsonl"))

    await bus.dispatch(_ctx("查周杰倫新歌"))

    cleaner_call.assert_awaited()
