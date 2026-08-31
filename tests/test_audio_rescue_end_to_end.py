"""In-process 整合測試：mock 掉 Gemini（synthesize），用真的 agent 跑
IntentBus._maybe_rescue → _execute_resolved_intent → agent.resolve_intent →
handler。三類意圖（查資料 / 控音樂 / 點歌）各一條，驗證接線到位、handler
拿到的是 LLM 填的 slot 不是糊掉的 ctx.query。

T8 of /plan-eng-review audio-rescue 轉正計畫。
"""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import intent_agents.grounded_qa_agent as gqa_mod
from intent_agents.grounded_qa_agent import GroundedQAAgent
from intent_agents.music_agent_v2 import MusicAgentV2
from intent_agents.playback_control_agent import PlaybackControlAgent
from intent_bus import IntentBus, IntentContext


class _StubRescue:
    """synthesize 回一個已解析好的 ctx（模擬 Gemini 已聽音訊選好 tool）。"""
    name = "AudioRescue"

    def __init__(self, agent_name, intent_name, slots):
        self._agent, self._intent, self._slots = agent_name, intent_name, slots

    async def synthesize(self, ctx):
        return replace(
            ctx, depth=ctx.depth + 1, dispatch_source="llm_rescue_audio",
            resolved_agent=self._agent, resolved_intent=self._intent,
            resolved_slots=self._slots,
        )


def _ctx(garbled: str):
    # ctx.query 永遠是糊掉的 STT —— 若 handler 用到它就會露餡
    return IntentContext(
        speaker="Alice", raw_text=garbled, query=garbled, original_raw=garbled,
        wake_intent=0.9, stream_active=True, game_mode=False, is_owner=False,
        now=0.0, mode="normal", depth=0, audio_wav_bytes=b"fake-wav",
    )


def _ctrl():
    return SimpleNamespace(
        _safe_music_command=AsyncMock(),
        play_tts=AsyncMock(),
        _quick_ack=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_query_data_routes_to_grounded_qa_with_clean_topic(monkeypatch):
    run_gqa = AsyncMock()
    monkeypatch.setattr(gqa_mod, "run_grounded_qa", run_gqa)

    ctrl = _ctrl()
    bus = IntentBus(
        [GroundedQAAgent(ctrl)],
        llm_rescue_agent=_StubRescue("grounded_qa", "factual_question",
                                     {"topic": "珠穆朗瑪峰有多高"}),
    )

    winner = await bus.dispatch(_ctx(garbled="豬木朗瑪峰 有 多 高 阿"))

    assert winner is not None and winner.name == "grounded_qa"
    run_gqa.assert_awaited_once()
    # 用 LLM 填的乾淨 topic，不是糊字
    assert run_gqa.await_args.args[2] == "珠穆朗瑪峰有多高"


@pytest.mark.asyncio
async def test_control_music_routes_to_playback_control():
    music_cog = SimpleNamespace(_safe_music_command=AsyncMock())
    ctrl = _ctrl()
    ctrl.bot = SimpleNamespace(cogs={"MusicCog": music_cog})
    bus = IntentBus(
        [PlaybackControlAgent(ctrl)],
        llm_rescue_agent=_StubRescue("playback_control", "skip_track", {}),
    )

    winner = await bus.dispatch(_ctx(garbled="呃 那個 下一手 什麼 的"))

    assert winner is not None and winner.name == "playback_control"
    music_cog._safe_music_command.assert_awaited_once()
    assert music_cog._safe_music_command.await_args.args[2] == "skip"


@pytest.mark.asyncio
async def test_request_music_routes_to_rescue_play_with_clean_query():
    ctrl = _ctrl()
    bus = IntentBus(
        [MusicAgentV2(ctrl)],
        llm_rescue_agent=_StubRescue("music", "rescue_play",
                                     {"song_query": "周杰倫 七里香"}),
    )

    winner = await bus.dispatch(_ctx(garbled="泡 放 齊 力 香"))

    assert winner is not None and winner.name == "music"
    ctrl._safe_music_command.assert_awaited_once()
    speaker, query, cmd = ctrl._safe_music_command.await_args.args
    assert (query, cmd) == ("周杰倫 七里香", "play")  # slot，不是糊字


@pytest.mark.asyncio
async def test_shadow_mode_all_three_observe_only(monkeypatch):
    run_gqa = AsyncMock()
    monkeypatch.setattr(gqa_mod, "run_grounded_qa", run_gqa)
    ctrl = _ctrl()
    bus = IntentBus(
        [GroundedQAAgent(ctrl), PlaybackControlAgent(ctrl), MusicAgentV2(ctrl)],
        llm_rescue_agent=_StubRescue("music", "rescue_play", {"song_query": "稻香"}),
        rescue_shadow_mode=True,
    )

    winner = await bus.dispatch(_ctx(garbled="稻 香"))

    assert winner is None
    ctrl._safe_music_command.assert_not_awaited()
    run_gqa.assert_not_awaited()
