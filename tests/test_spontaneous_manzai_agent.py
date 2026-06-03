"""TDD: SpontaneousManzaiAgent — Marvin 自發雙人漫才（SpeakBus，不依賴 openclaw）。

bid 契約：env gate（預設 OFF）/ 靜默門檻 / cooldown / 觀眾 / 取材 全 AND；
handler 串 generate_dual_dialogue(marvin_lead) → play_dual_dialogue。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from intent_agents.spontaneous_manzai_agent import SpontaneousManzaiAgent
from speak_bus import SpeakContext


def _ctx(silence_seconds: float = 200.0, **overrides) -> SpeakContext:
    base = dict(
        channel_id=100, guild_id=1,
        silence_seconds=silence_seconds,
        present_speakers=["alice"],
        room_mood=None,
        recent_utterances=[{"speaker": "alice", "text": "今天好累"}],
        trigger="idle_tick",
    )
    base.update(overrides)
    return SpeakContext(**base)


def _agent(**kw):
    ctrl = SimpleNamespace(play_dual_dialogue=AsyncMock())
    llm_fn = AsyncMock(return_value='{"segments": []}')
    a = SpontaneousManzaiAgent(ctrl, llm_fn=llm_fn, **kw)
    return a, ctrl


# ── env gate ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_does_not_bid_when_env_disabled(monkeypatch):
    monkeypatch.delenv("SPONTANEOUS_MANZAI", raising=False)
    a, _ = _agent()
    assert await a.speak_bid(_ctx()) is None


@pytest.mark.asyncio
async def test_bids_when_enabled_and_conditions_met(monkeypatch):
    monkeypatch.setenv("SPONTANEOUS_MANZAI", "true")
    a, _ = _agent(clock=lambda: 10000.0)
    bid = await a.speak_bid(_ctx(silence_seconds=200.0))
    assert bid is not None
    assert bid.agent_name == "SpontaneousManzaiAgent"
    assert 0.30 <= bid.confidence < 0.60  # 冷場補位、不搶 ProactiveTopic(0.6)


# ── gates ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_does_not_bid_when_silence_below_threshold(monkeypatch):
    monkeypatch.setenv("SPONTANEOUS_MANZAI", "true")
    a, _ = _agent(silence_threshold_s=120.0)
    assert await a.speak_bid(_ctx(silence_seconds=60.0)) is None


@pytest.mark.asyncio
async def test_does_not_bid_within_cooldown(monkeypatch):
    monkeypatch.setenv("SPONTANEOUS_MANZAI", "true")
    a, ctrl = _agent(min_gap_since_last_s=1800.0, clock=lambda: 10000.0)
    ctrl._last_manzai_time = 10000.0 - 600  # 10min 前剛演過 < 30min
    assert await a.speak_bid(_ctx()) is None


@pytest.mark.asyncio
async def test_does_not_bid_without_audience(monkeypatch):
    monkeypatch.setenv("SPONTANEOUS_MANZAI", "true")
    a, _ = _agent(min_present=1)
    assert await a.speak_bid(_ctx(present_speakers=[])) is None


@pytest.mark.asyncio
async def test_does_not_bid_without_recent_utterances(monkeypatch):
    monkeypatch.setenv("SPONTANEOUS_MANZAI", "true")
    a, _ = _agent()
    assert await a.speak_bid(_ctx(recent_utterances=[])) is None


# ── handler integration ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_handler_generates_and_plays_dual_dialogue(monkeypatch):
    monkeypatch.setenv("SPONTANEOUS_MANZAI", "true")
    segments = [{"voice": "marvin", "text": "..."}, {"voice": "marmo", "text": "..."}]
    gen = AsyncMock(return_value=segments)
    monkeypatch.setattr("services.dialogue_generation.generate_dual_dialogue", gen)

    a, ctrl = _agent(clock=lambda: 10000.0)
    bid = await a.speak_bid(_ctx())
    await bid.handler()

    # 用 marvin_lead pattern 生成、播雙段
    assert gen.await_args.kwargs["pattern"] == "marvin_lead"
    ctrl.play_dual_dialogue.assert_awaited_once_with(segments)
    # cooldown 已記
    assert ctrl._last_manzai_time == 10000.0


@pytest.mark.asyncio
async def test_handler_skips_play_when_generation_fails(monkeypatch):
    monkeypatch.setenv("SPONTANEOUS_MANZAI", "true")
    monkeypatch.setattr(
        "services.dialogue_generation.generate_dual_dialogue",
        AsyncMock(return_value=None),
    )
    a, ctrl = _agent(clock=lambda: 10000.0)
    bid = await a.speak_bid(_ctx())
    await bid.handler()
    # 生成失敗 → 不 fallback solo、不播
    ctrl.play_dual_dialogue.assert_not_awaited()
    # 但 cooldown 仍記（避免連續重試搶池）
    assert ctrl._last_manzai_time == 10000.0
