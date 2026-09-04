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


def _ctx(query="", mode="normal", audio=False):
    return IntentContext(
        speaker="Alice", raw_text=query, query=query, original_raw=query,
        wake_intent=0.9, stream_active=False, game_mode=False,
        is_owner=False, now=0.0, mode=mode,
        dispatch_source="llm_rescue_audio" if audio else "regex",
    )


def _volume_ctrl(**overrides):
    defaults = dict(stream_mode=False, radio_mode=False, stream_volume=0.5,
                     VOL_MIN=0.01, VOL_MAX=1.0, play_tts=AsyncMock())
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


# ── audio-rescue per-agent 分支（T2 of /plan-eng-review 轉正計畫）─────────────

def test_playback_control_skip_via_audio_rescue_bypasses_query_filter():
    """regex 路徑：糊字 ctx.query → post_match_filter 的 is_short_skip_command 回
    False → resolve_intent 回 None。audio-rescue 路徑：LLM 已指名 skip_track，
    post_match_filter 直接放行。"""
    from intent_agents.playback_control_agent import PlaybackControlAgent

    agent = PlaybackControlAgent(SimpleNamespace(play_tts=AsyncMock()))
    garbled = "呃那個下一手什麼的"

    assert agent.resolve_intent("skip_track", {}, _ctx(garbled)) is None  # regex-shape

    bid = agent.resolve_intent("skip_track", {}, _ctx(garbled, audio=True))
    assert bid is not None
    assert bid.reason.startswith("audio_rescue:")


@pytest.mark.asyncio
async def test_music_rescue_play_uses_slot_not_garbled_query():
    from intent_agents.music_agent_v2 import MusicAgentV2

    calls = {}

    class _Ctrl:
        async def _safe_music_command(self, speaker, query, cmd):
            calls["query"] = query
            calls["cmd"] = cmd

    agent = MusicAgentV2(_Ctrl())
    ctx = _ctx("泡放 齊 力 香", audio=True)  # STT 糊字
    bid = agent.resolve_intent("rescue_play", {"song_query": "周杰倫 七里香"}, ctx)

    assert bid is not None
    await bid.handler()
    assert calls == {"query": "周杰倫 七里香", "cmd": "play"}


def test_music_rescue_play_missing_song_query_returns_none():
    from intent_agents.music_agent_v2 import MusicAgentV2

    agent = MusicAgentV2(object())
    assert agent.resolve_intent("rescue_play", {}, _ctx("糊字", audio=True)) is None
    assert agent.resolve_intent("rescue_play", {"song_query": "  "}, _ctx("x", audio=True)) is None


def test_music_rescue_play_never_matches_regex_bid_path():
    """rescue_play patterns=[] → 只有 resolve_intent 走得到，bid() 永不選它。"""
    from intent_agents.music_agent_v2 import MusicAgentV2

    agent = MusicAgentV2(object())
    b = agent.bid(_ctx("播放七里香"))
    assert not (b.reason or "").startswith("rescue_play")


@pytest.mark.asyncio
async def test_volume_state_gate_still_applies_under_audio_rescue():
    """audio-rescue 只放行 wake-side 守門；VolumeAgent「沒在播放不調音量」是
    state 守門，audio rescue 一樣要擋。"""
    agent = VolumeAgent(_volume_ctrl(stream_mode=False, radio_mode=False))
    assert agent.resolve_intent("volume_down", {}, _ctx(audio=True)) is None

    agent2 = VolumeAgent(_volume_ctrl(stream_mode=True, stream_volume=0.5))
    bid = agent2.resolve_intent("volume_down", {}, _ctx(audio=True))
    assert bid is not None
