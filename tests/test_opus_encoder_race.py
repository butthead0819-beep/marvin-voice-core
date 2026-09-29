"""libopus 崩潰（9/23 21:56 SIGBUS、9/24 14:53 silk/resampler.c:193 assertion）回歸測試。

根因：discord.py `VoiceClient.play()` 每次都換一顆新的 `self.encoder`、開新 AudioPlayer
thread；`stop()` 只設旗標不等舊 thread 收尾。barge-in 在 Plan12 mixer 播放中呼叫
device.stop()，~20ms 後重武裝 → 舊 thread 最後一輪 send_audio_packet 讀到的是**新**
encoder，跟新 thread 同時 encode 同一顆 → libopus 狀態被寫壞。
（2 thread 共用一顆 opus.Encoder 的最小重現：3/3 次噴出一字不差的 silk/resampler.c:193
assertion；單 thread 4 萬幀無事。）

兩道修正：
1. Plan12 mixer 下 barge-in / tts_flush 不停 player，只 clear_tts（拔觸發點）。
2. DiscordPlaybackDevice.play/arm_mixer 前，舊 player thread 已停但還活著 → 最多等
   0.1s，還活著就拒絕（結構性保證同時只有一條 thread 碰 encoder）。
"""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

from cogs.voice_controller import VoiceController
from local_mixing_source import ensure_mixer_playing
from marvin_voice_core.playback_device import DiscordPlaybackDevice


# ── 1. 拔觸發點：Plan12 barge-in 不 stop player ───────────────────────────────

def _barge_fake():
    fake = MagicMock()
    fake.bot.cogs.get.return_value = None
    fake.bot.voice_clients = []
    fake.is_playing_audio = True
    fake._tts_protected = False
    fake._plan12 = True
    fake._mixer = MagicMock()
    fake._current_tts_text = "未說完的話"
    fake._current_tts_in_channel = True
    fake.stt_logger = MagicMock()
    fake.last_marvin_speech_time = 0.0
    fake.user_states = {}
    fake.bot.engine.conv_buffer.get_conversation_temperature.return_value = 2.0
    return fake


def test_plan12_barge_in_clears_tts_without_stopping_player():
    fake = _barge_fake()
    device = MagicMock()
    device.is_playing.return_value = True
    fake._resolve_playback_device.return_value = device

    VoiceController.handle_raw_speech_start(fake, "Alice")

    device.stop.assert_not_called()
    fake._mixer.clear_tts.assert_called_once()
    assert fake._tts_interrupted is True


@pytest.mark.asyncio
async def test_plan12_tts_flush_clears_tts_without_stopping_player(monkeypatch):
    from cogs.voice_controller_playback import PlaybackMixin
    vc = MagicMock()
    vc.is_connected.return_value = True
    vc.is_playing.return_value = True
    fake = MagicMock()
    fake.bot.voice_clients = [vc]
    fake._plan12 = True
    fake._mixer = MagicMock()

    async def _no_sleep(_s):
        return None
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    await PlaybackMixin.tts_flush(fake)

    vc.stop.assert_not_called()
    fake._mixer.clear_tts.assert_called_once()


@pytest.mark.asyncio
async def test_legacy_tts_flush_still_stops_player(monkeypatch):
    from cogs.voice_controller_playback import PlaybackMixin
    vc = MagicMock()
    vc.is_connected.return_value = True
    vc.is_playing.return_value = True
    fake = MagicMock()
    fake.bot.voice_clients = [vc]
    fake._plan12 = False
    fake._mixer = None

    async def _no_sleep(_s):
        return None
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    await PlaybackMixin.tts_flush(fake)

    vc.stop.assert_called_once()


# ── 2. 結構性保證：舊 player thread 沒收尾前不准 play ─────────────────────────

class _OldPlayer(threading.Thread):
    """模擬已 stop()、但還在跑最後一輪的 discord AudioPlayer thread。"""

    def __init__(self, linger_s: float):
        super().__init__(daemon=True)
        self._linger = linger_s

    def is_playing(self) -> bool:
        return False  # 已 stop（_end set）

    def run(self):
        time.sleep(self._linger)


def _vc_with_player(player):
    vc = MagicMock()
    vc._player = player
    vc.is_connected.return_value = True
    vc.is_playing.return_value = False
    vc.channel.bitrate = 96000
    alive_at_play = []
    vc.play.side_effect = lambda *a, **k: alive_at_play.append(player.is_alive())
    return vc, alive_at_play


@pytest.mark.parametrize("call", ["arm_mixer", "play"])
def test_waits_for_stopped_player_thread_before_play(call):
    old = _OldPlayer(0.03)
    old.start()
    vc, alive_at_play = _vc_with_player(old)
    dev = DiscordPlaybackDevice(vc)

    getattr(dev, call)(MagicMock())

    assert alive_at_play == [False]  # play 當下舊 thread 已結束


@pytest.mark.parametrize("call", ["arm_mixer", "play"])
def test_refuses_play_while_old_player_thread_still_alive(call):
    old = _OldPlayer(1.0)
    old.start()
    vc, _ = _vc_with_player(old)
    dev = DiscordPlaybackDevice(vc)

    with pytest.raises(RuntimeError):
        getattr(dev, call)(MagicMock())
    vc.play.assert_not_called()


def test_ensure_mixer_playing_returns_false_while_old_player_alive():
    old = _OldPlayer(1.0)
    old.start()
    vc, _ = _vc_with_player(old)
    dev = DiscordPlaybackDevice(vc)

    assert ensure_mixer_playing(dev, lambda: MagicMock()) is False
    vc.play.assert_not_called()


def test_no_player_or_finished_player_plays_immediately():
    vc = MagicMock()
    vc._player = None
    vc.channel.bitrate = 96000
    DiscordPlaybackDevice(vc).arm_mixer(MagicMock())
    vc.play.assert_called_once()

    done = _OldPlayer(0.0)
    done.start()
    done.join()
    vc2 = MagicMock()
    vc2._player = done
    vc2.channel.bitrate = 96000
    DiscordPlaybackDevice(vc2).arm_mixer(MagicMock())
    vc2.play.assert_called_once()
