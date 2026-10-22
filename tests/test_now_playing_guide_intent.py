"""NowPlayingAgent 補「介紹一下這首歌」說法 + manifest_description（2026-10-22）。

背景：使用者講「馬文介紹一下這首歌」要的是 /guide_song 那種導聆，但
NowPlayingAgent 的 regex 原本不認「介紹」，而且沒有 manifest_description，
Audio Rescue 根本選不到 now_playing（見 docs/evals/direction-audio-entry.prereg.md）。
handler 本身不用改：叫了馬文（dispatch_source != "nowake"）本來就會背景觸發
MusicCog.speak_now_playing_guide。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from intent_agents.audio_rescue_tools import manifest_to_function_declarations
from intent_bus import IntentContext

pytestmark = pytest.mark.asyncio


def _ctx(query: str, dispatch_source: str = "regex") -> IntentContext:
    return IntentContext(
        speaker="alice",
        raw_text=query,
        query=query,
        original_raw=query,
        wake_intent=0.9,
        stream_active=True,
        game_mode=False,
        is_owner=False,
        now=0.0,
        mode="stream",
        dispatch_source=dispatch_source,
    )


def _ctrl(mc=None, current_info=None):
    ctrl = MagicMock()
    ctrl.stream_mode = True
    ctrl._current_stream_info = current_info or {"title": "夜曲", "uploader": "周杰倫"}
    ctrl._handle_music_info_query = AsyncMock()
    ctrl.active_text_channel = None
    ctrl._pending_followups = {}
    ctrl.bot = MagicMock()
    ctrl.bot.cogs = {"MusicCog": mc} if mc is not None else {}
    return ctrl


# ── pattern coverage：命中 ───────────────────────────────────────────────────


@pytest.mark.parametrize("query", [
    "介紹一下這首歌",
    "馬文介紹這首歌",
    "幫我介紹一下現在這首",
    "介紹一下現在播的",
    "這首歌在講什麼",
    "這首的背景是什麼",
])
async def test_guide_patterns_hit(query):
    from intent_agents.now_playing_agent import NowPlayingAgent
    agent = NowPlayingAgent(_ctrl())
    bid = agent.bid(_ctx(query))
    assert bid.confidence == 0.90, f"expected 0.90 for {query!r}, got {bid.confidence}"


# ── pattern coverage：不命中 ─────────────────────────────────────────────────


@pytest.mark.parametrize("query", [
    "介紹一下周杰倫",
    "介紹一下你自己",
    "幫我介紹一間餐廳",
])
async def test_guide_patterns_miss(query):
    from intent_agents.now_playing_agent import NowPlayingAgent
    agent = NowPlayingAgent(_ctrl())
    bid = agent.bid(_ctx(query))
    assert bid.confidence == 0.0


# ── manifest_description：Audio Rescue 選得到 ───────────────────────────────


def test_manifest_description_is_non_empty():
    from intent_agents.now_playing_agent import NowPlayingAgent
    agent = NowPlayingAgent(_ctrl())
    schema = agent.declare_intents()[0]
    assert schema.manifest_description.strip() != ""


def test_manifest_exposes_now_playing_as_function_declaration():
    from intent_agents.now_playing_agent import NowPlayingAgent
    agent = NowPlayingAgent(_ctrl())
    schema = agent.declare_intents()[0]
    manifest = {
        "version": "test",
        "agents": [
            {
                "name": agent.name,
                "intents": [
                    {"name": schema.name, "required_slots": list(schema.required_slots),
                     "reason_template": schema.reason_template,
                     "description": schema.manifest_description},
                ],
            },
        ],
    }
    decls = manifest_to_function_declarations(manifest)
    names = {d.name for d in decls}
    assert f"{agent.name}__{schema.name}" in names


# ── handler：叫了馬文觸發導聆 ─────────────────────────────────────────────────


async def test_handler_triggers_guide_when_called_by_name():
    from intent_agents.now_playing_agent import NowPlayingAgent
    mc = MagicMock()
    mc.speak_now_playing_guide = AsyncMock()
    ctrl = _ctrl(mc=mc)
    agent = NowPlayingAgent(ctrl)
    bid = agent.bid(_ctx("介紹一下這首歌", dispatch_source="regex"))

    await bid.handler()
    assert agent._guide_task is not None
    await agent._guide_task

    ctrl._handle_music_info_query.assert_called_once()
    mc.speak_now_playing_guide.assert_awaited_once_with(ctrl._current_stream_info)
