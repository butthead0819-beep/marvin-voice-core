#!/usr/bin/env python3
"""
合成 DJ Tail 轉場音效（scratch / air horn / riser），純 numpy 產生、無外部素材授權疑慮。
每支 <2s，供 _run_tail_dj 在 DJ 口白播完後疊入下一首開頭。
Run from repo root: python scripts/gen_dj_sfx.py
"""
import os
import wave

import numpy as np
from scipy.signal import butter, sosfilt

RATE = 44100
SFX_DIR = "assets/dj_sfx"


def _write_wav(path: str, samples: np.ndarray) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    pcm16 = np.clip(samples, -1.0, 1.0)
    pcm16 = (pcm16 * 32767).astype(np.int16)
    with wave.open(path, "w") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(RATE)
        f.writeframes(pcm16.tobytes())


def _fade(samples: np.ndarray, attack: float = 0.01, release: float = 0.08) -> np.ndarray:
    n = len(samples)
    att = int(RATE * attack)
    rel = int(RATE * release)
    out = samples.copy()
    if att > 0:
        out[:att] *= np.linspace(0, 1, att)
    if rel > 0:
        out[n - rel:] *= np.linspace(1, 0, rel)
    return out


def gen_scratch() -> np.ndarray:
    """刷碟：真實 DJ 黑膠轉盤刷碟（Vinyl Scratch）。

    捨棄單純正弦波掃頻（聽起來像 8-bit 電子噪音），改用真實黑膠溝槽調變模型：
    1. 採用手腕加減速非對稱軌跡（Wicka-wicka 手法：Pull -> Push -> Short Pull -> Slide Cut）。
    2. 驅動黑膠溝槽 Formant（A/O 共振峰）切片重採樣（Doppler / Speed modulation）。
    3. 疊加唱針微觀摩擦顆粒（Needle Friction）、唱盤低頻共振（Turntable Rumble）與唱針微爆音（Crackle）。
    4. 換向停滯點動態歸零，經箱體濾波與類比暖度飽和，呈現清脆、扎實且自然的黑膠刷碟聲。
    """
    dur = 0.65
    n = int(RATE * dur)
    t = np.linspace(0, dur, n)

    # 4 段手部動作速度曲線 v(t) (相對正常播放速度的倍率)
    # 0.00 ~ 0.13: 向後拉回 (pull, v從 0 -> -2.6 -> 0)
    # 0.13 ~ 0.26: 向前快推 (push, v從 0 -> +3.2 -> 0)
    # 0.26 ~ 0.38: 短促拉回 (short pull, v從 0 -> -3.5 -> 0)
    # 0.38 ~ 0.65: 前推切入並順勢滑出 (release slide, v從 0 -> +4.0 -> 0)
    cuts = [0.0, 0.13, 0.26, 0.38, dur]
    v = np.zeros(n)

    s1 = (t >= cuts[0]) & (t < cuts[1])
    t_s1 = (t[s1] - cuts[0]) / (cuts[1] - cuts[0])
    v[s1] = -2.6 * np.sin(np.pi * t_s1) ** 1.3

    s2 = (t >= cuts[1]) & (t < cuts[2])
    t_s2 = (t[s2] - cuts[1]) / (cuts[2] - cuts[1])
    v[s2] = 3.2 * np.sin(np.pi * t_s2) ** 1.3

    s3 = (t >= cuts[2]) & (t < cuts[3])
    t_s3 = (t[s3] - cuts[2]) / (cuts[3] - cuts[2])
    v[s3] = -3.5 * np.sin(np.pi * t_s3) ** 1.4

    s4 = (t >= cuts[3]) & (t <= cuts[4])
    t_s4 = (t[s4] - cuts[3]) / (cuts[4] - cuts[3])
    v[s4] = 4.0 * np.sin(np.pi * t_s4 * 0.75) * np.exp(-3.2 * t_s4)

    # 1. 唱片音源層：富含 Formant 共振的人聲/樂器黑膠切片
    sample_dur = 4.0
    st = np.arange(int(RATE * sample_dur)) / RATE
    f0 = 170.0
    saw = 2 * (st * f0 - np.floor(0.5 + st * f0))
    pulse = np.where((st * f0 % 1.0) < 0.32, 1.0, -1.0)
    source_wave = 0.55 * saw + 0.45 * pulse

    # 經典嘻哈 "Ahhh/Fresh" 3 階共振峰濾波
    sos_f1 = butter(2, [580, 820], btype="bandpass", fs=RATE, output="sos")
    sos_f2 = butter(2, [1150, 1550], btype="bandpass", fs=RATE, output="sos")
    sos_f3 = butter(2, [2350, 3100], btype="bandpass", fs=RATE, output="sos")
    f1 = sosfilt(sos_f1, source_wave) * 1.7
    f2 = sosfilt(sos_f2, source_wave) * 1.3
    f3 = sosfilt(sos_f3, source_wave) * 0.9
    vinyl_content = np.tanh((f1 + f2 + f3) * 1.7)

    # 根據手速軌跡重採樣 (Doppler / Scratch modulation)
    pos = 1.0 + np.cumsum(v) / RATE
    pos_idx = np.clip(pos * RATE, 0, len(vinyl_content) - 2)
    idx_f = pos_idx.astype(int)
    idx_frac = pos_idx - idx_f
    scratched_tone = (1.0 - idx_frac) * vinyl_content[idx_f] + idx_frac * vinyl_content[idx_f + 1]

    # 2. 手速動態包絡 (速度越快聲音越響亮，換向停滯點無聲)
    speed = np.abs(v)
    vol_env = np.clip(speed / 1.6, 0.0, 1.0) ** 0.8

    # 3. 唱針微觀摩擦層 (Needle Friction & Texture)
    rng = np.random.default_rng(2026)
    noise = rng.normal(0, 1, n)
    sos_fric = butter(2, [800, 5200], btype="bandpass", fs=RATE, output="sos")
    friction = sosfilt(sos_fric, noise) * 0.45 * (speed ** 1.15)

    # 4. 黑膠唱盤箱體低頻 (Turntable Platter Rumble)
    sos_rumble = butter(2, [45, 110], btype="bandpass", fs=RATE, output="sos")
    rumble = sosfilt(sos_rumble, rng.normal(0, 1, n)) * 0.3 * (speed ** 0.5)

    # 5. 黑膠唱針微小碎音 (Needle crackle / micro-pops)
    crackle = np.zeros(n)
    pop_indices = rng.choice(n, size=int(n * 0.008), replace=False)
    crackle[pop_indices] = rng.uniform(0.25, 0.7, size=len(pop_indices)) * speed[pop_indices]

    # 混音組合
    mix = (scratched_tone * 0.70 + friction * 0.42 + rumble * 0.28 + crackle * 0.15) * vol_env

    # 溫暖度濾波（抑制過度刺耳的高頻，留下扎實的中頻刷碟質感）
    sos_body = butter(2, 6000, btype="lowpass", fs=RATE, output="sos")
    mix = sosfilt(sos_body, mix)

    # 軟飽和 (Analog Warmth)
    out = np.tanh(mix * 1.55)

    return _fade(out.astype(np.float32), attack=0.008, release=0.04)



