"""
Unit tests for SpatialVoiceRenderer (DSP Audio Rendering for Spatial Intelligence)
"""
import pytest
import numpy as np
from spatial_voice_renderer import SpatialVoiceRenderer


def test_spatial_voice_renderer_init():
    renderer = SpatialVoiceRenderer(sample_rate=48000)
    assert renderer.sample_rate == 48000


def test_stereo_panning():
    renderer = SpatialVoiceRenderer(sample_rate=48000)
    # 1 second of mono audio (float32) converted to stereo frame (N, 2)
    t = np.linspace(0, 1.0, 48000, dtype=np.float32)
    mono_signal = np.sin(2 * np.pi * 440 * t, dtype=np.float32)
    stereo_signal = np.column_stack((mono_signal, mono_signal))

    # Center panning (0.0) -> left and right should be equal
    center_panned = renderer.apply_stereo_panning(stereo_signal, pan=0.0)
    assert np.allclose(center_panned[:, 0], center_panned[:, 1], atol=1e-4)

    # Full left panning (-1.0) -> right channel should be nearly silent
    left_panned = renderer.apply_stereo_panning(stereo_signal, pan=-1.0)
    assert np.max(np.abs(left_panned[:, 0])) > 0.5
    assert np.max(np.abs(left_panned[:, 1])) < 1e-3

    # Full right panning (1.0) -> left channel should be nearly silent
    right_panned = renderer.apply_stereo_panning(stereo_signal, pan=1.0)
    assert np.max(np.abs(right_panned[:, 1])) > 0.5
    assert np.max(np.abs(right_panned[:, 0])) < 1e-3


def test_proximity_boost():
    renderer = SpatialVoiceRenderer(sample_rate=48000)
    t = np.linspace(0, 1.0, 48000, dtype=np.float32)
    # Low frequency signal (150Hz)
    low_sig = np.sin(2 * np.pi * 150 * t, dtype=np.float32)
    stereo_low = np.column_stack((low_sig, low_sig))

    boosted = renderer.apply_proximity_boost(stereo_low, gain_db=3.0)
    # Peak amplitude of low frequency signal should increase after boost
    assert np.max(np.abs(boosted)) > np.max(np.abs(stereo_low))


def test_vocal_clarity_boost():
    renderer = SpatialVoiceRenderer(sample_rate=48000)
    t = np.linspace(0, 1.0, 48000, dtype=np.float32)
    # Mid-high clarity signal (3000Hz)
    mid_sig = np.sin(2 * np.pi * 3000 * t, dtype=np.float32)
    stereo_mid = np.column_stack((mid_sig, mid_sig))

    boosted = renderer.apply_vocal_clarity_boost(stereo_mid, gain_db=3.0)
    assert np.max(np.abs(boosted)) > np.max(np.abs(stereo_mid))


def test_music_sidechain_mid_cut():
    renderer = SpatialVoiceRenderer(sample_rate=48000)
    t = np.linspace(0, 1.0, 48000, dtype=np.float32)
    music_sig = np.sin(2 * np.pi * 3000 * t, dtype=np.float32)
    stereo_music = np.column_stack((music_sig, music_sig))

    cut = renderer.apply_music_sidechain_mid_cut(stereo_music, cut_db=-4.0)
    # Peak amplitude of music mid frequency should decrease
    assert np.max(np.abs(cut)) < np.max(np.abs(stereo_music))


def test_acoustic_presets():
    renderer = SpatialVoiceRenderer(sample_rate=48000)
    t = np.linspace(0, 1.0, 48000, dtype=np.float32)
    sig = np.sin(2 * np.pi * 1000 * t, dtype=np.float32)
    stereo_sig = np.column_stack((sig, sig))

    # LATE_NIGHT_CHILL preset (attenuates overall volume)
    late_night = renderer.apply_acoustic_preset(stereo_sig, preset="LATE_NIGHT_CHILL")
    assert np.max(np.abs(late_night)) < np.max(np.abs(stereo_sig))

    # OUTDOOR_CAMP preset (applies high-pass filter)
    outdoor = renderer.apply_acoustic_preset(stereo_sig, preset="OUTDOOR_CAMP")
    assert outdoor.shape == stereo_sig.shape

    # PARTY_CROWD preset (compressor dynamic range adjustment)
    party = renderer.apply_acoustic_preset(stereo_sig, preset="PARTY_CROWD")
    assert party.shape == stereo_sig.shape


def test_render_spatial_voice_end_to_end():
    renderer = SpatialVoiceRenderer(sample_rate=48000)
    t = np.linspace(0, 1.0, 48000, dtype=np.float32)
    sig = np.sin(2 * np.pi * 500 * t, dtype=np.float32)
    stereo_sig = np.column_stack((sig, sig))

    spatial_control = {
        "panning": -0.5,
        "proximity_boost_db": 3.0,
        "clarity_boost_db": 2.0,
        "reverb_preset": "LATE_NIGHT_CHILL",
        "distance": 0.3
    }

    rendered = renderer.render_spatial_voice(stereo_sig, spatial_control)
    assert rendered.dtype == np.float32
    assert rendered.shape == stereo_sig.shape
    # Panning to left should make left channel stronger than right channel
    assert np.max(np.abs(rendered[:, 0])) > np.max(np.abs(rendered[:, 1]))
