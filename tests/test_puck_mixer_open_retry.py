"""car puck mk2：_loop() 開場的 _open_pcm() 若失敗（目標 BT 裝置目前沒連線／
`No such device`）不該讓整條播放 thread 直接掛掉、status() 卻繼續謊報在播放。
2026-08-17 實機踩到：Pi 藍牙目前接的是 Soundcore（家用對照測試），但
MARVIN_PUCK_BT_MAC 指向 BMW，_loop() 一開頭 `pcm = self._open_pcm()` 沒有
任何保護，直接丟 alsaaudio.ALSAAudioError、thread 死掉，`/puck/status` 卻一路
回報 playing=<url>，使用者完全看不出沒聲音的原因。"""
import threading
import time

import numpy as np
from unittest.mock import MagicMock

from device.puck_mixer import CHANNELS, CHUNK_FRAMES, PuckMixer


def test_loop_retries_open_pcm_until_it_succeeds(monkeypatch):
    mixer = PuckMixer(bt_mac="AA:BB:CC:DD:EE:FF")
    monkeypatch.setattr("time.sleep", lambda s: None)  # 不要真的等 backoff
    zeros = np.zeros(CHUNK_FRAMES * CHANNELS, dtype=np.int16)
    monkeypatch.setattr("device.puck_mixer._read_chunk_deck", lambda deck: zeros)

    attempts = []
    fake_pcm = MagicMock()

    def _open():
        attempts.append(1)
        if len(attempts) < 3:
            raise Exception("No such device")
        return fake_pcm

    monkeypatch.setattr(mixer, "_open_pcm", _open)
    mixer._deck_a = {"url": "a", "proc": MagicMock()}

    t = threading.Thread(target=mixer._loop, daemon=True)
    t.start()
    for _ in range(100):
        if fake_pcm.write.called:
            break
        time.sleep(0.02)
    mixer._stop_flag.set()
    t.join(timeout=1.0)

    assert not t.is_alive()
    assert len(attempts) >= 3   # 真的重試到成功，沒有第一次失敗就死掉
    assert fake_pcm.write.called


def test_loop_exits_cleanly_when_open_pcm_never_succeeds_before_stop(monkeypatch):
    """開不了（裝置永遠沒連線）且被 stop() 喊停：thread 該乾淨結束，不該炸例外、
    也不該永遠卡住。"""
    mixer = PuckMixer(bt_mac="AA:BB:CC:DD:EE:FF")
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setattr(mixer, "_open_pcm", MagicMock(side_effect=Exception("No such device")))
    mixer._deck_a = {"url": "a", "proc": MagicMock()}

    t = threading.Thread(target=mixer._loop, daemon=True)
    t.start()
    time.sleep(0.05)
    mixer._stop_flag.set()
    t.join(timeout=1.0)

    assert not t.is_alive()   # 沒有變成殭屍 thread、也沒有讓例外冒出來悶死整個 process
