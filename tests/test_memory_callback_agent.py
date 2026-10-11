"""MemoryCallbackAgent — SpeakBus 主動發話的第二個 agent（事件記憶 Phase 2）。

事件日期過後、owner 在場時主動追問「後來怎麼樣」。取代舊版 callback_queue 主題關聯
callback（整套已拔除，2026-10-11 使用者拍板）。候選來自 EventStore.list_followup_candidates
（經 _refresh 背景刷新），過濾走 event_memory.pick_followups；每個事件只問一次
（mark_followed_up）。bid 本身必須 sync-fast：候選刷新用 asyncio.create_task 丟到背景，
不在 speak_bid 裡 await I/O。
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from intent_agents.memory_callback_agent import MemoryCallbackAgent
from speak_bus import SpeakContext
from tts_speak_policy import SpeakKind


# ── fixtures ──────────────────────────────────────────────────────────────────


class _FakeStore:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.list_calls = 0
        self.marked: list[tuple[int, float]] = []

    def list_followup_candidates(self, guild_id):
        self.list_calls += 1
        return list(self.rows)

    def mark_followed_up(self, event_id, ts):
        self.marked.append((event_id, ts))


def _ev(event_id=1, owner="Alice", activity="露營", event_date=None, heard_by=None):
    now = time.time()
    return {
        "id": event_id, "guild_id": 0, "owner": owner, "activity": activity,
        "place": None, "event_date": event_date if event_date is not None else now - 2 * 86400,
        "date_text": "", "participants": [], "heard_by": heard_by if heard_by is not None else [owner],
        "quotes": [], "first_seen": now, "last_seen": now, "created_at": now,
    }


def _mk_ctrl():
    ctrl = MagicMock()
    ctrl.bot.tts_engine.get_estimated_duration = MagicMock(return_value=3.0)
    ctrl.speak = AsyncMock(return_value=None)
    ctrl.stt_logger = MagicMock()
    ctrl.last_music_control_time = 0.0
    return ctrl


def _mk_ctx(present_speakers, last_speaker=None):
    return SpeakContext(
        channel_id=1, guild_id=1, silence_seconds=0.0,
        present_speakers=present_speakers, room_mood=None,
        recent_utterances=[], trigger="idle_tick",
        last_speaker=last_speaker, last_text=None,
    )


# ── bid edge cases ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bid_returns_zero_when_feature_flag_off(monkeypatch):
    monkeypatch.delenv("SPEAK_MEMORY_CALLBACK", raising=False)
    store = _FakeStore([_ev()])
    agent = MemoryCallbackAgent(_mk_ctrl(), store=store)
    agent._candidates = [_ev()]
    bid = await agent.speak_bid(_mk_ctx(["Alice"]))
    assert bid.confidence == 0.0
    assert bid.reason == "feature_off"


@pytest.mark.asyncio
async def test_bid_returns_zero_when_no_present_speakers(monkeypatch):
    monkeypatch.setenv("SPEAK_MEMORY_CALLBACK", "true")
    agent = MemoryCallbackAgent(_mk_ctrl(), store=_FakeStore())
    bid = await agent.speak_bid(_mk_ctx([]))
    assert bid.confidence == 0.0
    assert bid.reason == "no_present"


@pytest.mark.asyncio
async def test_bid_returns_zero_within_music_control_cooldown(monkeypatch):
    monkeypatch.setenv("SPEAK_MEMORY_CALLBACK", "true")
    ctrl = _mk_ctrl()
    ctrl.last_music_control_time = time.time() - 5.0
    agent = MemoryCallbackAgent(ctrl, store=_FakeStore(), music_control_cooldown_s=30.0)
    agent._candidates = [_ev()]
    bid = await agent.speak_bid(_mk_ctx(["Alice"]))
    assert bid.confidence == 0.0
    assert bid.reason == "music_control_cooldown"


@pytest.mark.asyncio
async def test_bid_wins_when_event_due_and_owner_present(monkeypatch):
    monkeypatch.setenv("SPEAK_MEMORY_CALLBACK", "true")
    ev = _ev(event_id=5, owner="Alice", activity="露營")
    agent = MemoryCallbackAgent(_mk_ctrl(), store=_FakeStore(), confidence=0.7)
    agent._candidates = [ev]
    agent._cache_ts = time.time()  # 快取新鮮，不觸發刷新
    bid = await agent.speak_bid(_mk_ctx(["Alice"], last_speaker="Alice"))
    assert bid.confidence == 0.7
    assert bid.agent_name == "MemoryCallbackAgent"
    assert bid.reason.startswith("event_followup:")
    assert callable(bid.handler)


@pytest.mark.asyncio
async def test_bid_returns_zero_when_present_not_subset_of_heard_by(monkeypatch):
    """隱私濾網：在場有人不在 heard_by → no_due_event。"""
    monkeypatch.setenv("SPEAK_MEMORY_CALLBACK", "true")
    ev = _ev(event_id=5, owner="Alice", heard_by=["Alice"])
    agent = MemoryCallbackAgent(_mk_ctrl(), store=_FakeStore(), confidence=0.7)
    agent._candidates = [ev]
    agent._cache_ts = time.time()
    bid = await agent.speak_bid(_mk_ctx(["Alice", "Bob"], last_speaker="Alice"))
    assert bid.confidence == 0.0
    assert bid.reason == "no_due_event"


@pytest.mark.asyncio
async def test_bid_creates_refresh_task_without_sync_io_when_cache_stale(monkeypatch):
    """快取過期會建立 refresh task，但 bid 本身不同步呼叫 store（sync-fast）。"""
    monkeypatch.setenv("SPEAK_MEMORY_CALLBACK", "true")
    store = _FakeStore([_ev(owner="Alice")])
    agent = MemoryCallbackAgent(_mk_ctrl(), store=store, refresh_s=300.0)
    assert agent._cache_ts == 0.0  # 初始即過期

    bid = await agent.speak_bid(_mk_ctx(["Alice"]))

    assert agent._refresh_task is not None
    assert store.list_calls == 0  # bid 回傳時尚未同步呼叫 store
    # 讓背景 refresh task 真正跑完，不留 pending task 的警告
    await agent._refresh_task
    assert store.list_calls == 1


# ── handler ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handler_speaks_marks_and_removes_candidate(monkeypatch):
    from event_memory import render_followup_line

    monkeypatch.setenv("SPEAK_MEMORY_CALLBACK", "true")
    ev = _ev(event_id=9, owner="Alice", activity="露營")
    store = _FakeStore()
    ctrl = _mk_ctrl()
    agent = MemoryCallbackAgent(ctrl, store=store, confidence=0.7, cooldown_s=1800.0)
    agent._candidates = [ev]
    agent._cache_ts = time.time()

    bid = await agent.speak_bid(_mk_ctx(["Alice"], last_speaker="Alice"))
    await bid.handler()

    ctrl.speak.assert_awaited_once()
    _, kwargs = ctrl.speak.call_args
    assert kwargs.get("allow_dual") is False
    assert kwargs.get("proactive") is True
    assert kwargs.get("kind") == SpeakKind.MEMORY_CALLBACK
    assert ctrl.speak.call_args.args[0] == render_followup_line(ev)

    assert store.marked and store.marked[0][0] == 9
    assert agent._candidates == []

    # 已講過 → cooldown 內不再 bid
    second = await agent.speak_bid(_mk_ctx(["Alice"], last_speaker="Alice"))
    assert second.confidence == 0.0
    assert second.reason == "cooldown"


@pytest.mark.asyncio
async def test_handler_swallows_speak_exception_and_does_not_mark(monkeypatch):
    monkeypatch.setenv("SPEAK_MEMORY_CALLBACK", "true")
    ev = _ev(event_id=3, owner="Alice")
    store = _FakeStore()
    ctrl = _mk_ctrl()
    ctrl.speak = AsyncMock(side_effect=RuntimeError("voice client died"))
    agent = MemoryCallbackAgent(ctrl, store=store, confidence=0.7)
    agent._candidates = [ev]
    agent._cache_ts = time.time()

    bid = await agent.speak_bid(_mk_ctx(["Alice"], last_speaker="Alice"))
    await bid.handler()  # 不傳播

    assert store.marked == []
    assert agent._candidates == [ev]


def test_mode_compatible_is_normal_only():
    assert MemoryCallbackAgent.mode_compatible == frozenset({"normal"})
