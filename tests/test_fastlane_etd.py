"""
TDD —— 對談極速通道 F：對談窗內 Semantic ETD Track B-2 跳過 Groq（PLAN_fast_followup_conversation.md R7/R10）。

沿用 tests/test_wake_session_wiring.py 的 _FakeCog 寫法；接線斷言沿用
tests/test_response_lock_release.py 的 inspect.getsource 寫法。
"""
from __future__ import annotations

import inspect
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import cogs.voice_controller as vcmod
from cogs.voice_controller_wake_session import WakeSessionMixin
from etd_clean_reuse import remember_etd, take_etd, clean_for_worker
from wake_session import WakeSession


class _FakeCog(WakeSessionMixin):
    def __init__(self):
        self._wake_session = None
        self._mixer = None
        self._wake_response_pending = False
        self.bot = MagicMock()
        self.bot.router.clean_stt_text = AsyncMock(return_value={"text": "cleaned", "is_complete": True})


def _open_session(cog, speaker="A", now=0.0):
    cog._wake_session = WakeSession()
    cog._wake_session.on_wake_accepted(speaker, now)
    cog._wake_session.on_replied(now)


# ── 1. 窗內：跳過 Groq，回合成結果 ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_in_window_skips_groq_and_returns_synthetic_result():
    cog = _FakeCog()
    _open_session(cog, "A", now=0.0)
    with patch("time.time", return_value=1.0):
        result = await cog._etd_semantic_check("A", "幫我查明天天氣")
    assert result == {"text": "幫我查明天天氣", "is_complete": True}
    cog.bot.router.clean_stt_text.assert_not_awaited()


# ── 2. 窗外：呼叫 clean_stt_text 並回它的結果 ─────────────────────────────────

@pytest.mark.asyncio
async def test_out_of_window_other_speaker_calls_groq():
    cog = _FakeCog()
    _open_session(cog, "A", now=0.0)
    with patch("time.time", return_value=1.0):
        result = await cog._etd_semantic_check("B", "幫我查明天天氣")
    cog.bot.router.clean_stt_text.assert_awaited_once_with("幫我查明天天氣")
    assert result == {"text": "cleaned", "is_complete": True}


@pytest.mark.asyncio
async def test_no_session_calls_groq():
    cog = _FakeCog()
    with patch("time.time", return_value=1.0):
        result = await cog._etd_semantic_check("A", "幫我查明天天氣")
    cog.bot.router.clean_stt_text.assert_awaited_once_with("幫我查明天天氣")
    assert result == {"text": "cleaned", "is_complete": True}


# ── 3. env 關閘：MARVIN_FASTLANE_SKIP_CLEAN=0 → 窗內仍呼叫 Groq ───────────────

@pytest.mark.asyncio
async def test_env_disabled_still_calls_groq_even_in_window(monkeypatch):
    monkeypatch.setenv("MARVIN_FASTLANE_SKIP_CLEAN", "0")
    cog = _FakeCog()
    _open_session(cog, "A", now=0.0)
    with patch("time.time", return_value=1.0):
        result = await cog._etd_semantic_check("A", "幫我查明天天氣")
    cog.bot.router.clean_stt_text.assert_awaited_once_with("幫我查明天天氣")
    assert result == {"text": "cleaned", "is_complete": True}


# ── 4. remember_etd + take_etd/clean_for_worker 真的重用，不再打 cleaner ──────

@pytest.mark.asyncio
async def test_synthetic_result_reused_by_worker_without_second_cleaner_call():
    ctrl = MagicMock()
    ctrl._strip_wake_word = lambda t: t
    ctrl.bot.router.clean_stt_text = AsyncMock(return_value={"text": "SHOULD_NOT_BE_CALLED"})

    raw_text = "幫我查明天天氣"
    synthetic_res = {"text": raw_text, "is_complete": True}
    remember_etd(ctrl, "A", raw_text, synthetic_res, now=time.time())

    cleaned = await clean_for_worker(ctrl, raw_text, speaker="A")

    assert cleaned == raw_text
    ctrl.bot.router.clean_stt_text.assert_not_awaited()


# ── 5. 接線：_apply_semantic_etd 原始碼已改走 _etd_semantic_check ────────────

def test_apply_semantic_etd_wired_to_fastlane_check():
    src = inspect.getsource(vcmod)
    assert "res = await self._etd_semantic_check(speaker, combined_text)" in src
    assert "res = await self.bot.router.clean_stt_text(combined_text)" not in src


# ── 6. B-1 不被跳過：思考拖延詞結尾 → 未完成，且不呼叫 _etd_semantic_check ───

def _make_vc():
    from cogs.voice_controller import VoiceController
    vc = VoiceController.__new__(VoiceController)
    vc.user_sentence_buffer = {}
    vc.bot = MagicMock()
    vc._etd_semantic_check = AsyncMock(return_value={"text": "x", "is_complete": True})
    return vc


@pytest.mark.asyncio
async def test_thinking_word_in_window_still_buffers_without_calling_fastlane_check():
    vc = _make_vc()
    result = await vc._apply_semantic_etd(
        "A", "我要點歌然後", 100.0, prosody_data=None, wav_bytes=b"", track="B",
    )
    assert result is None
    assert "A" in vc.user_sentence_buffer
    vc._etd_semantic_check.assert_not_awaited()
    buf = vc.user_sentence_buffer.pop("A")
    if buf.get("task"):
        buf["task"].cancel()


# ── 7. 窗外喚醒句：跳過 Groq，直接回合成結果 ─────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("wake_phrase", [
    "馬文下一首",
    "馬文停止播放",
    "馬文播放陳華的如果我很平庸",
    "馬文收到請迴答",
    "法文下一首",
])
async def test_out_of_window_wake_phrase_skips_groq(wake_phrase):
    cog = _FakeCog()
    with patch("time.time", return_value=1.0):
        result = await cog._etd_semantic_check("A", wake_phrase)
    assert result == {"text": wake_phrase, "is_complete": True}
    cog.bot.router.clean_stt_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_env_disabled_wake_phrase_still_calls_groq(monkeypatch):
    monkeypatch.setenv("MARVIN_FASTLANE_SKIP_CLEAN", "0")
    cog = _FakeCog()
    with patch("time.time", return_value=1.0):
        result = await cog._etd_semantic_check("A", "馬文下一首")
    cog.bot.router.clean_stt_text.assert_awaited_once_with("馬文下一首")
    assert result == {"text": "cleaned", "is_complete": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("non_wake_phrase", [
    "幫我查明天天氣",
    "今天天氣真好",
])
async def test_out_of_window_non_wake_phrase_calls_groq(non_wake_phrase):
    cog = _FakeCog()
    with patch("time.time", return_value=1.0):
        result = await cog._etd_semantic_check("A", non_wake_phrase)
    cog.bot.router.clean_stt_text.assert_awaited_once_with(non_wake_phrase)
    assert result == {"text": "cleaned", "is_complete": True}

