"""TDD：car puck 冷啟動語音狀態提示——見 .claude 規劃「Car puck 冷啟動語音狀態
提示」。BT PCM 一開成功、還沒接上 /audio_stream 前，如果 Mac 端目前沒有真內容
在播（純靜音/idle），本機播一句狀態提示；已經有真內容在播就不要蓋過去。

MARVIN_PUCK_LOCAL_STATUS_AUDIO 空字串＝功能關閉，零行為改變；process 生命週期
內只播一次（_boot_announced flag），BT 中途斷線重連不重播。
"""
import queue
import threading
import time
from unittest.mock import MagicMock

import numpy as np

import device.puck_mixer as puck_mixer
from device.puck_mixer import CHANNELS, CHUNK_FRAMES, PuckMixer


class _AliveThread:
    """假 reader_thread——is_alive() 永遠 True，模擬串流還在收資料。"""

    def is_alive(self):
        return True


def _fake_proc(chunks):
    """假 ffmpeg Popen：stdout.read() 依序吐 chunks，最後回 b"" 代表 EOF。"""
    proc = MagicMock()
    remaining = list(chunks) + [b""]
    proc.stdout.read.side_effect = remaining
    return proc


def test_no_announcement_when_path_unset(monkeypatch):
    monkeypatch.setattr(puck_mixer, "LOCAL_STATUS_AUDIO_PATH", "")
    fetch_mock = MagicMock()
    monkeypatch.setattr(puck_mixer, "fetch_car_now_track", fetch_mock)
    decoder_mock = MagicMock()
    monkeypatch.setattr(puck_mixer, "_make_decoder", decoder_mock)

    mixer = PuckMixer(bt_mac="AA:BB:CC:DD:EE:FF")
    fake_pcm = MagicMock()
    result = mixer._maybe_play_boot_announcement(fake_pcm)

    assert result is fake_pcm
    fetch_mock.assert_not_called()
    decoder_mock.assert_not_called()
    assert mixer._boot_announced is True


def test_plays_clip_once_when_idle(monkeypatch):
    monkeypatch.setattr(puck_mixer, "LOCAL_STATUS_AUDIO_PATH", "/fake/status.mp3")
    monkeypatch.setattr(puck_mixer, "fetch_car_now_track", lambda: None)
    proc = _fake_proc([b"\x00" * 8])
    decoder_mock = MagicMock(return_value=proc)
    monkeypatch.setattr(puck_mixer, "_make_decoder", decoder_mock)

    mixer = PuckMixer(bt_mac="AA:BB:CC:DD:EE:FF")
    fake_pcm = MagicMock()
    result = mixer._maybe_play_boot_announcement(fake_pcm)

    decoder_mock.assert_called_once_with("/fake/status.mp3")
    fake_pcm.write.assert_called_once_with(b"\x00" * 8)
    proc.kill.assert_called_once()
    assert result is fake_pcm


def test_skips_when_real_content_already_playing(monkeypatch):
    monkeypatch.setattr(puck_mixer, "LOCAL_STATUS_AUDIO_PATH", "/fake/status.mp3")
    monkeypatch.setattr(
        puck_mixer, "fetch_car_now_track",
        lambda: {"title": "已經在播的歌", "artist": "", "album": ""},
    )
    decoder_mock = MagicMock()
    monkeypatch.setattr(puck_mixer, "_make_decoder", decoder_mock)

    mixer = PuckMixer(bt_mac="AA:BB:CC:DD:EE:FF")
    fake_pcm = MagicMock()
    result = mixer._maybe_play_boot_announcement(fake_pcm)

    decoder_mock.assert_not_called()
    fake_pcm.write.assert_not_called()
    assert result is fake_pcm
    assert mixer._boot_announced is True


def test_clip_playback_stops_immediately_when_stop_flag_set(monkeypatch):
    """假裝解碼器一直吐得出資料（永遠沒 EOF）——不靠 EOF 結束，得靠 stop_flag。"""
    monkeypatch.setattr(puck_mixer, "LOCAL_STATUS_AUDIO_PATH", "/fake/status.mp3")
    proc = MagicMock()
    proc.stdout.read.return_value = b"\x00" * 8  # 永遠有資料，永遠不 EOF
    monkeypatch.setattr(puck_mixer, "_make_decoder", MagicMock(return_value=proc))

    mixer = PuckMixer(bt_mac="AA:BB:CC:DD:EE:FF")
    mixer._stop_flag.set()  # 播放前就喊停
    fake_pcm = MagicMock()

    result = mixer._play_local_status_clip(fake_pcm)

    assert result is fake_pcm
    fake_pcm.write.assert_not_called()
    proc.kill.assert_called_once()


def test_loop_runs_boot_announcement_once_before_streaming(monkeypatch):
    mixer = PuckMixer(bt_mac="AA:BB:CC:DD:EE:FF")
    monkeypatch.setattr(mixer, "_open_pcm_with_retry", lambda: MagicMock())
    calls = []
    monkeypatch.setattr(
        mixer, "_maybe_play_boot_announcement",
        lambda pcm: (calls.append(1), pcm)[1],
    )
    q = queue.Queue()
    q.put(np.zeros(CHUNK_FRAMES * CHANNELS, dtype=np.int16))
    monkeypatch.setattr(mixer, "_connect_stream", lambda: (q, _AliveThread(), threading.Event()))

    t = threading.Thread(target=mixer._loop, daemon=True)
    t.start()
    time.sleep(0.05)
    mixer._stop_flag.set()
    t.join(timeout=1.0)

    assert not t.is_alive()
    assert calls == [1]


def test_loop_skips_boot_announcement_when_already_announced(monkeypatch):
    """_boot_announced 已經是 True（process 之前已經播過）→ 不該重播，比照
    BT 中途斷線重連場景。"""
    mixer = PuckMixer(bt_mac="AA:BB:CC:DD:EE:FF")
    mixer._boot_announced = True
    monkeypatch.setattr(mixer, "_open_pcm_with_retry", lambda: MagicMock())
    calls = []
    monkeypatch.setattr(
        mixer, "_maybe_play_boot_announcement",
        lambda pcm: (calls.append(1), pcm)[1],
    )
    q = queue.Queue()
    q.put(np.zeros(CHUNK_FRAMES * CHANNELS, dtype=np.int16))
    monkeypatch.setattr(mixer, "_connect_stream", lambda: (q, _AliveThread(), threading.Event()))

    t = threading.Thread(target=mixer._loop, daemon=True)
    t.start()
    time.sleep(0.05)
    mixer._stop_flag.set()
    t.join(timeout=1.0)

    assert not t.is_alive()
    assert calls == []
