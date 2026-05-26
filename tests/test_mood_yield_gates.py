"""MoodAgent 行動級閘 — heavy tier 時其他 SpeakAgent 該禮讓。

P3：讓 MoodAgent 的 action_tier 真的影響行為（之前只寫 store，沒人讀）。

涵蓋：
  - ProactiveTopicAgent：tier="heavy" 時 dense 0 with reason="mood_heavy_yield"
  - BridgeAgent：tier="heavy" 時 dense 0 with reason="mood_heavy_yield"
  - mood_agent=None 時行為不變（向後相容）

設計取捨：先只 gate "heavy"（低落 + 溫度 ≤0.3 + 靜默 ≥60s），其他 tier 不影響。
"light" / "mid" 是 hint 不是禁令；agent 自己決定要不要回應。"heavy" 是真的該退。
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from bridge_agent import BridgeAgent
from proactive_topic_agent import ProactiveTopicAgent
from speak_bus import SpeakContext
from speaker_topic_graph import SpeakerTopicGraph


# ── shared stubs ─────────────────────────────────────────────────────────────


class _HeavyMoodAgent:
    """Stub MoodAgent — get_action_tier 永遠回 heavy。"""
    def get_action_tier(self, channel_id, *, silence_seconds=0.0):
        return "heavy"


class _NoneMoodAgent:
    """Stub MoodAgent — 一切正常，回 none。"""
    def get_action_tier(self, channel_id, *, silence_seconds=0.0):
        return "none"


@pytest.fixture
def graph() -> SpeakerTopicGraph:
    return SpeakerTopicGraph(db_path=":memory:")


def _controller():
    async def fake_play_tts(text: str, **_):
        pass
    async def fake_trigger():
        pass
    return SimpleNamespace(
        play_tts=fake_play_tts,
        trigger_proactive_topic=fake_trigger,
        proactive_silence_threshold=90.0,
        last_proactive_time=0.0,
        active_text_channel=SimpleNamespace(id=100, guild=SimpleNamespace(id=1)),
        radio_mode=False, stream_mode=False,
        bot=SimpleNamespace(router=SimpleNamespace(current_game=None)),
    )


def _proactive_ctx(silence: float = 120) -> SpeakContext:
    return SpeakContext(
        channel_id=100, guild_id=1,
        silence_seconds=silence,
        present_speakers=["alice", "bob"],
        room_mood=None, recent_utterances=[],
        trigger="idle_tick",
    )


def _bridge_ctx() -> SpeakContext:
    return SpeakContext(
        channel_id=100, guild_id=1,
        silence_seconds=0.0,
        present_speakers=["alice", "bob"],
        room_mood=None, recent_utterances=[],
        trigger="post_utterance",
        last_speaker="alice", last_text="今天主管又在罵人",
    )


# ── ProactiveTopicAgent ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_proactive_yields_when_mood_heavy():
    a = ProactiveTopicAgent(
        _controller(),
        min_gap_since_last_s=0,
        mood_agent=_HeavyMoodAgent(),
    )
    bid = await a.speak_bid(_proactive_ctx())
    assert bid is None or bid.confidence == 0.0


@pytest.mark.asyncio
async def test_proactive_unchanged_when_mood_none():
    a = ProactiveTopicAgent(
        _controller(),
        min_gap_since_last_s=0,
        mood_agent=_NoneMoodAgent(),
    )
    bid = await a.speak_bid(_proactive_ctx())
    assert bid is not None and bid.confidence > 0


@pytest.mark.asyncio
async def test_proactive_unchanged_without_mood_agent():
    """向後相容：mood_agent=None 行為與舊版相同。"""
    a = ProactiveTopicAgent(_controller(), min_gap_since_last_s=0)
    bid = await a.speak_bid(_proactive_ctx())
    assert bid is not None and bid.confidence > 0


# ── BridgeAgent ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bridge_yields_when_mood_heavy(graph):
    graph.record_utterance("bob", 100, "上週主管也在罵人", ts=time.time() - 10)
    a = BridgeAgent(
        _controller(),
        topic_graph=graph,
        min_overlap=0.1,
        mood_agent=_HeavyMoodAgent(),
    )
    bid = await a.speak_bid(_bridge_ctx())
    assert bid.confidence == 0.0
    assert "mood_heavy_yield" in bid.reason


@pytest.mark.asyncio
async def test_bridge_unchanged_when_mood_none(graph):
    graph.record_utterance("bob", 100, "上週主管也在罵人", ts=time.time() - 10)
    a = BridgeAgent(
        _controller(),
        topic_graph=graph,
        min_overlap=0.1,
        mood_agent=_NoneMoodAgent(),
    )
    bid = await a.speak_bid(_bridge_ctx())
    assert bid.confidence > 0


@pytest.mark.asyncio
async def test_bridge_unchanged_without_mood_agent(graph):
    graph.record_utterance("bob", 100, "上週主管也在罵人", ts=time.time() - 10)
    a = BridgeAgent(_controller(), topic_graph=graph, min_overlap=0.1)
    bid = await a.speak_bid(_bridge_ctx())
    assert bid.confidence > 0
