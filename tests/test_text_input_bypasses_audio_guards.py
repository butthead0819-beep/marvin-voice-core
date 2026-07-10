"""
tests/test_text_input_bypasses_audio_guards.py

TDD：文字輸入（Siri/stdin，is_text_input=True）非音訊回授，不該被為「語音」設計的
兩道守衛擋下：
(A) Echo Guard（_apply_wake_guards）——播音樂時仍要能用文字下「停/下一首」
(B) Confirmation 等後續語音（_confirmation_flow）——文字是一次送完的完整指令，
    短句不該被當「只喊喚醒詞」去等後續語音而逾時
對 MagicMock 假 self 直接呼叫方法（mirror test_music_echo_guard.py）。
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from cogs.voice_controller import VoiceController


# ── (A) Echo Guard bypass ────────────────────────────────────────────────────

def _make_guard_self(*, is_playing_audio=True):
    fake = MagicMock()
    fake.processed_wake_segments = {}
    fake.last_wake_time = {}
    fake._wake_response_pending = False
    fake._wake_accepted_time = 0.0
    fake._storm_active = False
    fake._storm_last_wake_time = 0.0
    fake._wake_burst_times = []
    fake.is_playing_audio = is_playing_audio
    fake._tts_echo_cooldown_until = 0.0
    fake._current_tts_text = ""
    fake._last_global_wake_time = 0.0
    fake.game_mode = False
    fake._nudges = MagicMock()
    fake._nudges.signal = MagicMock(return_value=False)
    # staticmethod：MagicMock 預設回 truthy Mock 會誤繞 echo，明確設 False
    fake._strong_voice_bypass_echo = MagicMock(return_value=False)
    return fake


def test_text_input_bypasses_echo_guard_during_music():
    """播音樂中（is_playing_audio）文字喚醒 → is_echo False、is_fast 保留。"""
    fake = _make_guard_self(is_playing_audio=True)
    is_fast, is_echo = VoiceController._apply_wake_guards(
        fake, "狗與露", "馬文停", 123.0, "A", True,
        None, None, None, None, is_text_input=True)
    assert is_echo is False
    assert is_fast is True


def test_text_input_bypasses_response_lock_and_storm():
    """關鍵回歸：前一指令開了 Response Lock（+ Storm）→ 文字「停」仍放行。

    Response Lock/Storm 為防音訊快速重複喚醒而設；播音樂時會壓掉文字下的「停」。
    """
    fake = _make_guard_self(is_playing_audio=True)
    fake._wake_response_pending = True   # 前一指令的回應鎖仍在
    fake._wake_accepted_time = 1e18      # 遠未逾時
    fake._storm_active = True            # 風暴壓抑中
    fake._storm_last_wake_time = 1e18
    is_fast, is_echo = VoiceController._apply_wake_guards(
        fake, "狗與露", "馬文停", 123.0, "A", True,
        None, None, None, None, is_text_input=True)
    assert is_fast is True
    assert is_echo is False


def test_voice_input_still_echo_guarded_during_music():
    """回歸對照：同情境但語音（is_text_input=False）→ Echo Guard 照舊擋下。"""
    fake = _make_guard_self(is_playing_audio=True)
    is_fast, is_echo = VoiceController._apply_wake_guards(
        fake, "狗與露", "馬文停", 123.0, "A", True,
        None, None, None, None, is_text_input=False)
    assert is_echo is True
    assert is_fast is False


# ── (B) Confirmation 不等後續語音 ─────────────────────────────────────────────

def _make_confirm_self():
    fake = MagicMock()
    fake.speaker_dialogue_states = {}
    fake._strip_wake_word = lambda t: (t or "").replace("馬文", "")
    fake._detect_music_command = lambda t: None
    fake._get_music_fastpath = lambda: None
    fake._CONFIRM_WAIT_TIMEOUT = 0.05
    fake._CONFIRM_CLEAN_TIMEOUT = 2.0
    fake.play_tts = AsyncMock()
    fake.bot.router.clean_stt_text = AsyncMock(return_value={"text": "你好嗎"})
    return fake


# ── (C) Quality gate 動機：純語音閘會擋短控制詞 → 故文字輸入要跳過 ─────────────

def test_quality_gate_rejects_bare_stop_char():
    """記錄動機：品質閘把 1 字「停」當 too_short 丟 → _process_queued_query 對
    文字輸入跳過此閘，才能讓控制台的「停」按鈕生效。"""
    fake = MagicMock()
    fake._strip_wake_word = lambda t: t
    should, reason = VoiceController._query_quality_gate(fake, "停")
    assert should is False
    assert reason == "too_short"


@pytest.mark.asyncio
async def test_text_input_short_query_returns_without_waiting():
    """文字短句「馬文你好嗎」→「你好嗎」(3字) → 直接當完整指令回傳，不等、不逾時。"""
    fake = _make_confirm_self()
    result = await VoiceController._confirmation_flow(
        fake, "狗與露", 123.0, initial_text="馬文你好嗎", is_text_input=True)
    assert result == "你好嗎"
    fake.play_tts.assert_not_awaited()  # 沒逾時→沒播「沒聽清楚」


@pytest.mark.asyncio
async def test_voice_input_short_query_waits_and_times_out():
    """回歸對照：語音短句無後續 → 等待逾時 → 播「沒聽清楚」→ 回 None。"""
    fake = _make_confirm_self()
    fake.bot.engine.conv_buffer.get_harvest.return_value = ""  # 無 harvest
    result = await VoiceController._confirmation_flow(
        fake, "狗與露", 123.0, initial_text="馬文你好嗎", is_text_input=False)
    assert result is None
    import asyncio
    await asyncio.sleep(0.01)  # 讓逾時分支的 create_task(play_tts) 跑一圈
    fake.play_tts.assert_awaited_once()
