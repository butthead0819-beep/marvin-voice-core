"""
Response Lock 解鎖補回測試（1b790f9 刪掉 play_tts 結束解鎖後的回歸修復）。

涵蓋 WakeSessionMixin 新增的三個方法：_release_response_lock /
_process_query_task_locked / _release_lock_after，以及接線點
（_query_worker_loop 走 locked 版、WakeShortcut 直派用 _release_lock_after 包住）。
"""
from __future__ import annotations

import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock

from cogs.voice_controller_wake_session import WakeSessionMixin


class _FakeVC(WakeSessionMixin):
    def __init__(self, **over):
        self._wake_response_pending = False
        for k, v in over.items():
            setattr(self, k, v)


# ── _release_response_lock ───────────────────────────────────────────────
def test_release_response_lock_noop_when_already_false(caplog):
    vc = _FakeVC(_wake_response_pending=False)
    with caplog.at_level("INFO"):
        vc._release_response_lock("query_done")
    assert vc._wake_response_pending is False
    assert "🔓" not in caplog.text


def test_release_response_lock_clears_when_pending(caplog):
    vc = _FakeVC(_wake_response_pending=True)
    with caplog.at_level("INFO"):
        vc._release_response_lock("query_done")
    assert vc._wake_response_pending is False
    assert "🔓" in caplog.text


# ── _process_query_task_locked ───────────────────────────────────────────
@pytest.mark.asyncio
async def test_process_query_task_locked_releases_on_success():
    vc = _FakeVC(_wake_response_pending=True)
    vc._process_query_task = AsyncMock(return_value=None)
    await vc._process_query_task_locked({"speaker": "A"})
    vc._process_query_task.assert_awaited_once_with({"speaker": "A"})
    assert vc._wake_response_pending is False


@pytest.mark.asyncio
async def test_process_query_task_locked_releases_on_exception():
    vc = _FakeVC(_wake_response_pending=True)
    vc._process_query_task = AsyncMock(side_effect=RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        await vc._process_query_task_locked({"speaker": "A"})
    assert vc._wake_response_pending is False


# ── _release_lock_after ──────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_release_lock_after_releases_on_success():
    vc = _FakeVC(_wake_response_pending=True)

    async def _ok():
        return "done"

    await vc._release_lock_after(_ok())
    assert vc._wake_response_pending is False


@pytest.mark.asyncio
async def test_release_lock_after_releases_on_exception():
    vc = _FakeVC(_wake_response_pending=True)

    async def _boom():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await vc._release_lock_after(_boom())
    assert vc._wake_response_pending is False


# ── 接線：_query_worker_loop 走 locked 版 ─────────────────────────────────
@pytest.mark.asyncio
async def test_query_worker_loop_calls_locked_version():
    from cogs.voice_controller import VoiceController

    vc = VoiceController.__new__(VoiceController)
    vc.query_queue = asyncio.Queue()
    vc._process_query_task_locked = AsyncMock(side_effect=asyncio.CancelledError())
    await vc.query_queue.put({"speaker": "A"})

    await vc._query_worker_loop()

    vc._process_query_task_locked.assert_awaited_once_with({"speaker": "A"})


# ── 端到端：A 喚醒接受→解鎖後，5 秒後 B 的 fast 喚醒不被壓 ───────────────────
def _make_vc_for_guards(**over):
    from cogs.voice_controller import VoiceController

    vc = VoiceController.__new__(VoiceController)
    vc.processed_wake_segments = {}
    vc.last_wake_time = {}
    vc._wake_response_pending = False
    vc._wake_accepted_time = 0.0
    vc._storm_active = False
    vc._storm_last_wake_time = 0.0
    vc._wake_burst_times = []
    vc._last_global_wake_time = 0.0
    vc._is_playing_audio = False
    vc._tts_echo_cooldown_until = 0.0
    vc._current_tts_text = ""
    vc.game_mode = False
    vc._nudges = MagicMock()
    vc._nudges.signal.return_value = False
    vc._send_noise_nudge = AsyncMock()
    for k, v in over.items():
        setattr(vc, k, v)
    return vc


def _call_guards(vc, speaker, now, is_fast):
    fusion = MagicMock()
    fusion.is_open.return_value = False
    return vc._apply_wake_guards(speaker, "馬文你好", now, None, is_fast,
                                  fusion, "voice", 0.9, 0.8)


def test_response_lock_released_after_query_done_lets_next_speaker_fast_wake(monkeypatch):
    import cogs.voice_controller as vcmod
    clock = {"t": 1000.0}
    monkeypatch.setattr(vcmod.time, "time", lambda: clock["t"])
    vc = _make_vc_for_guards()

    # A 喚醒被接受 → Response Lock 開
    is_fast_a, _ = _call_guards(vc, "A", 100.0, True)
    assert is_fast_a is True
    assert vc._wake_response_pending is True

    # 回應處理完（修復後的解鎖路徑）
    vc._release_response_lock("query_done")
    assert vc._wake_response_pending is False

    # 5 秒後 B 喚醒（避開 2 秒 Global Wake Guard）：鎖已解，不該被 Response Lock 壓
    clock["t"] += 5.0
    is_fast_b, is_echo_b = _call_guards(vc, "B", 105.0, True)
    assert is_fast_b is True
    assert is_echo_b is False
