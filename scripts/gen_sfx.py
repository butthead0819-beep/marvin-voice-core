#!/usr/bin/env python3
"""
Generate synthesized WAV sound effects for the Busted game.
No external dependencies — uses only stdlib wave/math/struct.
Run from repo root: python scripts/gen_sfx.py
"""
import math
import os
import struct
import wave

RATE = 44100
SFX_DIR = "assets/sfx"


def make_wav(path: str, samples: list) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with wave.open(path, "w") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(RATE)
        data = b"".join(
            struct.pack("<h", max(-32767, min(32767, int(s * 32767)))) for s in samples
        )
        f.writeframes(data)


def sine_seg(freq: float, dur: float, amp: float = 0.65) -> list:
    n = int(RATE * dur)
    return [amp * math.sin(2 * math.pi * freq * i / RATE) for i in range(n)]


def fade(samples: list, attack: float = 0.01, release: float = 0.08) -> list:
    n = len(samples)
    att = int(RATE * attack)
    rel = int(RATE * release)
    for i in range(min(att, n)):
        samples[i] *= i / att
    for i in range(min(rel, n)):
        idx = n - rel + i
        if 0 <= idx < n:
            samples[idx] *= (rel - i) / rel
    return samples


def concat(*segs) -> list:
    out = []
    for s in segs:
        out.extend(s)
    return out


# ── Sound definitions ──────────────────────────────────────────────────────────

def gen_fanfare() -> list:
    """Short ascending fanfare — game starts (C-E-G-C)."""
    parts = []
    for freq, dur in [(262, 0.12), (330, 0.12), (392, 0.12), (523, 0.45)]:
        parts.append(fade(sine_seg(freq, dur, 0.65), attack=0.01, release=0.06))
    return concat(*parts)


def gen_buzz() -> list:
    """Rising sweep buzz — someone hits BUZZ IN."""
    n = int(RATE * 0.22)
    samples = []
    for i in range(n):
        # Linear frequency sweep 220 → 1100 Hz
        phase_acc = 2 * math.pi * (220 + 880 * (i / n)) * i / RATE
        samples.append(0.70 * math.sin(phase_acc))
    return fade(samples, attack=0.005, release=0.07)


def gen_correct() -> list:
    """Happy 4-note arpeggio — correct answer (C5-E5-G5-C6)."""
    parts = []
    for freq, dur in [(523, 0.11), (659, 0.11), (784, 0.11), (1047, 0.5)]:
        parts.append(fade(sine_seg(freq, dur, 0.62), attack=0.01, release=0.08))
    return concat(*parts)


def gen_wrong() -> list:
    """Descending sad tones — wrong answer / timeout (A4→F#4→D4→B3)."""
    parts = []
    for freq, dur in [(440, 0.28), (370, 0.28), (294, 0.28), (247, 0.55)]:
        seg = sine_seg(freq, dur, 0.55)
        # Gentle vibrato for trombone feel
        n = len(seg)
        for i in range(n):
            t = i / RATE
            seg[i] *= 1.0 + 0.025 * math.sin(2 * math.pi * 5.5 * t)
        parts.append(fade(seg, attack=0.01, release=0.09))
    return concat(*parts)


def gen_sad_horn() -> list:
    """Wah-wah descending — nobody guessed / round 5 penalty (F4→D4→B3)."""
    parts = []
    for freq, dur in [(349, 0.42), (294, 0.42), (247, 0.65)]:
        seg = sine_seg(freq, dur, 0.60)
        n = len(seg)
        for i in range(n):
            t = i / RATE
            # Wah: amp modulation 3 Hz
            seg[i] *= 0.72 + 0.28 * math.sin(2 * math.pi * 3 * t)
        parts.append(fade(seg, attack=0.01, release=0.12))
    return concat(*parts)


def gen_game_over() -> list:
    """Triumphant 5-note finish — game ends (C-E-G-C-E)."""
    parts = []
    for freq, dur in [(262, 0.11), (330, 0.11), (392, 0.11), (523, 0.11), (659, 0.8)]:
        rel = 0.25 if dur > 0.5 else 0.06
        parts.append(fade(sine_seg(freq, dur, 0.70), attack=0.01, release=rel))
    return concat(*parts)


# ── Runner ─────────────────────────────────────────────────────────────────────

SOUNDS = {
    "fanfare":   gen_fanfare,
    "buzz":      gen_buzz,
    "correct":   gen_correct,
    "wrong":     gen_wrong,
    "sad_horn":  gen_sad_horn,
    "game_over": gen_game_over,
}

if __name__ == "__main__":
    os.makedirs(SFX_DIR, exist_ok=True)
    for name, fn in SOUNDS.items():
        path = f"{SFX_DIR}/{name}.wav"
        make_wav(path, fn())
        print(f"✅  {path}")
    print("Done.")
