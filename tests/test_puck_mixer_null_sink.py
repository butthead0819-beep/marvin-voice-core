"""TDD：device/puck_mixer.py MARVIN_PUCK_NULL_SINK=1 測試用假輸出。

2026-08-20 背景：在家測試 car puck mk2 時，BMW 車機當然連不到，備援喇叭又
可能沒開機在線——_open_pcm() 卡在跟 bluealsa 談 A2DP transport 永遠失敗，
_loop() 連 PCM 都開不成，量不到解碼端/網路端真正的靜音持續時間。加一個
測試用開關：完全跳過 alsaaudio/bluealsa，用吃光所有 write() 的假 PCM，讓
_loop() 照常跑起來。"""
import time

import pytest

import device.puck_mixer as puck_mixer
from device.puck_mixer import CHANNELS, RATE, PuckMixer, _NullPCM


def test_open_pcm_returns_null_pcm_when_null_sink_enabled(monkeypatch):
    monkeypatch.setattr(puck_mixer, "NULL_SINK", True)
    monkeypatch.setattr(puck_mixer, "alsaaudio", None)  # 沒裝到也該完全不碰
    mixer = PuckMixer(bt_mac="AA:BB:CC:DD:EE:FF")
    pcm = mixer._open_pcm()
    # 用 puck_mixer._NullPCM（模組屬性即時查）而不是頂層 import 進來的 _NullPCM
    # ——test_puck_mixer_mac_relay.py 的某條測試會 importlib.reload(puck_mixer)，
    # 若跟這條在同一個 pytest session 混跑，頂層 import 抓到的會是 reload 前的
    # 舊 class 物件，isinstance 對 reload 後產生的新實例會誤判為 False。
    assert isinstance(pcm, puck_mixer._NullPCM)


def test_open_pcm_sets_current_mac_when_null_sink_enabled(monkeypatch):
    """2026-08-20 實機踩到：NULL_SINK 分支忘了設 self._current_mac，
    _maybe_switch_bt_target() 每輪迴圈都看到 target != None（初始值），
    誤判成「目標換了」，每個 chunk 都重開一次 PCM——量到的靜音全是這個
    假象造成的重連，不是真實網路/解碼落差。"""
    monkeypatch.setattr(puck_mixer, "NULL_SINK", True)
    mixer = PuckMixer(bt_mac="AA:BB:CC:DD:EE:FF")
    assert mixer._current_mac is None
    mixer._open_pcm()
    assert mixer._current_mac == "AA:BB:CC:DD:EE:FF"


def test_null_pcm_write_paces_to_real_playback_speed():
    """假 PCM 的 write() 該花跟真的 alsaaudio PCM 差不多的時間（chunk 秒數），
    不然 _loop() 會用 CPU 能跑多快就跑多快榨乾 prefetch buffer，量到的靜音
    是自己抽太快的假象，不是真實播放節奏下的網路/解碼落差。"""
    pcm = _NullPCM()
    n_frames = 1024
    data = b"\x00" * (n_frames * CHANNELS * 2)
    expected = n_frames / RATE
    start = time.monotonic()
    pcm.write(data)
    elapsed = time.monotonic() - start
    # 寬容一點的誤差——這裡測的是「有沒有真的照 chunk 秒數 sleep」不是精確計時，
    # 系統忙碌時（例如跟整批測試一起跑）time.sleep() 本身就有數十 ms 級的抖動。
    assert elapsed == pytest.approx(expected, abs=0.05)
