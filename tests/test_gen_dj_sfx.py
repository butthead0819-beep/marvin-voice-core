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
    assert 0.4 <= dur <= 1.2
    assert np.max(np.abs(scratch)) <= 1.0
    # 驗證動態調變與黑膠摩擦
    rms = np.sqrt(np.mean(scratch ** 2))
    assert 0.05 <= rms <= 0.8


def test_gen_scratch_from_pcm_normalizes_quiet_input():
    """真實歌曲 PCM（loudnorm 後 RMS 常只有 ~0.1-0.15）遠比內建 formant 素材
    （tanh 飽和後 RMS≈0.3）安靜，而摩擦/rumble 噪點層的增益是照後者調的——沒有
    先歸一化，安靜輸入的旋律會被噪點層蓋過，聽起來像跟歌曲完全無關的電子噪音
    （2026-08-09 使用者實測回報）。鎖住：同一段旋律不論原始音量多小聲，輸出響度
    要落在穩定區間內，不能因為輸入安靜就被噪點主導、輸出音量遠低於正常水準。"""
    from scripts.gen_dj_sfx import gen_scratch_from_pcm
    rate = 48000
    t = np.arange(int(rate * 2)) / rate
    quiet_music = (0.05 * np.sin(2 * np.pi * 220 * t) + 0.03 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    loud_music = quiet_music * 6.0  # 同一段旋律，音量差 6 倍

    out_quiet = gen_scratch_from_pcm(quiet_music, rate=rate, style="wicka")
    out_loud = gen_scratch_from_pcm(loud_music, rate=rate, style="wicka")

    rms_quiet = np.sqrt(np.mean(out_quiet ** 2))
    rms_loud = np.sqrt(np.mean(out_loud ** 2))
    assert rms_quiet > 0.15  # 安靜輸入不能被正規化前的做法拖到幾乎聽不見旋律
    assert rms_loud / rms_quiet < 1.5  # 歸一化後響度差距應被拉近，不能維持原始 6 倍懸殊


def test_all_scratch_styles_supported():
    """驗證所有支援的刷盤手法（wicka, spinback, vinyl_brake, chirp, transform, tear）均能正常生成。"""
    from scripts.gen_dj_sfx import gen_scratch_from_pcm, SCRATCH_STYLES
    rate = 48000
    t = np.arange(rate * 3) / rate
    test_pcm = 0.4 * np.sin(2 * np.pi * 330 * t) + 0.3 * np.sin(2 * np.pi * 660 * t)

    expected_styles = {"wicka", "spinback", "vinyl_brake", "chirp", "transform", "tear"}
    assert set(SCRATCH_STYLES) == expected_styles

    for style in SCRATCH_STYLES:
        out = gen_scratch_from_pcm(test_pcm, rate=rate, style=style)
        assert isinstance(out, np.ndarray)
        assert out.dtype == np.float32
        dur = len(out) / rate
        assert 0.4 <= dur <= 1.5, f"Style {style} duration {dur:.2f}s out of bounds"
        assert not np.isnan(out).any(), f"Style {style} contains NaN"
        assert not np.isinf(out).any(), f"Style {style} contains Inf"
        assert np.max(np.abs(out)) <= 1.0
        rms = np.sqrt(np.mean(out ** 2))
        assert rms > 0.03, f"Style {style} too quiet (rms={rms})"


def test_find_cue_point_detects_transient_onset():
    """驗證 find_cue_point 能在開頭有靜音/微弱環境音時，精準鎖定前奏的第一個重音攻擊點 (Attack)。"""
    from scripts.gen_dj_sfx import find_cue_point
    rate = 48000
    # 前 0.45 秒為純靜音，0.45s 處出現強烈人聲/樂器攻擊音
    test_pcm = np.zeros(rate * 3, dtype=np.float32)
    n_sound = int(rate * 1.5)
    t_sound = np.arange(n_sound) / rate
    test_pcm[int(rate * 0.45) : int(rate * 0.45) + n_sound] = (
        0.7 * np.sin(2 * np.pi * 440 * t_sound) * np.exp(-1.5 * t_sound)
    )

    cue = find_cue_point(test_pcm, rate=rate)
    assert 0.40 <= cue <= 0.48, f"Cue point {cue:.3f}s should be around 0.45s"

    # 若整段皆為極小雜音/靜音，應優雅 fallback 到預設 ~0.2s 而非報錯
    silent_pcm = np.random.normal(0, 1e-6, rate * 2).astype(np.float32)
    fallback_cue = find_cue_point(silent_pcm, rate=rate)
    assert 0.1 <= fallback_cue <= 0.5


def test_gen_scratch_bpm_continuous_routine():
    """驗證傳入 BPM 時，生成的是一段完整連貫的 Turntablism Routine，長度在 1.0~1.8 秒之間，且無破音。"""
    from scripts.gen_dj_sfx import gen_scratch_from_pcm
    rate = 48000
    t = np.arange(rate * 4) / rate
    # 模擬包含前奏的歌曲
    music = (0.5 * np.sin(2 * np.pi * 261.6 * t) + 0.3 * np.sin(2 * np.pi * 523.2 * t)).astype(np.float32)

    for test_bpm in (85.0, 110.0, 128.0, 150.0):
        out = gen_scratch_from_pcm(music, rate=rate, bpm=test_bpm)
        assert isinstance(out, np.ndarray)
        assert out.dtype == np.float32
        dur = len(out) / rate
        assert 0.8 <= dur <= 2.0, f"BPM {test_bpm} duration {dur:.2f}s out of DJ tail window"
        assert not np.isnan(out).any()
        assert not np.isinf(out).any()
        assert np.max(np.abs(out)) <= 1.0
        rms = np.sqrt(np.mean(out ** 2))
        assert 0.08 <= rms <= 0.7, f"BPM {test_bpm} rms={rms:.3f} out of reasonable range"



