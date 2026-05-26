"""DuckingAgent 補洞測試（不重寫現有偵測核心）。

對齊 social_catalyst_plan.md 三個缺口：
  1. 偵測命中時寫 RoomMoodState.hot_chat（playback 層用來判 fade-out）
  2. wake_threshold_boost() — 熱聊時提高 wake 門檻（不改 MIN_CONFIDENCE 常數）
  3. release() — 手動解除（給「被點名」路徑用）

不變式：mood_store=None 時所有新行為退化為 no-op（向後相容）。
"""
from __future__ import annotations

import pytest

from ducking_agent import DuckingAgent
from speak_bus import SpeakBus
from room_mood_state import RoomMoodStateStore


class _Clock:
    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def bus() -> SpeakBus:
    return SpeakBus()


@pytest.fixture
def mood_store(tmp_path) -> RoomMoodStateStore:
    return RoomMoodStateStore(dump_path=str(tmp_path / "mood.json"))


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


# ── 1. mood_store integration ────────────────────────────────────────────────


def test_hot_chat_writes_mood_store_when_provided(bus, mood_store, clock):
    a = DuckingAgent(bus, mood_store=mood_store, channel_id=100, clock=clock)
    a.on_utterance("alice", ts=0.0)
    a.on_utterance("bob", ts=3.0)
    a.on_utterance("alice", ts=6.0); clock.t = 6.0

    state = mood_store.get(100)
    assert state.hot_chat is True
    assert state.hot_chat_pair is not None
    assert set(state.hot_chat_pair) == {"alice", "bob"}


def test_mood_store_inert_when_none(bus, clock):
    """無 mood_store → 不該炸；既有偵測 + multiplier 路徑不變。"""
    a = DuckingAgent(bus, mood_store=None, clock=clock)
    a.on_utterance("alice", ts=0.0)
    a.on_utterance("bob", ts=3.0)
    a.on_utterance("alice", ts=6.0); clock.t = 6.0
    assert bus.get_global_multiplier() == pytest.approx(0.2)


# ── 2. wake_threshold_boost ──────────────────────────────────────────────────


def test_wake_threshold_boost_when_hot(bus, clock):
    a = DuckingAgent(bus, clock=clock, wake_threshold_boost=0.1)
    a.on_utterance("alice", ts=0.0)
    a.on_utterance("bob", ts=3.0)
    a.on_utterance("alice", ts=6.0); clock.t = 6.0
    assert a.wake_threshold_boost() == pytest.approx(0.1)


def test_wake_threshold_boost_zero_when_cold(bus, clock):
    a = DuckingAgent(bus, clock=clock, wake_threshold_boost=0.1)
    assert a.wake_threshold_boost() == pytest.approx(0.0)


def test_wake_threshold_boost_decays_after_ttl(bus, clock):
    a = DuckingAgent(
        bus, clock=clock,
        wake_threshold_boost=0.1, suppress_ttl_s=30.0,
    )
    a.on_utterance("alice", ts=0.0)
    a.on_utterance("bob", ts=3.0)
    a.on_utterance("alice", ts=6.0); clock.t = 6.0
    assert a.wake_threshold_boost() == pytest.approx(0.1)
    clock.t = 40.0  # 過了 suppress_ttl_s
    assert a.wake_threshold_boost() == pytest.approx(0.0)


# ── 3. release() ─────────────────────────────────────────────────────────────


def test_release_clears_multiplier(bus, clock):
    a = DuckingAgent(bus, clock=clock)
    a.on_utterance("alice", ts=0.0)
    a.on_utterance("bob", ts=3.0)
    a.on_utterance("alice", ts=6.0); clock.t = 6.0
    assert bus.get_global_multiplier() == pytest.approx(0.2)

    a.release()
    assert bus.get_global_multiplier() == pytest.approx(1.0)


def test_release_clears_mood_store_flag(bus, mood_store, clock):
    a = DuckingAgent(bus, mood_store=mood_store, channel_id=100, clock=clock)
    a.on_utterance("alice", ts=0.0)
    a.on_utterance("bob", ts=3.0)
    a.on_utterance("alice", ts=6.0); clock.t = 6.0
    assert mood_store.get(100).hot_chat is True

    a.release()
    assert mood_store.get(100).hot_chat is False
    assert mood_store.get(100).hot_chat_pair is None


def test_release_clears_wake_boost(bus, clock):
    a = DuckingAgent(bus, clock=clock, wake_threshold_boost=0.1)
    a.on_utterance("alice", ts=0.0)
    a.on_utterance("bob", ts=3.0)
    a.on_utterance("alice", ts=6.0); clock.t = 6.0
    assert a.wake_threshold_boost() > 0

    a.release()
    assert a.wake_threshold_boost() == pytest.approx(0.0)


def test_release_when_not_hot_is_noop(bus, mood_store, clock):
    """從未進入 hot 就 release → 不該炸，狀態不變。"""
    a = DuckingAgent(bus, mood_store=mood_store, channel_id=100, clock=clock)
    a.release()
    assert bus.get_global_multiplier() == pytest.approx(1.0)
    assert mood_store.get(100).hot_chat is False


# ── is_hot inspection ────────────────────────────────────────────────────────


def test_is_hot_reflects_current_state(bus, clock):
    a = DuckingAgent(bus, clock=clock, suppress_ttl_s=30.0)
    assert a.is_hot() is False
    a.on_utterance("alice", ts=0.0)
    a.on_utterance("bob", ts=3.0)
    a.on_utterance("alice", ts=6.0); clock.t = 6.0
    assert a.is_hot() is True
    clock.t = 40.0
    assert a.is_hot() is False
