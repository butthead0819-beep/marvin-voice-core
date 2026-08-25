"""DeclarativeIntentAgent.resolve_intent() — Audio Rescue v2 專用。

LLM 已經指名要哪個 intent（跳過 regex 文字比對），resolve_intent() 只重放
bid() 除了「regex 找 schema」以外的所有既有守門邏輯：mode_compatible / gate() /
post_match_filter() / make_handler()。用既有 VolumeAgent / FindSongAgent 當
fixture，不寫假 agent。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from intent_agents.find_song_agent import FindSongAgent
from intent_agents.volume_agent import VolumeAgent
from intent_bus import IntentContext


def _ctx(query="", mode="normal"):
    return IntentContext(
        speaker="Alice", raw_text=query, query=query, original_raw=query,
        wake_intent=0.9, stream_active=False, game_mode=False,
        is_owner=False, now=0.0, mode=mode,
    )


def _volume_ctrl(**overrides):
    defaults = dict(stream_mode=False, radio_mode=False, stream_volume=0.5,
                     VOL_MIN=0.01, VOL_MAX=1.0, play_tts=AsyncMock(),
                     request_volume_swap=lambda: None)
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.mark.asyncio
async def test_resolve_intent_executes_when_gate_passes():
    ctrl = _volume_ctrl(stream_mode=True, stream_volume=0.5)
    agent = VolumeAgent(ctrl)

    bid = agent.resolve_intent("volume_down", {}, _ctx())

    assert bid is not None
    await bid.handler()
    # 語音步進 25%（2026-08-25 改前 10%）：0.5 - 0.25 = 0.25
    assert ctrl.stream_volume == pytest.approx(0.25)


@pytest.mark.asyncio
async def test_resolve_intent_blocked_by_gate_when_no_playback():
    ctrl = _volume_ctrl(stream_mode=False, radio_mode=False, stream_volume=0.5)
    agent = VolumeAgent(ctrl)

    bid = agent.resolve_intent("volume_down", {}, _ctx())

    assert bid is None
    assert ctrl.stream_volume == 0.5  # 未被調動


def test_resolve_intent_unknown_intent_name_returns_none():
    agent = VolumeAgent(_volume_ctrl(stream_mode=True))
    assert agent.resolve_intent("does_not_exist", {}, _ctx()) is None


def test_resolve_intent_mode_incompatible_returns_none():
    agent = VolumeAgent(_volume_ctrl(stream_mode=True))
    bid = agent.resolve_intent("volume_down", {}, _ctx(mode="game"))
    assert bid is None


@pytest.mark.asyncio
async def test_resolve_intent_passes_slots_to_handler():
    handled = {}

    class _Ctrl:
        async def _handle_find_song(self, mode, payload, speaker):
            handled["mode"] = mode
            handled["payload"] = payload
            handled["speaker"] = speaker

    agent = FindSongAgent(_Ctrl())
    bid = agent.resolve_intent("find_artist", {"artist": "周杰倫"}, _ctx())

    assert bid is not None
    await bid.handler()
    assert handled == {"mode": "find_artist", "payload": "周杰倫", "speaker": "Alice"}


def test_resolve_intent_missing_required_slot_returns_none():
    class _Ctrl:
        async def _handle_find_song(self, *a):
            pass

    agent = FindSongAgent(_Ctrl())
    assert agent.resolve_intent("find_artist", {}, _ctx()) is None
