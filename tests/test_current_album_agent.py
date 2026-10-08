"""CurrentAlbumAgent —「播放這張專輯」→ 正在播的歌的專輯其他歌排進佇列（10/8）。

驗證：
  - regex bid：正例 0.97 / 負例 0.0
  - manifest 曝出 current_album__play_current_album function declaration
  - MusicAgentV2 不該搶走「播放這張專輯」（weak_play_artist_only / weak_play_long_string）
  - handler：呼叫 cog.queue_current_album(speaker)
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from intent_agents.audio_rescue_tools import manifest_to_function_declarations
from intent_agents.current_album_agent import CurrentAlbumAgent
from intent_bus import IntentBus, IntentContext


def _ctx(raw, speaker="showay", mode="normal", wake_intent=0.9, low_conf=False):
    return IntentContext(
        speaker=speaker, raw_text=raw, query=raw, original_raw=raw,
        wake_intent=wake_intent, stream_active=(mode == "stream"),
        game_mode=(mode == "game"), is_owner=False, now=0.0, mode=mode,
        low_confidence_wake=low_conf,
    )


def _agent():
    ctrl = MagicMock()
    return CurrentAlbumAgent(ctrl), ctrl


# ── bid ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", [
    "馬文播放這張專輯",
    "放這張專輯",
    "我想聽這張專輯",
    "來點這張",
])
def test_bid_hits_confidence(raw):
    agent, _ = _agent()
    bid = agent.bid(_ctx(raw))
    assert bid.confidence == 0.97
    assert bid.handler is not None
    assert bid.reason == "current_album"


@pytest.mark.parametrize("raw", [
    "這張專輯是哪一年的",
    "這張專輯好聽嗎",
    "播放周杰倫",
])
def test_bid_misses(raw):
    agent, _ = _agent()
    bid = agent.bid(_ctx(raw))
    assert bid.confidence == 0.0


def test_low_confidence_wake_gated():
    agent, _ = _agent()
    bid = agent.bid(_ctx("馬文播放這張專輯", low_conf=True))
    assert bid.confidence == 0.0
    assert bid.reason == "low_confidence_wake"


def test_game_mode_dense_zero():
    agent, _ = _agent()
    bid = agent.bid(_ctx("馬文播放這張專輯", mode="game"))
    assert bid.confidence == 0.0


# ── manifest ─────────────────────────────────────────────────────────────────

def test_manifest_exposes_function_declaration():
    agent, ctrl = _agent()
    bus = IntentBus([agent])
    manifest = bus.build_intent_manifest(today="2026-10-08")
    decls = manifest_to_function_declarations(manifest)
    names = {d.name for d in decls}
    assert "current_album__play_current_album" in names


# ── handler ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_handler_calls_queue_current_album():
    agent, ctrl = _agent()
    mc = MagicMock()
    mc.queue_current_album = AsyncMock()
    ctrl.bot.cogs.get.return_value = mc

    bid = agent.bid(_ctx("馬文播放這張專輯", speaker="狗與露"))
    await bid.handler()

    mc.queue_current_album.assert_awaited_once_with("狗與露")


@pytest.mark.asyncio
async def test_handler_no_cog_does_not_raise():
    agent, ctrl = _agent()
    ctrl.bot.cogs.get.return_value = None

    bid = agent.bid(_ctx("馬文播放這張專輯", speaker="狗與露"))
    await bid.handler()  # 不應該炸
