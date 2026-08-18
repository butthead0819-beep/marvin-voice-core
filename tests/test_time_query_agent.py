"""TimeQueryAgent — 「現在幾點」報時 intent.

對應 records/agent_gaps.jsonl 分析（2026-08-18）：time_query 達 READY_THRESHOLD=2
（5 筆/2 distinct，樣本「現在幾點」）。零 LLM 成本，直接讀系統時鐘報時。
"""
from __future__ import annotations

import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from intent_bus import IntentContext


pytestmark = pytest.mark.asyncio


def _ctx(query: str, mode: str = "normal") -> IntentContext:
    return IntentContext(
        speaker="alice",
        raw_text=query,
        query=query,
        original_raw=query,
        wake_intent=0.9,
        stream_active=(mode == "stream"),
        game_mode=(mode == "game"),
        is_owner=False,
        now=0.0,
        mode=mode,
    )


def _ctrl() -> MagicMock:
    ctrl = MagicMock()
    ctrl.play_tts = AsyncMock()
    return ctrl


async def test_time_query_matches_gap_sample():
    """agent_gaps.jsonl 實際樣本「現在幾點」應該命中，confidence=0.90。"""
    from intent_agents.time_query_agent import TimeQueryAgent
    agent = TimeQueryAgent(_ctrl())
    bid = agent.bid(_ctx("現在幾點"))
    assert bid.confidence == 0.90
    assert bid.handler is not None


@pytest.mark.parametrize("query", ["現在幾點", "現在幾點了", "幾點了", "現在是什麼時間", "報時"])
async def test_time_query_variants_match(query):
    from intent_agents.time_query_agent import TimeQueryAgent
    agent = TimeQueryAgent(_ctrl())
    bid = agent.bid(_ctx(query))
    assert bid.confidence == 0.90, f"{query!r} 應命中 time_query，got reason={bid.reason}"


async def test_time_query_no_gate_works_without_playback():
    """報時跟播放狀態無關，沒開音樂也該回答（跟 VolumeAgent 的 no_playback_active gate 不同）。"""
    from intent_agents.time_query_agent import TimeQueryAgent
    agent = TimeQueryAgent(_ctrl())
    bid = agent.bid(_ctx("現在幾點"))
    assert bid.confidence == 0.90


async def test_time_query_handler_speaks_current_time():
    from intent_agents.time_query_agent import TimeQueryAgent
    ctrl = _ctrl()
    agent = TimeQueryAgent(ctrl)
    bid = agent.bid(_ctx("現在幾點"))
    await bid.handler()

    ctrl.play_tts.assert_awaited_once()
    spoken_text = ctrl.play_tts.await_args.args[0]
    tpe_now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    assert f"{tpe_now.hour}點" in spoken_text


async def test_time_query_does_not_match_unrelated_query():
    from intent_agents.time_query_agent import TimeQueryAgent
    agent = TimeQueryAgent(_ctrl())
    bid = agent.bid(_ctx("放一首歌"))
    assert bid.confidence == 0.0
