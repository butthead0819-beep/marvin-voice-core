"""TDD — 重啟／回台後音樂接不回來（2026-09-18 事故）。

事故鏈：18:39 重啟後 AutoRejoin 呼叫 mc._ensure_stream_loop() 想接續
autopilot，但迴圈 1ms 內就「佇列播放完畢」：_stream_loop_topup() 裡
`_rb = (self._current_stream_info or {}).get('requested_by')` 重啟後是
None（沒有上一首）→ `_autorecommend_seed(None, online)` 回 None → 不推薦；
本場歷史空 → `_last_resort_replay` 也失敗 → break。
接著使用者說「馬文繼續播放」→ resume 分支因為沒東西暫停，回「😑 沒有東西
在暫停。」就結束，一片安靜。

F1：_rb 沒有上一首時要 fallback 成 Marvin 推薦 seed，讓 _autorecommend_seed
用在場者續推。
F2：resume 指令在「沒東西暫停 + 迴圈沒在跑」時要叫醒串流迴圈，而不是單純告知
沒東西暫停。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_cog():
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    from cogs.music_cog import MusicCog
    cog = MusicCog(bot)
    return cog


# ── F1: _stream_loop_topup 重啟後空 seed fallback ──────────────────────────

@pytest.mark.asyncio
async def test_topup_falls_back_to_marvin_seed_when_no_previous_song():
    """重啟後 _current_stream_info=None（沒有上一首）+ 在場者非空 → 要以 Marvin 推薦
    seed 觸發 _autorecommend_seed，續推給在場者。"""
    cog = _make_cog()
    cog._personal_shuffle = None
    cog._current_stream_info = None
    cog.stream_queue = []
    cog._vc = MagicMock(return_value=None)
    cog._autopilot_online_members = MagicMock(return_value=["狗與露"])
    cog._auto_recommend = AsyncMock(return_value=None)
    cog._last_resort_replay = AsyncMock(return_value=False)

    await cog._stream_loop_topup()

    cog._auto_recommend.assert_awaited_once()
    assert cog._auto_recommend.await_args.args[0] == "狗與露"


@pytest.mark.asyncio
async def test_topup_no_recommend_when_no_previous_song_and_no_one_online():
    """沒有上一首 + 沒人在場 → 沒有 seed 可用，不該呼叫 _auto_recommend。"""
    cog = _make_cog()
    cog._personal_shuffle = None
    cog._current_stream_info = None
    cog.stream_queue = []
    cog._vc = MagicMock(return_value=None)
    cog._autopilot_online_members = MagicMock(return_value=[])
    cog._auto_recommend = AsyncMock(return_value=None)
    cog._last_resort_replay = AsyncMock(return_value=False)

    await cog._stream_loop_topup()

    cog._auto_recommend.assert_not_awaited()


# ── F2: resume 指令在迴圈死掉時要叫醒它 ────────────────────────────────────

def _mock_channel():
    ch = MagicMock()
    ch.send = AsyncMock(return_value=None)
    return ch


@pytest.mark.asyncio
async def test_resume_wakes_dead_loop_when_nothing_paused():
    """沒東西暫停 + stream_task 是 None（重啟後）+ 可播放 → 該叫醒串流迴圈，
    不能只回『沒有東西在暫停』就結束。"""
    cog = _make_cog()
    cog.stream_paused = False
    cog.radio_paused = False
    cog.radio_mode = False
    cog.stream_task = None

    vc = MagicMock()
    vc._resolve_playback_device.return_value = MagicMock()
    ch = _mock_channel()
    vc.active_text_channel = ch
    vc._mixer = None
    vc.stt_logger = MagicMock()
    cog._vc = MagicMock(return_value=vc)
    cog._ensure_stream_loop = MagicMock(return_value=True)

    await cog._handle_voice_music_command("狗與露", "", "resume")

    cog._ensure_stream_loop.assert_called_once()
    sent_texts = [c.args[0] for c in ch.send.call_args_list]
    assert not any("沒有東西在暫停" in t for t in sent_texts)


@pytest.mark.asyncio
async def test_resume_still_reports_nothing_paused_when_loop_alive():
    """迴圈還活著（stream_task 未 done）→ 維持原行為：回『沒有東西在暫停』，不叫醒迴圈。"""
    cog = _make_cog()
    cog.stream_paused = False
    cog.radio_paused = False
    cog.radio_mode = False
    alive_task = MagicMock()
    alive_task.done.return_value = False
    cog.stream_task = alive_task

    vc = MagicMock()
    vc._resolve_playback_device.return_value = MagicMock()
    ch = _mock_channel()
    vc.active_text_channel = ch
    vc._mixer = None
    vc.stt_logger = MagicMock()
    cog._vc = MagicMock(return_value=vc)
    cog._ensure_stream_loop = MagicMock(return_value=True)

    await cog._handle_voice_music_command("狗與露", "", "resume")

    cog._ensure_stream_loop.assert_not_called()
    sent_texts = [c.args[0] for c in ch.send.call_args_list]
    assert any("沒有東西在暫停" in t for t in sent_texts)
