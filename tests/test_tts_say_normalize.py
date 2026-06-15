"""TDD: macOS say 終極備援的可聽度修正。

2026-06-14 incident：edge-tts 被微軟限流（22:00–23:30 滿屏 No audio was
received），say 終極備援有觸發、也有推幀進 mixer，但使用者「沒聽到」。
診斷：say 輸出振幅天生比 edge-tts 低，疊上近期 mixer tts_gain=0.5 後蓋在
音樂下偏小聲。

修法：對 say 產出的 WAV 做峰值正規化（peak normalization）拉到接近滿幅，
補償後段的 0.5 gain，讓限流退備援時仍清楚可聽。純函式、stdlib only。
"""
from __future__ import annotations

import io
import wave
from array import array

import pytest

from tts_engine import peak_normalize_wav_bytes


def _make_wav(samples: list[int], framerate: int = 44100) -> bytes:
    """產一個 mono 16-bit LE WAV bytes。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(framerate)
        w.writeframes(array("h", samples).tobytes())
    return buf.getvalue()


def _peak_of(wav_bytes: bytes) -> int:
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        frames = w.readframes(w.getnframes())
    return max((abs(s) for s in array("h", frames)), default=0)


def test_quiet_wav_boosted_to_target_peak():
    """低振幅（peak≈3000）→ 拉到 ≈0.9 滿幅（29490）。"""
    quiet = _make_wav([0, 1500, -3000, 2000, -1000] * 100)
    out = peak_normalize_wav_bytes(quiet, target_peak_ratio=0.9)
    peak = _peak_of(out)
    target = int(0.9 * 32767)
    assert abs(peak - target) <= 2, f"峰值應拉到 ~{target}，實際 {peak}"


def test_silent_wav_returned_unchanged():
    """全靜音（max=0）→ 不可除以零，原樣回傳。"""
    silent = _make_wav([0] * 500)
    out = peak_normalize_wav_bytes(silent, target_peak_ratio=0.9)
    assert out == silent


def test_loud_wav_scaled_down_to_target():
    """近滿幅（peak≈32000）→ 也縮到 target（一致響度，不爆音）。"""
    loud = _make_wav([32000, -32000, 16000, -16000] * 100)
    out = peak_normalize_wav_bytes(loud, target_peak_ratio=0.9)
    peak = _peak_of(out)
    assert peak <= int(0.9 * 32767) + 2


def test_output_stays_valid_wav_same_format():
    """正規化後仍是合法 WAV，且聲道/取樣率/取樣寬度不變。"""
    src = _make_wav([0, 5000, -5000] * 50, framerate=44100)
    out = peak_normalize_wav_bytes(src, target_peak_ratio=0.9)
    with wave.open(io.BytesIO(out), "rb") as w:
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getframerate() == 44100


def test_corrupt_bytes_fail_open_returns_original():
    """非 WAV / 壞資料 → 不丟例外，原樣回傳（fail-open，絕不中斷 TTS）。"""
    junk = b"not a wav at all"
    assert peak_normalize_wav_bytes(junk, target_peak_ratio=0.9) == junk
