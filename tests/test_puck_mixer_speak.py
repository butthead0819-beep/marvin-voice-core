"""car puck mk2 DJ 口白傳輸（2026-08-17）：先前 pi_bt 硬體完全沒有任何管道把
DJ 口白送到 Pi——`_maybe_play_dj_interjection` 只推 Mac 自己的 TTS 層 +
esp32_edge_mix 的 speak()，PuckMixerClient 沒有 speak，車上聽 Pi/BT 喇叭完全
聽不到口白、直接跳下一首。補上「Mac 送文字、Pi 自己 Edge TTS 合成+duck 音樂」
這條路（見 project_car_puck_mk2_pi_zero2w_bt_mixer_validated 記憶「DJ口白傳輸
路線定案」）。"""
import threading
import time

import numpy as np
import pytest
from unittest.mock import MagicMock

from device.puck_mixer import (
    CHANNELS, CHUNK_FRAMES, DJ_INTERJECTION_VOLUME, PuckMixer, _mix_tts_ducked,
    _synthesize_tts_pcm,
)


def _stereo_chunk(n_frames: int, value: int) -> np.ndarray:
    return np.full(n_frames * CHANNELS, value, dtype=np.int16)


# ---- _mix_tts_ducked：純函式 ----

def test_mix_tts_ducked_none_leaves_music_unducked():
    mixed = _stereo_chunk(CHUNK_FRAMES, 1000)
    out, pos = _mix_tts_ducked(mixed, None, 0, DJ_INTERJECTION_VOLUME)
    assert np.array_equal(out, mixed)
    assert pos is None


def test_mix_tts_ducked_attenuates_music_and_adds_speech():
    mixed = _stereo_chunk(CHUNK_FRAMES, 1000)
    tts = _stereo_chunk(CHUNK_FRAMES, 2000)
    out, pos = _mix_tts_ducked(mixed, tts, 0, 0.3)
    # 1000*0.3 + 2000 = 2300
    assert np.all(out == 2300)
    assert pos == CHUNK_FRAMES * CHANNELS


def test_mix_tts_ducked_advances_position_when_speech_longer_than_chunk():
    mixed = _stereo_chunk(CHUNK_FRAMES, 0)
    tts = _stereo_chunk(CHUNK_FRAMES * 3, 500)
    out, pos = _mix_tts_ducked(mixed, tts, 0, 0.3)
    assert pos == CHUNK_FRAMES * CHANNELS
    assert np.all(out == 500)


def test_mix_tts_ducked_pads_final_partial_chunk_and_stops_ducking_after():
    mixed = _stereo_chunk(CHUNK_FRAMES, 1000)
    tail_len = 10
    tts = np.full(tail_len, 800, dtype=np.int16)
    out, pos = _mix_tts_ducked(mixed, tts, 0, 0.5)
    assert pos >= len(tts)   # 播完了，呼叫端該清 armed 狀態、下一輪音樂恢復原音量
    assert out[0] == 800 + int(1000 * 0.5)
    assert np.all(out[tail_len:] == int(1000 * 0.5))  # 沒 speech 那段音樂還是被 duck 的那個 chunk


# ---- _synthesize_tts_pcm：edge-tts CLI argv 組法（2026-08-17 實機踩到的迴歸測試）----

