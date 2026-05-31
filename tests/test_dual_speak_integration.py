"""DualSpeak 整合測試：從 IntentBus.dispatch 起跑，驗證

  marmo_inject ctx → bus.dispatch → DualSpeakAgent.bid wins → handler
    → generate_dual_dialogue (mock LLM) → VoiceController.play_dual_dialogue

T9 marmo_server.py 的 HTTP-層接線（asyncio.create_task(bus.dispatch(ctx))）
留到下次部署、本測試從 ctx-level 起跑，等同模擬 marmo_server 已呼叫 dispatch。

也驗證 fallback：LLM 拋例外 → generate 回 None → handler 走 play_tts 單 Marvin。
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from intent_agents.dual_speak_agent import DualSpeakAgent
from intent_bus import IntentBus, IntentContext


def _ctx(payload):
    return IntentContext(
        speaker="marmo_server", raw_text="", query="", original_raw=None,
        wake_intent=None, stream_active=False, game_mode=False,
        is_owner=False, now=0.0, mode="normal",
        dispatch_source="marmo_inject", payload=payload,
    )


def _fake_bot_with_vc(vc):
    bot = MagicMock()
    bot.cogs.get = MagicMock(side_effect=lambda name: vc if name == "VoiceController" else None)
    return bot


def _fake_vc():
    vc = MagicMock()
    vc.tts_queue_duration = 0.0
    vc.play_dual_dialogue = AsyncMock(return_value=None)
    vc.play_tts = AsyncMock(return_value=None)
    return vc


@pytest.mark.asyncio
async def test_marmo_inject_to_dual_dialogue_full_path():
    """整路順走：bus.dispatch → DualSpeakAgent winner → generate → play_dual_dialogue."""
    vc = _fake_vc()
    bot = _fake_bot_with_vc(vc)

    # Mock LLM 回 valid dual segments JSON
    llm_payload = json.dumps({
        "segments": [
            {"voice": "marvin", "text": "好的。"},
            {"voice": "marmo", "text": "閉嘴他要結果。"},
        ]
    }, ensure_ascii=False)
    llm_fn = AsyncMock(return_value=llm_payload)

    agent = DualSpeakAgent(bot=bot, llm_fn=llm_fn)
    bus = IntentBus(agents=[agent])

    payload = {"text": "找到了第 7083 行", "job_id": "j-001"}
    winner = await bus.dispatch(_ctx(payload))

    assert winner is not None
    assert winner.name == "dual_speak"
    assert winner.confidence == 0.95

    # LLM 應被呼叫一次（user prompt 帶 marmo_text）
    llm_fn.assert_awaited_once()
    _, user_prompt = llm_fn.call_args.args
    assert "找到了第 7083 行" in user_prompt

    # play_dual_dialogue 應收到順序 [marvin, marmo] 的 segments
    vc.play_dual_dialogue.assert_awaited_once()
    segments = vc.play_dual_dialogue.call_args.args[0]
    assert len(segments) == 2
    assert segments[0]["voice"] == "marvin"
    assert segments[1]["voice"] == "marmo"
    assert segments[1]["text"] == "閉嘴他要結果。"

    # play_tts (fallback) 不該被呼叫
    vc.play_tts.assert_not_called()


@pytest.mark.asyncio
async def test_marmo_inject_llm_failure_falls_back_to_single_marvin():
    """LLM 例外 → generate 回 None → fallback play_tts 走單 Marvin 播原文。"""
    vc = _fake_vc()
    bot = _fake_bot_with_vc(vc)

    llm_fn = AsyncMock(side_effect=RuntimeError("LLM timeout"))
    agent = DualSpeakAgent(bot=bot, llm_fn=llm_fn)
    bus = IntentBus(agents=[agent])

    marmo_text = "Marmo 完成的原始任務文字"
    winner = await bus.dispatch(_ctx({"text": marmo_text, "job_id": "j-002"}))

    assert winner is not None
    vc.play_dual_dialogue.assert_not_called()
    vc.play_tts.assert_awaited_once()
    args, _ = vc.play_tts.call_args
    assert args[0] == marmo_text


@pytest.mark.asyncio
async def test_marmo_inject_red_line_falls_back_to_single_marvin():
    """LLM 回紅線命中內容 → generate_dual_dialogue 內部 filter 回 None → fallback。"""
    vc = _fake_vc()
    bot = _fake_bot_with_vc(vc)

    # 用真實紅線詞觸發 filter
    from services.dialogue_generation import RED_LINE_KEYWORDS
    bad_word = next(iter(RED_LINE_KEYWORDS))
    llm_payload = json.dumps({
        "segments": [
            {"voice": "marvin", "text": "好的。"},
            {"voice": "marmo", "text": f"你這個 {bad_word}"},
        ]
    }, ensure_ascii=False)
    llm_fn = AsyncMock(return_value=llm_payload)

    agent = DualSpeakAgent(bot=bot, llm_fn=llm_fn)
    bus = IntentBus(agents=[agent])

    marmo_text = "原始任務"
    winner = await bus.dispatch(_ctx({"text": marmo_text, "job_id": "j-003"}))

    assert winner is not None
    vc.play_dual_dialogue.assert_not_called()
    vc.play_tts.assert_awaited_once()
    assert vc.play_tts.call_args.args[0] == marmo_text


@pytest.mark.asyncio
async def test_marmo_inject_during_storm_no_dispatch_winner():
    """tts_queue_duration > 10s → bid 0.0 backpressure → 沒人贏，handler 不跑。"""
    vc = _fake_vc()
    vc.tts_queue_duration = 12.0  # storm
    bot = _fake_bot_with_vc(vc)

    llm_fn = AsyncMock()
    agent = DualSpeakAgent(bot=bot, llm_fn=llm_fn)
    bus = IntentBus(agents=[agent])

    winner = await bus.dispatch(_ctx({"text": "任務", "job_id": "j-004"}))

    # No winner above MIN_CONFIDENCE → None
    assert winner is None
    llm_fn.assert_not_called()
    vc.play_dual_dialogue.assert_not_called()
    vc.play_tts.assert_not_called()