def gen_dj_airhorn() -> np.ndarray:
    """雷鬼 DJ 空氣號角「meep-meep」兩響，比 assets/sfx/air_horn.wav 更短促、堆疊泛音更厚。"""
    def hit(dur: float) -> np.ndarray:
        n = int(RATE * dur)
        t = np.arange(n) / RATE
        # 基頻 + 完全五度堆疊，方波製造喇叭感
        sig = 0.5 * np.sign(np.sin(2 * np.pi * 370 * t))
        sig += 0.35 * np.sign(np.sin(2 * np.pi * 554 * t))
        sig += 0.15 * np.sign(np.sin(2 * np.pi * 740 * t))
        return _fade(sig, attack=0.01, release=0.05)

    gap = np.zeros(int(RATE * 0.08))
    return np.concatenate([hit(0.32), gap, hit(0.32)])


def gen_riser() -> np.ndarray:
    """上升 riser／whoosh：噪點淡入疊加快速上滑音，轉場前墊一下氣氛。"""
    dur = 1.1
    n = int(RATE * dur)
    t = np.arange(n) / RATE
    sweep_freq = 200 + (2200 - 200) * (t / dur) ** 2
    phase = 2 * np.pi * np.cumsum(sweep_freq) / RATE
    tone = np.sin(phase)
    noise = np.random.default_rng(1).uniform(-1, 1, n)
    env = (t / dur) ** 1.5
    sig = (0.5 * tone + 0.5 * noise) * env
    return _fade(sig, attack=0.02, release=0.03)


SOUNDS = {
    "scratch": gen_scratch,
    "dj_airhorn": gen_dj_airhorn,
    "riser": gen_riser,
}

if __name__ == "__main__":
    os.makedirs(SFX_DIR, exist_ok=True)
    for name, fn in SOUNDS.items():
        path = f"{SFX_DIR}/{name}.wav"
        samples = fn()
        _write_wav(path, samples)
        print(f"✅  {path}  ({len(samples) / RATE:.2f}s)")
    print("Done.")