def test_synthesize_tts_pcm_uses_equals_form_for_rate_and_pitch(monkeypatch):
    """--rate/--pitch 的值以 "-" 開頭（例如 "-20%"），如果當成獨立 argv 元素傳給
    edge-tts CLI，argparse 會誤判成一個新選項、報 "expected one argument"——
    實機驗證過必須用 --key=value 單一 token 形式。這條鎖住 argv 組法，不能改回
    分開傳。"""
    captured = {}

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return MagicMock(returncode=0)

    monkeypatch.setattr("device.puck_mixer.subprocess.run", _fake_run)
    monkeypatch.setattr("device.puck_mixer._make_decoder", lambda path: MagicMock(
        stdout=MagicMock(read=lambda n: b""), wait=lambda timeout=None: 0))

    _synthesize_tts_pcm("測試")

    cmd = captured["cmd"]
    rate_args = [a for a in cmd if a.startswith("--rate")]
    pitch_args = [a for a in cmd if a.startswith("--pitch")]
    assert len(rate_args) == 1 and "=" in rate_args[0]
    assert len(pitch_args) == 1 and "=" in pitch_args[0]
    # 值本身沒被拆成獨立 argv 元素（不會出現孤立的 "-20%"/"-15Hz" token）
    assert "-20%" not in cmd
    assert "-15Hz" not in cmd


# ---- PuckMixer.speak()：背景合成 + 灌進 TTS deck ----

def test_speak_synthesizes_in_background_and_arms_tts_deck(monkeypatch):
    mixer = PuckMixer(bt_mac="AA:BB:CC:DD:EE:FF")
    pcm = np.full(4000, 999, dtype=np.int16)
    monkeypatch.setattr("device.puck_mixer._synthesize_tts_pcm", lambda text: pcm)

    mixer.speak("測試口白")
    for _ in range(50):
        if mixer._tts_samples is not None:
            break
        time.sleep(0.02)

    assert mixer._tts_samples is not None
    assert np.array_equal(mixer._tts_samples, pcm)
    assert mixer._tts_pos == 0


def test_speak_synthesis_failure_does_not_arm_tts_deck(monkeypatch):
    """edge-tts 掛掉/沒裝套件 → 安靜放棄，不擋音樂繼續播（跟其他降級路徑同一套
    哲學：拿不到就退回舊行為）。"""
    mixer = PuckMixer(bt_mac="AA:BB:CC:DD:EE:FF")

    def _boom(text):
        raise RuntimeError("edge-tts unavailable")

    monkeypatch.setattr("device.puck_mixer._synthesize_tts_pcm", _boom)

    mixer.speak("測試口白")
    time.sleep(0.1)

    assert mixer._tts_samples is None


def test_stop_clears_tts_deck_state():
    mixer = PuckMixer(bt_mac="AA:BB:CC:DD:EE:FF")
    mixer._tts_samples = np.zeros(10, dtype=np.int16)
    mixer._tts_pos = 5
    mixer.stop()
    assert mixer._tts_samples is None
    assert mixer._tts_pos == 0


# ---- _loop() 端到端：TTS deck 真的疊進輸出、duck 了音樂 ----

def test_loop_mixes_tts_ducked_into_output(monkeypatch):
    mixer = PuckMixer(bt_mac="AA:BB:CC:DD:EE:FF")
    monkeypatch.setattr(mixer, "_open_pcm", lambda: MagicMock())

    music = np.full(CHUNK_FRAMES * CHANNELS, 1000, dtype=np.int16)
    monkeypatch.setattr("device.puck_mixer._read_chunk_deck", lambda deck: music)

    mixer._deck_a = {"url": "a", "proc": MagicMock()}
    tts = np.full(2000, 4000, dtype=np.int16)
    mixer._tts_samples = tts
    mixer._tts_pos = 0

    written = []
    real_write = mixer._write_with_reconnect

    def _capture(pcm, data):
        written.append(data)
        return pcm

    monkeypatch.setattr(mixer, "_write_with_reconnect", _capture)

    t = threading.Thread(target=mixer._loop, daemon=True)
    t.start()
    for _ in range(100):
        if written:
            break
        time.sleep(0.02)
    mixer._stop_flag.set()
    t.join(timeout=1.0)

    assert written
    first = np.frombuffer(written[0], dtype=np.int16)
    # 前 2000 個 sample 該是 duck 過的音樂(1000*DJ_INTERJECTION_VOLUME) + speech(4000)
    expected = int(1000 * DJ_INTERJECTION_VOLUME) + 4000
    assert first[0] == expected
