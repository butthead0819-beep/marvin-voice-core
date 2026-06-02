"""Plan 12 純 DSP module — gain / f32 mix-sum / TPDF dither / s16 clip。

這份 DSP 由 offline A/B script（已驗證 ±2 LSB）與 live LocalMixingAudioSource 共用，
所以測試鎖住「抽出的演算法 == offline render_path_b 的演算法」逐位元一致。
"""
from __future__ import annotations

import numpy as np
import pytest

import audio_mixing as am


# ── apply_gain ───────────────────────────────────────────────────────────────

def test_apply_gain_scales_elementwise():
    frame = np.array([0.5, -0.5, 0.25], dtype=np.float32)
    out = am.apply_gain(frame, 0.5)
    assert np.allclose(out, [0.25, -0.25, 0.125])
    assert out.dtype == np.float32


def test_apply_gain_zero_mutes():
    frame = np.array([0.9, -0.9], dtype=np.float32)
    assert np.all(am.apply_gain(frame, 0.0) == 0.0)


def test_apply_gain_unity_unchanged():
    frame = np.array([0.3, -0.7, 1.0], dtype=np.float32)
    assert np.array_equal(am.apply_gain(frame, 1.0), frame)


# ── mix_layers ───────────────────────────────────────────────────────────────

def test_mix_layers_sums_same_shape():
    a = np.array([0.1, 0.2], dtype=np.float32)
    b = np.array([0.3, -0.1], dtype=np.float32)
    out = am.mix_layers([a, b])
    assert np.allclose(out, [0.4, 0.1])
    assert out.dtype == np.float32


def test_mix_layers_single_layer_is_itself():
    a = np.array([0.5, -0.5], dtype=np.float32)
    assert np.array_equal(am.mix_layers([a]), a)


# ── tpdf_dither ──────────────────────────────────────────────────────────────

def test_tpdf_dither_within_one_lsb():
    frame = np.zeros(2048, dtype=np.float32)
    rng = np.random.default_rng(123)
    out = am.tpdf_dither(frame, rng)
    lsb = 1.0 / 32768.0
    # 兩個 Uniform(-0.5,0.5) 相加 ∈ [-1,1] → 偏移幅度 ≤ 1 LSB
    assert np.all(np.abs(out) <= lsb + 1e-12)
    assert out.dtype == np.float32


def test_tpdf_dither_deterministic_with_same_seed():
    frame = np.array([0.1, -0.2, 0.3], dtype=np.float32)
    out1 = am.tpdf_dither(frame, np.random.default_rng(999))
    out2 = am.tpdf_dither(frame, np.random.default_rng(999))
    assert np.array_equal(out1, out2)


# ── to_s16 ───────────────────────────────────────────────────────────────────

def test_to_s16_clips_overflow_no_wrap():
    frame = np.array([2.0, -2.0], dtype=np.float32)  # 超出 [-1,1]
    out = am.to_s16(frame)
    assert list(out) == [32767, -32768]
    assert out.dtype == np.int16


def test_to_s16_unity_and_zero():
    frame = np.array([1.0, 0.0, -1.0], dtype=np.float32)
    out = am.to_s16(frame)
    assert list(out) == [32767, 0, -32768]


# ── 抽取忠實度：與 offline render_path_b 演算法逐位元一致 ────────────────────────

def test_pipeline_matches_offline_render_path_b_formula():
    """gain → tpdf(seeded) → to_s16 必須等於 offline script 的內聯算式。"""
    f32 = np.array([[0.10, -0.20], [0.30, -0.40]], dtype=np.float32)
    volume = 0.30

    # via module
    gained = am.apply_gain(f32, volume)
    dithered = am.tpdf_dither(gained, np.random.default_rng(0xD17 ^ int(volume * 1000)))
    got = am.to_s16(dithered)

    # offline 內聯算式（render_path_b lines 71-80）逐字重現
    exp_gained = f32 * np.float32(volume)
    rng = np.random.default_rng(0xD17 ^ int(volume * 1000))
    lsb = np.float32(1.0 / 32768.0)
    d1 = rng.uniform(-0.5, 0.5, size=exp_gained.shape).astype(np.float32)
    d2 = rng.uniform(-0.5, 0.5, size=exp_gained.shape).astype(np.float32)
    exp_dithered = exp_gained + (d1 + d2) * lsb
    exp = np.clip(np.round(exp_dithered * 32768.0), -32768, 32767).astype(np.int16)

    assert np.array_equal(got, exp)
