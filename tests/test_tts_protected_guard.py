"""_protected_tts_window() 回歸測試（Phase A 重構：抽出共用 choke point）。

歷史上「進入 protected TTS 窗口時忘記 save/restore _tts_protected」這類 bug
出過至少 3 次（PR#84/86/87）。這裡鎖住 context manager 本身的最小契約：
進入時設 True、離開時（含例外路徑）還原成先前的值，不論先前是 False 或 True。

Phase B（本檔下半部）補三個 Phase A 刻意跳過的呼叫點，這次改成用
`self._protected_tts_window()`（不是純 save/restore 樣板，屬於行為修正）：
    1. voice_controller_connection.py::handle_summon 進場招呼
       —— 原本完全沒有 try/finally，play_tts 中途拋例外會讓 _tts_protected
       卡在 True 永遠不還原（之後所有 TTS 都被當成 protected，guard 失效）。
    2. voice_controller.py::_handle_nemoclaw_query NemoClaw 回應 TTS
       —— 原本 finally 硬設 False，若外層已在另一個 protected 窗口中（巢狀），
       會把外層的保護提早清掉。
    3. music_cog_tail_dj.py::_maybe_play_dj_interjection DJ 尾段口白
       —— 同上，finally 硬設 False 的巢狀風險。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from cogs.voice_controller_connection import ConnectionMixin
from cogs.voice_controller_playback import PlaybackMixin


class _Dummy(PlaybackMixin):
    """只需要 self._tts_protected 屬性；PlaybackMixin 沒有 __init__，裸繼承即可。"""


def test_protected_tts_window_sets_true_on_enter_and_restores_false():
    obj = _Dummy()
    obj._tts_protected = False
    with obj._protected_tts_window():
        assert obj._tts_protected is True
    assert obj._tts_protected is False


def test_protected_tts_window_restores_prior_true():
    obj = _Dummy()
    obj._tts_protected = True
    with obj._protected_tts_window():
        assert obj._tts_protected is True
    assert obj._tts_protected is True


def test_protected_tts_window_restores_on_exception():
    obj = _Dummy()
    obj._tts_protected = False
    with pytest.raises(RuntimeError):
        with obj._protected_tts_window():
            assert obj._tts_protected is True
            raise RuntimeError("boom")
    assert obj._tts_protected is False


def test_protected_tts_window_defaults_missing_attr_to_false():
    """呼叫端沒先設過 _tts_protected 時（getattr 預設 False）也不炸、且正確還原。"""
    obj = _Dummy()
    assert not hasattr(obj, "_tts_protected")
    with obj._protected_tts_window():
        assert obj._tts_protected is True
    assert obj._tts_protected is False


# ── Phase B：3 個呼叫點的行為修正回歸測試 ──────────────────────────────────


class _SummonHarness(ConnectionMixin, PlaybackMixin):
    """handle_summon 所需的最小依賴：vc 查不到（跳過進場音樂）、greeting 走
    mock router、其餘副作用（人格排班/daily review）全部 mock 掉。"""

    def __init__(self):
        self.bot = MagicMock()
        self.bot.voice_clients = []  # → vc 查不到，略過進場音樂分支
        self.bot.router.generate_greeting = AsyncMock(return_value="哈囉，我來了")
        self._pending_greeting_task = None
        self.active_text_channel = None
        self.stt_logger = MagicMock()
        self._maybe_apply_daily_persona_schedule = AsyncMock()
        self._maybe_run_daily_review = AsyncMock()
        self.temperature_monitor = None
        self._tts_interrupted = False
        self._tts_protected = False


@pytest.mark.asyncio
async def test_handle_summon_restores_tts_protected_after_play_tts_exception():
    """修 bug 1：原本 self._tts_protected = True 後直接 await play_tts，完全沒有
    try/finally——play_tts 中途拋例外時 _tts_protected 會卡在 True 永遠不還原。
    改用 _protected_tts_window() 後例外仍會傳播，但旗標要正確還原成 False。"""
    harness = _SummonHarness()
    harness.play_tts = AsyncMock(side_effect=RuntimeError("boom"))

    with pytest.raises(RuntimeError):
        await harness.handle_summon()

    assert harness._tts_protected is False


class _NemoClawHarness(PlaybackMixin):
    """_handle_nemoclaw_query 所需的最小依賴：owner 驗證通過、無去重命中、
    文字頻道略過（無 placeholder）、cover 關閉、_ask_nemoclaw 直接回應。"""

    def __init__(self):
        self._is_owner_speaker = MagicMock(return_value=True)
        self._nemo_dedup = {}
        self._nemo_lock = asyncio.Lock()
        self._play_ack = AsyncMock()
        self.active_text_channel = None
        self._ask_nemoclaw = AsyncMock(return_value="測試回應內容")
        self.stt_logger = MagicMock()
        self._plan12 = False
        self.play_tts = AsyncMock()
        self._tts_interrupted = False
        self._tts_protected = True  # 外層已在另一個 protected 窗口中（模擬巢狀）
        self._storm_active = True
        self._wake_burst_times = MagicMock()
        self._storm_last_wake_time = 123.0


@pytest.mark.asyncio
async def test_nemoclaw_tts_restores_outer_nested_protected_window_not_cleared(monkeypatch):
    """修 bug 2：原本 finally 硬設 self._tts_protected = False，若呼叫前已經處於
    外層 protected 窗口中（巢狀），會把外層的保護提早清掉。改用
    _protected_tts_window() 後離開時要還原成呼叫前的值（True），而不是被清成 False。"""
    monkeypatch.delenv("NEMOCLAW_COVER", raising=False)
    from cogs.voice_controller import VoiceController

    harness = _NemoClawHarness()
    await VoiceController._handle_nemoclaw_query(harness, "Jack", "幫我查一下今天天氣")

    assert harness._tts_protected is True
    harness.play_tts.assert_awaited_once()
    # 巢狀行為修正之外，原本 finally 裡的 Wake Storm 清除副作用要維持不變。
    harness._wake_burst_times.clear.assert_called_once()
    assert harness._storm_active is False
    assert harness._storm_last_wake_time == 0.0


class _DjTailVC(PlaybackMixin):
    """_maybe_play_dj_interjection 所需的最小 vc 依賴（文字分支，不吃 audio_path，
    避免另外 mock _get_puck_client）。"""

    def __init__(self):
        self.play_tts = AsyncMock()
        self.play_dj_on_tts_layer = AsyncMock(return_value=True)
        self._intimate_mode = False
        self._tts_protected = True  # 外層已在另一個 protected 窗口中（模擬巢狀）


@pytest.mark.asyncio
async def test_dj_tail_interjection_restores_outer_nested_protected_window_not_cleared():
    """修 bug 3：同 bug 2，finally 硬設 vc._tts_protected = False 的巢狀風險。"""
    from cogs.music_cog_tail_dj import MusicTailDJMixin

    vc = _DjTailVC()

    class _Harness(MusicTailDJMixin):
        def _vc(self):
            return vc

    harness = _Harness()
    await harness._maybe_play_dj_interjection({"text": "安可安可", "audio_path": None})

    assert vc._tts_protected is True
    vc.play_tts.assert_awaited_once()
