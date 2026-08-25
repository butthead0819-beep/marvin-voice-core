"""Tests for FrustrationAgent (TDD).

FrustrationAgent detects:
1. Frustration / dissatisfaction keywords ("到底", "有沒有在聽", "聽不懂", "不對", "不是這首", etc.)
2. Stuttered / repeated wake/action patterns ("把文文播放馬文播放張宇的文播放張宇的傘下")
And rescues the turn by sending raw audio to Audio LLM interpretation.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock
import pytest

from intent_bus import IntentContext, Bid
from intent_agents.frustration_agent import FrustrationAgent


def _make_ctx(
    query: str,
    audio: bytes | None = b"fake-pcm-wav-bytes",
    speaker: str = "大肚",
    prev_turn_audio: bytes | None = None,
) -> IntentContext:
    return IntentContext(
        speaker=speaker,
        raw_text=query,
        query=query,
        original_raw=query,
        wake_intent=0.9,
        stream_active=False,
        game_mode=False,
        is_owner=False,
        now=0.0,
        mode="normal",
        audio_wav_bytes=audio,
        prev_turn_audio_wav_bytes=prev_turn_audio,
    )


def test_frustration_agent_bids_zero_on_normal_query():
    agent = FrustrationAgent(controller=MagicMock())
    ctx = _make_ctx("今天天氣真好")
    bid = agent.bid(ctx)
    assert bid.confidence == 0.0
    assert "no_frustration" in bid.reason


def test_frustration_agent_bids_zero_when_no_audio():
    agent = FrustrationAgent(controller=MagicMock())
    ctx = _make_ctx("到底有沒有在聽", audio=None)
    bid = agent.bid(ctx)
    assert bid.confidence == 0.0
    assert "no_audio" in bid.reason


def test_frustration_agent_bids_high_on_explicit_frustration():
    agent = FrustrationAgent(controller=MagicMock())
    ctx = _make_ctx("到底有沒有在聽啊換一首好不好")
    bid = agent.bid(ctx)
    assert bid.confidence >= 0.90
    assert bid.name == "frustration"
    assert "frustration_pattern" in bid.reason
    assert bid.handler is not None


def test_frustration_agent_bids_high_on_stutter_repetition():
    # 2026-08-25 21:38 incident: '把文文播放馬文播放張宇的文播放張宇的傘下'
    agent = FrustrationAgent(controller=MagicMock())
    ctx = _make_ctx("把文文播放馬文播放張宇的文播放張宇的傘下")
    bid = agent.bid(ctx)
    assert bid.confidence >= 0.90
    assert bid.name == "frustration"
    assert "stutter_repetition" in bid.reason


@pytest.mark.asyncio
async def test_frustration_agent_handler_calls_rescue_and_executes():
    mock_ctrl = MagicMock()
    mock_ctrl.play_tts = AsyncMock()

    mock_rescue_agent = MagicMock()
    base_ctx = _make_ctx("播放張宇的傘下")
    rescued_ctx = replace(
        base_ctx,
        resolved_agent="music",
        resolved_intent="play_music",
        resolved_slots={"song_choice": "張宇 傘下"},
    )
    mock_rescue_agent.synthesize = AsyncMock(return_value=rescued_ctx)

    mock_music_agent = MagicMock()
    mock_music_agent.name = "music"
    mock_music_handler = AsyncMock()
    mock_music_agent.resolve_intent = MagicMock(return_value=Bid(
        name="music",
        confidence=0.95,
        handler=mock_music_handler,
        reason="audio_rescue:test",
    ))

    mock_intent_bus = MagicMock()
    mock_intent_bus.agents = [mock_music_agent]

    agent = FrustrationAgent(
        controller=mock_ctrl,
        audio_rescue_agent=mock_rescue_agent,
        intent_bus=mock_intent_bus,
    )

    ctx = _make_ctx("把文文播放馬文播放張宇的文播放張宇的傘下")
    bid = agent.bid(ctx)
    assert bid.handler is not None

    await bid.handler()

    mock_rescue_agent.synthesize.assert_awaited_once_with(ctx)
    mock_music_agent.resolve_intent.assert_called_once_with("play_music", {"song_choice": "張宇 傘下"}, rescued_ctx)
    mock_music_handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_explicit_frustration_rescues_prev_turn_audio_not_current():
    """挫折句本身（「到底要講幾遍」）通常不含歌名線索，真正該送去 Audio LLM
    的是「挫折產生之前」那輪失敗嘗試的音訊（prev_turn_audio_wav_bytes），
    而不是這句抱怨自己的音訊（audio_wav_bytes）。"""
    mock_rescue_agent = MagicMock()
    mock_rescue_agent.synthesize = AsyncMock(return_value=None)

    agent = FrustrationAgent(
        controller=MagicMock(),
        audio_rescue_agent=mock_rescue_agent,
        intent_bus=MagicMock(agents=[]),
    )

    ctx = _make_ctx(
        "到底要講幾遍",
        audio=b"complaint-only-audio",
        prev_turn_audio=b"actual-garbled-song-request-audio",
    )
    bid = agent.bid(ctx)
    assert "frustration_pattern" in bid.reason

    await bid.handler()

    mock_rescue_agent.synthesize.assert_awaited_once()
    sent_ctx = mock_rescue_agent.synthesize.await_args.args[0]
    assert sent_ctx.audio_wav_bytes == b"actual-garbled-song-request-audio"


@pytest.mark.asyncio
async def test_explicit_frustration_falls_back_to_current_audio_when_no_prev_turn():
    """沒有上一輪音訊快照時（例如挫折句就是第一句），退回用當輪音訊，
    維持舊行為不 regress。"""
    mock_rescue_agent = MagicMock()
    mock_rescue_agent.synthesize = AsyncMock(return_value=None)

    agent = FrustrationAgent(
        controller=MagicMock(),
        audio_rescue_agent=mock_rescue_agent,
        intent_bus=MagicMock(agents=[]),
    )

    ctx = _make_ctx("到底要講幾遍", audio=b"only-this-turn-audio", prev_turn_audio=None)
    bid = agent.bid(ctx)
    await bid.handler()

    sent_ctx = mock_rescue_agent.synthesize.await_args.args[0]
    assert sent_ctx.audio_wav_bytes == b"only-this-turn-audio"
