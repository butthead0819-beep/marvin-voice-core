"""意圖漏斗步驟 2 第二批：刪除零流量路徑（系統狀態關鍵字、模仿秀、Gap Pre-check、
gap classifier、Presence Gate）。

條件（O8）：步驟 1 上線滿 7 天（2026-10-29 19:00）後 route 計數仍為 0；
10/20–10/30 bot_main.log 這幾條路徑的 log 也全是 0。

行為鎖：bus 沒人接之後，就算舊路徑的觸發條件成立，也一律走到 _stream_response
（Marvin 主 LLM）。filler 已由上游 _query_quality_gate 擋（weak_fillers / too_short）。
"""
from __future__ import annotations

import importlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.test_stream_response_characterization import _make_vc, _run


@pytest.fixture(autouse=True)
def _no_question_opener(monkeypatch):
    monkeypatch.setattr("cogs.voice_controller.try_question_opener", AsyncMock(return_value=False))


def _vc_with_query(monkeypatch, query):
    vc, _ph, _ch = _make_vc(monkeypatch, ["好。"])
    vc.bot.engine.conv_buffer.get_harvest.return_value = query
    vc._stream_response = AsyncMock()
    return vc


def _assert_reached_llm(vc):
    vc._intent_bus.dispatch.assert_awaited_once()
    vc._stream_response.assert_awaited_once()


# ── 行為：舊觸發條件成立也直達主 LLM ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_status_keyword_query_reaches_main_llm(monkeypatch):
    vc = _vc_with_query(monkeypatch, "系統狀態還剩多少")
    await _run(vc)
    _assert_reached_llm(vc)
    vc.play_tts.assert_not_called()


@pytest.mark.asyncio
async def test_imitate_query_reaches_main_llm(monkeypatch):
    import cogs.voice_controller as vcmod
    # 就算舊的模仿偵測器會命中，也不再有模仿秀分支
    vc = _vc_with_query(monkeypatch, "模仿一下showay講話")
    monkeypatch.setattr(vcmod, "detect_imitation_target", lambda q, p: "showay", raising=False)
    await _run(vc)
    _assert_reached_llm(vc)


@pytest.mark.asyncio
async def test_pa_query_leaking_past_bus_reaches_main_llm(monkeypatch):
    import cogs.voice_controller as vcmod
    # 舊 Gap Pre-check：PA 句漏到這裡會 silent return
    vc = _vc_with_query(monkeypatch, "幫我記一下明天要繳電費")
    monkeypatch.setattr(vcmod, "is_personal_assistant_query", lambda q: True, raising=False)
    await _run(vc)
    _assert_reached_llm(vc)


@pytest.mark.asyncio
async def test_gap_classifier_never_built_even_with_env(monkeypatch):
    import cogs.voice_controller as vcmod
    monkeypatch.setenv("MARVIN_GAP_CLASSIFIER", "1")
    factory = MagicMock()
    monkeypatch.setattr(vcmod, "make_groq_gap_classifier", factory, raising=False)
    vc = _vc_with_query(monkeypatch, "今天天氣如何呢")
    vc._shared_tier_router = MagicMock()
    await _run(vc)
    factory.assert_not_called()
    _assert_reached_llm(vc)


@pytest.mark.asyncio
async def test_no_intent_signal_still_reaches_main_llm(monkeypatch):
    import cogs.voice_controller as vcmod
    # 舊 Presence Gate：has_intent_signal=False → silent。現在交給上游 quality gate。
    vc = _vc_with_query(monkeypatch, "我覺得還好欸")
    monkeypatch.setattr(vcmod, "has_intent_signal", lambda q: False)
    await _run(vc)
    _assert_reached_llm(vc)


# ── 結構：死碼確實拔掉 ───────────────────────────────────────────────────────

def test_removed_voice_controller_methods():
    from cogs.voice_controller import VoiceController
    for name in ("_handle_voice_status_query", "_handle_voice_imitate_command",
                 "_route_gap_record", "_dm_owner_intent_gap"):
        assert not hasattr(VoiceController, name), name


def test_gap_notify_module_removed():
    with pytest.raises(ImportError):
        importlib.import_module("cogs.voice_controller_gap_notify")


def test_intent_gap_keeps_record_and_logger_only():
    import intent_gap
    assert hasattr(intent_gap, "IntentGapRecord")
    assert hasattr(intent_gap, "GapLogger")
    for name in ("make_groq_gap_classifier", "handle_intent_gap"):
        assert not hasattr(intent_gap, name), name


def test_impression_engine_keeps_speech_dna_only():
    import impression_engine
    assert hasattr(impression_engine, "get_speech_dna")
    for name in ("detect_imitation_target", "build_imitation_system_prompt", "_IMITATE_RE"):
        assert not hasattr(impression_engine, name), name


def test_followup_path_still_uses_has_intent_signal():
    # wake_followup 仍靠 has_intent_signal；Presence Gate 拔掉不能連 import 一起拔
    import cogs.voice_controller as vcmod
    import wake_intent_gate
    assert vcmod.has_intent_signal is wake_intent_gate.has_intent_signal
