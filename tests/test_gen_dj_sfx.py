# tests/test_gen_dj_sfx.py
"""測試 DJ Tail 轉場音效生成與黑膠轉盤音效（scratch）品質。"""
import os
import wave
import numpy as np
import pytest

from scripts.gen_dj_sfx import gen_scratch, gen_dj_airhorn, gen_riser, SOUNDS, SFX_DIR, RATE


def test_gen_scratch_shape_and_duration():
    """scratch 音效長度應在 0.4s ~ 1.0s 之間，採樣率為 float32。"""
    samples = gen_scratch()
    assert isinstance(samples, np.ndarray)
    assert samples.dtype == np.float32
    dur = len(samples) / RATE
    assert 0.4 <= dur <= 1.0
    assert np.max(np.abs(samples)) <= 1.0
    # RMS 能量適中，不應為靜音或全幅破音
    rms = np.sqrt(np.mean(samples ** 2))
    assert 0.1 <= rms <= 0.6


def test_gen_scratch_dynamics_and_harmonics():
    """scratch 應具備真實手刷加減速動態（換向點能量有衰減）與多頻段共振諧波，而非純電子噪音。"""
    samples = gen_scratch()
    # 檢查手刷換向點具有動態包絡（最小值與最大值能量比）
    # 切分成 10 個區間，動態應該有顯著高低起伏
    chunk_size = len(samples) // 10
    rms_chunks = [np.sqrt(np.mean(samples[i * chunk_size:(i + 1) * chunk_size] ** 2)) for i in range(10)]
    max_chunk_rms = max(rms_chunks)
    min_chunk_rms = min(rms_chunks)
    # 動態範圍至少有起伏
    assert min_chunk_rms < max_chunk_rms * 0.5

    # 檢查低頻轉盤箱體 Rumble / 溝槽能量 (30~150Hz) 與中頻共振 (500~3000Hz)
    fft = np.abs(np.fft.rfft(samples))
    freqs = np.fft.rfftfreq(len(samples), 1 / RATE)
    low_rumble = np.sum(fft[(freqs >= 30) & (freqs <= 150)])
    mid_energy = np.sum(fft[(freqs >= 500) & (freqs <= 3000)])
    total_energy = np.sum(fft)
    assert mid_energy / total_energy > 0.35
    assert low_rumble / total_energy > 0.004  # 黑膠低頻轉盤共振感（新版 ~0.006，舊版正弦波 <0.0005）




def test_gen_scratch_wav_file_output(tmp_path):
    """測試寫入 wav 檔案後可正常讀取且格式正確。"""
    wav_path = str(tmp_path / "scratch_test.wav")
    samples = gen_scratch()
    pcm16 = np.clip(samples, -1.0, 1.0)
    pcm16 = (pcm16 * 32767).astype(np.int16)
    with wave.open(wav_path, "w") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(RATE)
        f.writeframes(pcm16.tobytes())

    assert os.path.exists(wav_path)
    with wave.open(wav_path, "r") as f:
        assert f.getnchannels() == 1
        assert f.getsampwidth() == 2
        assert f.getframerate() == RATE
        assert f.getnframes() == len(samples)


def test_gen_scratch_from_pcm_music_input():
    """傳入真實音樂 PCM（如立體聲 48kHz）時，應能成功產生帶有音樂特徵的動態刷碟音訊。"""
    from scripts.gen_dj_sfx import gen_scratch_from_pcm
    rate = 48000
    t = np.arange(rate * 2) / rate  # 2 秒音訊
    # 模擬音樂：440Hz 主音 + 880Hz 泛音
    music_ch1 = 0.5 * np.sin(2 * np.pi * 440 * t)
    music_ch2 = 0.4 * np.sin(2 * np.pi * 880 * t)
    stereo_pcm = np.stack([music_ch1, music_ch2], axis=-1)

    scratch = gen_scratch_from_pcm(stereo_pcm, rate=rate)
    assert isinstance(scratch, np.ndarray)
    assert scratch.dtype == np.float32
    dur = len(scratch) / rate
    assert 0.4 <= dur <= 1.0
    assert np.max(np.abs(scratch)) <= 1.0
    # 驗證動態調變與黑膠摩擦
    rms = np.sqrt(np.mean(scratch ** 2))
    assert 0.05 <= rms <= 0.8

