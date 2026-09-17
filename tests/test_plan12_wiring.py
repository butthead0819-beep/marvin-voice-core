"""Plan 12 god-class 接線（T3 sub-2a）— flag + mixer 實例化 + ensure-playing。

flag=off：mixer None、_ensure_mixer_playing no-op（舊路徑零改變）。
flag=on：cog 持 LocalMixingAudioSource、ensure 在 idle vc 上 play 一個 MixerPlaybackAdapter。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from local_mixing_source import LocalMixingAudioSource, MixerPlaybackAdapter
from marvin_voice_core.playback_device import DiscordPlaybackDevice


def _make_cog(plan12: bool, monkeypatch):
    if plan12:
        monkeypatch.setenv("PLAN12_LOCAL_MIX", "true")
    else:
        monkeypatch.delenv("PLAN12_LOCAL_MIX", raising=False)
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.tts_engine = MagicMock()
    bot.tts_engine.get_estimated_duration.return_value = 2.0
    with patch("discord_voice_engine.faster_whisper", None, create=True):
        from discord_voice_engine import DiscordVoiceEngine
        bot.engine = DiscordVoiceEngine(bot)
    with patch("discord.ext.tasks.loop", lambda *a, **kw: lambda f: f), \
         patch("cogs.voice_controller.DepartureStats", MagicMock), \
         patch("cogs.voice_controller.ConsentManager", MagicMock):
        from cogs.voice_controller import VoiceController
        return VoiceController(bot)


def _idle_vc():
    vc = MagicMock()
    vc.is_connected.return_value = True
    vc.is_playing.return_value = False
    return vc





def test_flag_on_builds_mixer_and_ensure_plays(monkeypatch):
    cog = _make_cog(plan12=True, monkeypatch=monkeypatch)
    assert cog._plan12 is True
    assert isinstance(cog._mixer, LocalMixingAudioSource)
    vc = _idle_vc()
    device = DiscordPlaybackDevice(vc)
    assert cog._ensure_mixer_playing(device) is True
    assert vc.play.call_count == 1
    assert isinstance(vc.play.call_args.args[0], MixerPlaybackAdapter)


def test_flag_on_ensure_idempotent_when_playing(monkeypatch):
    cog = _make_cog(plan12=True, monkeypatch=monkeypatch)
    vc = _idle_vc()
    vc.is_playing.return_value = True
    device = DiscordPlaybackDevice(vc)
    assert cog._ensure_mixer_playing(device) is False
    assert not vc.play.called


# ── 自癒重武裝（2026-09-16 incident：AudioPlayer thread 在連線短暫抖動時靜默死掉，
# 沒人通知，只能等下一個剛好路過的呼叫點撿到，最壞情況全靜音到 60 秒 sentinel 週期）──


def test_ensure_mixer_playing_wires_a_non_none_after_callback(monkeypatch):
    """arm 時要帶一個 after callback 進 vc.play，不是 None——沒有就代表播放器死掉沒人知道。"""
    cog = _make_cog(plan12=True, monkeypatch=monkeypatch)
    vc = _idle_vc()
    device = DiscordPlaybackDevice(vc)
    cog._ensure_mixer_playing(device)
    after_cb = vc.play.call_args.kwargs["after"]
    assert after_cb is not None
    assert callable(after_cb)


def test_after_callback_reschedules_ensure_mixer_playing_on_event_loop(monkeypatch):
    """AudioPlayer thread 死掉時 after(None) 被呼叫 → 必須 call_soon_threadsafe 排回
    event loop 重試 _ensure_mixer_playing（callback 來自非 event-loop 的播放器 thread，
    不能直接碰 self 狀態）。

    2026-09-17 修正：原版用全新（＝閒置）mixer 斷言「一定重排」，等於把 4021 事故的
    bug 寫進測試——閒置回 b"" 是正常停送，重武裝會變無窮迴圈（見
    tests/test_mixer_rearm_storm.py）。這裡改成 mixer 仍有內容（真故障），測試原本要守的
    「必須走 call_soon_threadsafe 不可直接碰 self」意圖不變。"""
    cog = _make_cog(plan12=True, monkeypatch=monkeypatch)
    cog.bot.loop.is_closed.return_value = False
    vc = _idle_vc()
    device = DiscordPlaybackDevice(vc)
    cog._ensure_mixer_playing(device)
    after_cb = vc.play.call_args.kwargs["after"]

    with patch.object(cog._mixer, "is_idle", return_value=False):
        after_cb(None)

    cog.bot.loop.call_soon_threadsafe.assert_called_once_with(cog._ensure_mixer_playing, device)


def test_after_callback_noop_when_loop_closed(monkeypatch):
    """bot 正在關閉（loop 已關）時 after 不該再排任何東西進去，避免對已死 loop 操作炸例外。

    mixer 設成非閒置，才會真的走到 loop 檢查那一步（閒置會更早短路，測不到這個守衛）。"""
    cog = _make_cog(plan12=True, monkeypatch=monkeypatch)
    cog.bot.loop.is_closed.return_value = True
    vc = _idle_vc()
    device = DiscordPlaybackDevice(vc)
    cog._ensure_mixer_playing(device)
    after_cb = vc.play.call_args.kwargs["after"]

    with patch.object(cog._mixer, "is_idle", return_value=False):
        after_cb(None)

    assert not cog.bot.loop.call_soon_threadsafe.called


def test_after_callback_does_not_rearm_when_mixer_idle(monkeypatch):
    """☢️ 2026-09-17 4021 事故守門：閒置停送不是故障，不可重武裝（詳見
    tests/test_mixer_rearm_storm.py）。"""
    cog = _make_cog(plan12=True, monkeypatch=monkeypatch)
    cog.bot.loop.is_closed.return_value = False
    vc = _idle_vc()
    device = DiscordPlaybackDevice(vc)
    cog._ensure_mixer_playing(device)
    after_cb = vc.play.call_args.kwargs["after"]

    assert cog._mixer.is_idle() is True
    after_cb(None)

    assert not cog.bot.loop.call_soon_threadsafe.called


def test_after_callback_logs_warning_when_error_present(monkeypatch, caplog):
    """AudioPlayer 因例外（非單純連線抖動）掛掉時，error 不是 None——要留紀錄，別悄悄吞掉。"""
    import logging
    cog = _make_cog(plan12=True, monkeypatch=monkeypatch)
    cog.bot.loop.is_closed.return_value = False
    vc = _idle_vc()
    device = DiscordPlaybackDevice(vc)
    cog._ensure_mixer_playing(device)
    after_cb = vc.play.call_args.kwargs["after"]

    with caplog.at_level(logging.WARNING):
        after_cb(RuntimeError("send_audio_packet boom"))

    assert "send_audio_packet boom" in caplog.text


# ── T4：狀態欄位委派 mixer ─────────────────────────────────────────────────────

def test_flag_on_state_fields_delegate_to_mixer(monkeypatch):
    import numpy as np
    cog = _make_cog(plan12=True, monkeypatch=monkeypatch)
    assert cog.is_playing_audio is False          # mixer idle
    assert cog.tts_queue_duration == 0.0
    cog._mixer.push_tts(np.zeros(48000 * 2, dtype=np.float32))  # 1s
    assert cog.is_playing_audio is True            # 20+ reader 自然看到
    assert cog.tts_queue_duration == pytest.approx(1.0, abs=0.01)





# ── 2c：play_tts flag=on → render + push mixer ────────────────────────────────

@pytest.mark.asyncio
async def test_play_tts_flag_on_pushes_to_mixer(monkeypatch):
    import numpy as np
    cog = _make_cog(plan12=True, monkeypatch=monkeypatch)
    cog.game_mode = False
    cog._tts_protected = True   # 繞過 silence gate
    cog.stream_mode = False
    cog._tts_interrupted = False
    vc = _idle_vc()
    cog.bot.voice_clients = [vc]

    # streaming render：mock 成「逐幀 push 進 mixer」（真的接 edge-tts+ffmpeg 不適合單測）
    async def _fake_stream(text, **kw):
        cog._mixer.push_tts(np.full(960 * 2, 0.3, dtype=np.float32))
        return 1
    cog._stream_tts_to_mixer = _fake_stream
    await cog.play_tts("哈囉馬文")
    assert not cog._mixer.is_idle()      # TTS 已 push 進 mixer
    assert vc.play.called                # ensure_mixer_playing 啟動 adapter
