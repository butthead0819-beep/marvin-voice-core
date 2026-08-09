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


def _lowpass(sig: np.ndarray, cutoff: float, order: int = 2) -> np.ndarray:
    sos = butter(order, cutoff, btype="lowpass", fs=RATE, output="sos")
    return sosfilt(sos, sig)


def _bandpass(sig: np.ndarray, low: float, high: float, order: int = 2) -> np.ndarray:
    sos = butter(order, [low, high], btype="bandpass", fs=RATE, output="sos")
    return sosfilt(sos, sig)


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
    """刷碟：正反來回的音高擺盪 + 濾波噪點，模擬 DJ 手刷黑膠。

    上一版用方波（np.sign）當音高擺盪的音色，方波高次諧波一路延伸到 Nyquist，
    聽起來像 8-bit 蜂鳴而非刷碟——這是「不自然」的主因。改用正弦音高擺盪
    （音高本身的來回擺動已經有辨識度，不需要靠方波刺耳感）+ lowpass 收乾淨；
    噪點也從全頻域白噪音改成 bandpass 濾到中高頻段，模擬唱針磨擦黑膠的顆粒感
    而非一片嘶聲。
    """
    dur = 0.55
    n = int(RATE * dur)
    t = np.arange(n) / RATE
    # 音高在 3 段來回擺盪（去-回-去），模擬手刷來回
    sweep = 900 + 700 * np.sin(2 * np.pi * 3.2 * t)
    phase = 2 * np.pi * np.cumsum(sweep) / RATE
    tone = _lowpass(np.sin(phase), 3500)
    noise = np.random.default_rng(0).uniform(-1, 1, n)
    noise = _bandpass(noise, 400, 6000)
    sig = 0.55 * tone + 0.3 * noise
    env = 0.6 + 0.4 * np.abs(np.sin(2 * np.pi * 3.2 * t))
    return _fade((sig * env).astype(np.float32), attack=0.005, release=0.1)


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
