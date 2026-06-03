"""DualSpeakAgent — Template B state-checking agent for Marmo dual-speak PoC.

驗證（每 distinct reason 一條 dense 0.0 / 一條 happy path / handler 整合）：
  - mode != normal/stream → mode_mismatch:{mode}
  - dispatch_source != marmo_inject → not_marmo_inject
  - payload None / 缺 text → missing_payload
  - VC cog 沒 loaded → vc_not_loaded
  - tts_queue_duration > 10s → backpressure_tts_storm
  - happy path：mode=normal + marmo_inject + payload+text + tts queue ok → bid 0.95
  - handler 正路：呼叫 generate_dual_dialogue → 呼叫 play_dual_dialogue(segments)
  - handler fallback：generate 回 None → play_tts(raw marmo_text) 走單 Marvin
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from intent_agents.dual_speak_agent import DualSpeakAgent
from intent_bus import IntentContext


def _ctx(*, mode="normal", dispatch_source="marmo_inject",
        payload=None, speaker="marmo_server"):
    if payload is None and dispatch_source == "marmo_inject":
        payload = {"text": "找到了第 7083 行", "job_id": "abc"}
    return IntentContext(
        speaker=speaker, raw_text="", query="", original_raw=None,
        wake_intent=None, stream_active=False, game_mode=(mode == "game"),
        is_owner=False, now=0.0, mode=mode,
        dispatch_source=dispatch_source, payload=payload,
    )


def _fake_bot(*, vc=None):
    bot = MagicMock()
    bot.cogs.get = MagicMock(side_effect=lambda name: vc if name == "VoiceController" else None)
    return bot


def _fake_vc(*, tts_queue_duration=0.0):
    vc = MagicMock()
    vc.tts_queue_duration = tts_queue_duration
    vc.play_dual_dialogue = AsyncMock(return_value=None)
    vc.play_tts = AsyncMock(return_value=None)
    return vc


def _make_agent(*, vc=None, llm_fn=None):
    bot = _fake_bot(vc=vc)
    llm_fn = llm_fn or AsyncMock(return_value='{"segments":[]}')
    return DualSpeakAgent(bot=bot, llm_fn=llm_fn)


# ── Mode gating ───────────────────────────────────────────────────────────────

def test_mode_game_dense_zero():
    agent = _make_agent(vc=_fake_vc())
    bid = agent.bid(_ctx(mode="game"))
    assert bid.confidence == 0.0
    assert bid.reason == "mode_mismatch:game"


def test_mode_normal_compatible():
    agent = _make_agent(vc=_fake_vc())
    bid = agent.bid(_ctx(mode="normal"))
    assert bid.confidence == 0.95


def test_mode_stream_compatible():
    agent = _make_agent(vc=_fake_vc())
    bid = agent.bid(_ctx(mode="stream"))
    assert bid.confidence == 0.95


# ── Dispatch source / payload gating ──────────────────────────────────────────

def test_not_marmo_inject_dense_zero():
    agent = _make_agent(vc=_fake_vc())
    bid = agent.bid(_ctx(dispatch_source="regex", payload=None))
    assert bid.confidence == 0.0
    assert bid.reason == "not_marmo_inject"


def test_marmo_inject_but_payload_none_dense_zero():
    agent = _make_agent(vc=_fake_vc())
    bid = agent.bid(_ctx(payload=None))
    # 注意：_ctx 預設只在 dispatch_source=marmo_inject 時填 payload；強迫 None 走這條
    # 用顯式 payload={} 避免預設邏輯
    bid = agent.bid(_ctx(payload={}))
    assert bid.confidence == 0.0
    assert bid.reason == "missing_payload"


def test_marmo_inject_payload_no_text_dense_zero():
    agent = _make_agent(vc=_fake_vc())
    bid = agent.bid(_ctx(payload={"job_id": "abc"}))
    assert bid.confidence == 0.0
    assert bid.reason == "missing_payload"


def test_marmo_inject_payload_empty_text_dense_zero():
    agent = _make_agent(vc=_fake_vc())
    bid = agent.bid(_ctx(payload={"text": "  "}))
    assert bid.reason == "missing_payload"


# ── VC / backpressure gating ─────────────────────────────────────────────────

def test_voice_controller_not_loaded_dense_zero():
    agent = DualSpeakAgent(bot=_fake_bot(vc=None), llm_fn=AsyncMock())
    bid = agent.bid(_ctx())
    assert bid.confidence == 0.0
    assert bid.reason == "vc_not_loaded"


def test_backpressure_tts_storm_dense_zero():
    agent = _make_agent(vc=_fake_vc(tts_queue_duration=12.5))
    bid = agent.bid(_ctx())
    assert bid.confidence == 0.0
    assert bid.reason == "backpressure_tts_storm"


def test_backpressure_at_threshold_still_bids():
    """tts_queue_duration == 10.0 不算 over；嚴格大於才 backpressure。"""
    agent = _make_agent(vc=_fake_vc(tts_queue_duration=10.0))
    bid = agent.bid(_ctx())
    assert bid.confidence == 0.95


# ── Happy path ────────────────────────────────────────────────────────────────

def test_happy_path_bid_095():
    agent = _make_agent(vc=_fake_vc())
    bid = agent.bid(_ctx())
    assert bid.confidence == 0.95
    assert bid.reason.startswith("dual_speak")
    assert bid.handler is not None


# ── Handler integration ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_handler_calls_generate_then_play_dual_on_success():
    """Happy: generate 回 segments → play_dual_dialogue 被呼叫，play_tts 不被叫。"""
    vc = _fake_vc()
    segments = [
        {"voice": "marvin", "text": "好的。"},
        {"voice": "marmo", "text": "閉嘴。"},
    ]
    with patch("intent_agents.dual_speak_agent.generate_dual_dialogue",
               new=AsyncMock(return_value=segments)) as gen_mock:
        agent = _make_agent(vc=vc)
        bid = agent.bid(_ctx())
        await bid.handler()
    gen_mock.assert_awaited_once()
    vc.play_dual_dialogue.assert_awaited_once_with(segments, interject=False)
    vc.play_tts.assert_not_called()


@pytest.mark.asyncio
async def test_payload_pattern_override_marvin_lead():
    """payload 帶 pattern=marvin_lead → handler 用 marvin_lead 呼叫 generate（Case B 測試後門）。"""
    vc = _fake_vc()
    segments = [{"voice": "marvin", "text": "a"}, {"voice": "marmo", "text": "b"}]
    with patch("intent_agents.dual_speak_agent.generate_dual_dialogue",
               new=AsyncMock(return_value=segments)) as gen_mock:
        agent = _make_agent(vc=vc)
        bid = agent.bid(_ctx(payload={"text": "x", "job_id": "j", "pattern": "marvin_lead"}))
        await bid.handler()
    assert gen_mock.await_args.kwargs["pattern"] == "marvin_lead"


@pytest.mark.asyncio
async def test_payload_pattern_default_marmo_lead():
    """payload 不帶 pattern → 預設 marmo_lead。"""
    vc = _fake_vc()
    segments = [{"voice": "marmo", "text": "a"}, {"voice": "marvin", "text": "b"}]
    with patch("intent_agents.dual_speak_agent.generate_dual_dialogue",
               new=AsyncMock(return_value=segments)) as gen_mock:
        agent = _make_agent(vc=vc)
        bid = agent.bid(_ctx())  # 預設 payload 無 pattern
        await bid.handler()
    assert gen_mock.await_args.kwargs["pattern"] == "marmo_lead"


@pytest.mark.asyncio
async def test_payload_pattern_invalid_falls_back_marmo_lead():
    """payload 帶非法 pattern → fallback marmo_lead，不爆。"""
    vc = _fake_vc()
    segments = [{"voice": "marmo", "text": "a"}, {"voice": "marvin", "text": "b"}]
    with patch("intent_agents.dual_speak_agent.generate_dual_dialogue",
               new=AsyncMock(return_value=segments)) as gen_mock:
        agent = _make_agent(vc=vc)
        bid = agent.bid(_ctx(payload={"text": "x", "job_id": "j", "pattern": "garbage"}))
        await bid.handler()
    assert gen_mock.await_args.kwargs["pattern"] == "marmo_lead"


@pytest.mark.asyncio
async def test_handler_fallback_to_single_marvin_when_generation_none():
    """generate 回 None（紅線 trip / LLM fail / parse fail）→ fallback play_tts 走單 Marvin。"""
    vc = _fake_vc()
    marmo_text = "Marmo 完成的任務原文"
    payload = {"text": marmo_text, "job_id": "j1"}
    with patch("intent_agents.dual_speak_agent.generate_dual_dialogue",
               new=AsyncMock(return_value=None)):
        agent = _make_agent(vc=vc)
        bid = agent.bid(_ctx(payload=payload))
        await bid.handler()
    vc.play_dual_dialogue.assert_not_called()
    vc.play_tts.assert_awaited_once()
    # play_tts 第一個 arg 是 text，要是原始 marmo_text
    args, _ = vc.play_tts.call_args
    assert args[0] == marmo_text


# ── declare_intents 必須回 [] (Template B 規範) ───────────────────────────────

def test_declare_intents_returns_empty():
    agent = _make_agent(vc=_fake_vc())
    assert agent.declare_intents() == []
