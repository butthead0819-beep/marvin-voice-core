"""J1 Regex & J3 Cleaner Race Integration tests.

Tests the parallel race coordinator wired up in IntentBus.dispatch.
"""
from __future__ import annotations

import asyncio
import json
import pytest
from pathlib import Path

from intent_agents.base import DeclarativeIntentAgent, IntentSchema
from intent_bus import IntentBus, IntentContext, Bid
from intent_judges.race import RaceResult


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


def _ctx(query: str, dispatch_source: str = "regex") -> IntentContext:
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
        dispatch_source=dispatch_source,
    )


def _fake_cleaner(cleaned: str, delay_ms: int = 0, called_list: list[str] | None = None):
    async def _call(ctx):
        if called_list is not None:
            called_list.append(ctx.query)
        if delay_ms:
            await asyncio.sleep(delay_ms / 1000)
        return cleaned
    return _call


async def test_race_j1_fast_path_wins_instantly(tmp_path):
    """J1 直接命中且高於 threshold，J3 應該被 cancellation。"""
    agent = _StubAgent("music", [("打開 YouTube", 0.95)])
    cleaner_called = []
    
    # J3 的 cleaner_call 故意延遲 100ms
    cleaner_call = _fake_cleaner("打開 YouTube", delay_ms=100, called_list=cleaner_called)
    outcome_file = tmp_path / "outcomes.jsonl"
    
    bus = IntentBus(
        agents=[agent],
        cleaner_call=cleaner_call,
        outcome_path=str(outcome_file)
    )
    
    winner = await bus.dispatch(_ctx("打開 YouTube"))
    
    assert winner is not None
    assert winner.name == "music"
    assert winner.confidence == 0.95
    
    # 讀取 outcomes.jsonl 檢查
    assert outcome_file.exists()
    lines = outcome_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    outcome_data = json.loads(lines[0])
    
    # 應該有 j1_regex 與 j3_cleaner 記錄
    judges = outcome_data["judges"]
    assert len(judges) == 2
    j1_o = next(o for o in judges if o["name"] == "j1_regex")
    j3_o = next(o for o in judges if o["name"] == "j3_cleaner")
    
    assert j1_o["status"] == "completed"
    assert j3_o["status"] == "cancelled"


async def test_race_j1_misses_j3_wins(tmp_path):
    """J1 未命中，J3 透過 cleaner_call 改寫後匹配命中，最終 J3 獲勝。"""
    agent = _StubAgent("music", [("打開 YouTube", 0.95)])
    cleaner_called = []
    
    # raw text 模糊，J1 會 miss；但 cleaner 改寫為 "打開 YouTube"
    cleaner_call = _fake_cleaner("打開 YouTube", delay_ms=10, called_list=cleaner_called)
    outcome_file = tmp_path / "outcomes.jsonl"
    
    bus = IntentBus(
        agents=[agent],
        cleaner_call=cleaner_call,
        outcome_path=str(outcome_file)
    )
    
    winner = await bus.dispatch(_ctx("打打...打開 U2"))
    
    assert winner is not None
    assert winner.name == "music"
    assert winner.confidence == 0.95
    assert "打開 YouTube" in winner.reason
    
    # 檢查 outcomes
    assert outcome_file.exists()
    lines = outcome_file.read_text(encoding="utf-8").splitlines()
    outcome_data = json.loads(lines[0])
    judges = outcome_data["judges"]
    
    j1_o = next(o for o in judges if o["name"] == "j1_regex")
    j3_o = next(o for o in judges if o["name"] == "j3_cleaner")
    
    assert j1_o["status"] == "completed"
    assert j1_o["confidence"] == 0.0
    assert j3_o["status"] == "completed"
    assert j3_o["confidence"] == 0.95


async def test_race_no_double_race_on_redispatch(tmp_path):
    """重投 (dispatch_source != 'regex') 時不重複 race，改走常規單步 dispatch。"""
    agent = _StubAgent("music", [("打開 YouTube", 0.95)])
    cleaner_called = []
    
    # 即使有 cleaner_call，但因為 dispatch_source="resolver"，不應進入 race
    cleaner_call = _fake_cleaner("打開 YouTube", delay_ms=10, called_list=cleaner_called)
    outcome_file = tmp_path / "outcomes.jsonl"
    
    bus = IntentBus(
        agents=[agent],
        cleaner_call=cleaner_call,
        outcome_path=str(outcome_file)
    )
    
    winner = await bus.dispatch(_ctx("打開 YouTube", dispatch_source="resolver"))
    
    assert winner is not None
    assert winner.name == "music"
    assert winner.confidence == 0.95
    
    # 不應該執行 cleaner_call
    assert len(cleaner_called) == 0
    # 不應該寫入 outcomes.jsonl (因為沒有 race)
    assert not outcome_file.exists()
