"""
SpatialVoiceRenderer — High Performance DSP Audio Rendering for Spatial Intelligence

Provides dynamic spectral fitting, acoustic environment matching, stereo panning,
and distance scaling on float32 stereo audio (48kHz) using numpy and scipy.signal.
"""
from __future__ import annotations

import logging
import numpy as np
from scipy import signal

logger = logging.getLogger(__name__)


class SpatialVoiceRenderer:
    def __init__(self, sample_rate: int = 48000):
        self.sample_rate = sample_rate

    def _ensure_2d(self, audio: np.ndarray) -> tuple[np.ndarray, bool]:
        """Ensure audio is shape (N, 2) float32."""
        audio = np.asarray(audio, dtype=np.float32)
        is_interleaved = False
        if audio.ndim == 1:
            is_interleaved = True
            # Reshape 1D interleaved array (L, R, L, R, ...) to (N, 2)
            if audio.size % 2 != 0:
                audio = audio[:audio.size - (audio.size % 2)]
            audio = audio.reshape(-1, 2)
        elif audio.ndim == 2 and audio.shape[0] == 2 and audio.shape[1] != 2:
            audio = audio.T
        return audio, is_interleaved

    def _restore_shape(self, audio: np.ndarray, is_interleaved: bool) -> np.ndarray:
        if is_interleaved:
            return audio.reshape(-1)
        return audio

    def apply_stereo_panning(self, audio: np.ndarray, pan: float = 0.0) -> np.ndarray:
        """Constant power stereo panning. pan in [-1.0 (left), 1.0 (right)]."""
        arr, is_1d = self._ensure_2d(audio)
        if arr.size == 0:
            return audio
        clamped_pan = max(-1.0, min(1.0, float(pan)))
        angle = (clamped_pan + 1.0) * (np.pi / 4.0)  # 0 to pi/2
        gain_l = float(np.cos(angle))
        gain_r = float(np.sin(angle))

        out = np.empty_like(arr)
        out[:, 0] = arr[:, 0] * gain_l
        out[:, 1] = arr[:, 1] * gain_r
        return self._restore_shape(out, is_1d)

    def apply_proximity_boost(self, audio: np.ndarray, gain_db: float = 3.0) -> np.ndarray:
        """Low-shelf boost around 150 Hz (Proximity Effect)."""
        arr, is_1d = self._ensure_2d(audio)
        if arr.size == 0 or gain_db <= 0.0:
            return audio

        # 2nd order low-shelf IIR filter at 150 Hz
        cutoff = 150.0
        A = 10.0 ** (gain_db / 40.0)
        w0 = 2.0 * np.pi * cutoff / self.sample_rate
        alpha = np.sin(w0) / 2.0 * np.sqrt(2.0)

        b0 = A * ((A + 1.0) - (A - 1.0) * np.cos(w0) + 2.0 * np.sqrt(A) * alpha)
        b1 = 2.0 * A * ((A - 1.0) - (A + 1.0) * np.cos(w0))
        b2 = A * ((A + 1.0) - (A - 1.0) * np.cos(w0) - 2.0 * np.sqrt(A) * alpha)
        a0 = (A + 1.0) + (A - 1.0) * np.cos(w0) + 2.0 * np.sqrt(A) * alpha
        a1 = -2.0 * ((A - 1.0) + (A + 1.0) * np.cos(w0))
        a2 = (A + 1.0) + (A - 1.0) * np.cos(w0) - 2.0 * np.sqrt(A) * alpha

        b = np.array([b0, b1, b2], dtype=np.float64) / a0
        a = np.array([1.0, a1 / a0, a2 / a0], dtype=np.float64)

        out = signal.lfilter(b, a, arr, axis=0).astype(np.float32)
        return self._restore_shape(out, is_1d)

    def apply_vocal_clarity_boost(self, audio: np.ndarray, gain_db: float = 3.0) -> np.ndarray:
        """Peaking EQ filter at 3000 Hz (Q=1.5) for vocal clarity cut-through."""
        arr, is_1d = self._ensure_2d(audio)
        if arr.size == 0 or gain_db <= 0.0:
            return audio

        f0 = 3000.0
        Q = 1.5
        A = 10.0 ** (gain_db / 40.0)
        w0 = 2.0 * np.pi * f0 / self.sample_rate
        alpha = np.sin(w0) / (2.0 * Q)

        b0 = 1.0 + alpha * A
        b1 = -2.0 * np.cos(w0)
        b2 = 1.0 - alpha * A
        a0 = 1.0 + alpha / A
        a1 = -2.0 * np.cos(w0)
        a2 = 1.0 - alpha / A

        b = np.array([b0, b1, b2], dtype=np.float64) / a0
        a = np.array([1.0, a1 / a0, a2 / a0], dtype=np.float64)

        out = signal.lfilter(b, a, arr, axis=0).astype(np.float32)
        return self._restore_shape(out, is_1d)

    def apply_music_sidechain_mid_cut(self, music_audio: np.ndarray, cut_db: float = -4.0) -> np.ndarray:
        """Peaking notch cut on music layer at 3000 Hz to free mid frequencies for vocals."""
        arr, is_1d = self._ensure_2d(music_audio)
        if arr.size == 0 or cut_db >= 0.0:
            return music_audio

        f0 = 3000.0
        Q = 1.5
        A = 10.0 ** (cut_db / 40.0)
        w0 = 2.0 * np.pi * f0 / self.sample_rate
        alpha = np.sin(w0) / (2.0 * Q)

        b0 = 1.0 + alpha * A
        b1 = -2.0 * np.cos(w0)
        b2 = 1.0 - alpha * A
        a0 = 1.0 + alpha / A
        a1 = -2.0 * np.cos(w0)
        a2 = 1.0 - alpha / A

        b = np.array([b0, b1, b2], dtype=np.float64) / a0
        a = np.array([1.0, a1 / a0, a2 / a0], dtype=np.float64)

        out = signal.lfilter(b, a, arr, axis=0).astype(np.float32)
        return self._restore_shape(out, is_1d)

    def apply_high_pass_filter(self, audio: np.ndarray, cutoff_hz: float = 80.0) -> np.ndarray:
        """Butterworth 2nd order high pass filter."""
        arr, is_1d = self._ensure_2d(audio)
        if arr.size == 0 or cutoff_hz <= 0.0:
            return audio
        sos = signal.butter(2, cutoff_hz, btype='highpass', fs=self.sample_rate, output='sos')
        out = signal.sosfilt(sos, arr, axis=0).astype(np.float32)
        return self._restore_shape(out, is_1d)

    def apply_low_pass_filter(self, audio: np.ndarray, cutoff_hz: float = 8000.0) -> np.ndarray:
        """Butterworth 2nd order low pass filter."""
        arr, is_1d = self._ensure_2d(audio)
        if arr.size == 0 or cutoff_hz >= self.sample_rate / 2.0:
            return audio
        sos = signal.butter(2, cutoff_hz, btype='lowpass', fs=self.sample_rate, output='sos')
        out = signal.sosfilt(sos, arr, axis=0).astype(np.float32)
        return self._restore_shape(out, is_1d)

    def apply_distance_scaling(self, audio: np.ndarray, distance: float = 0.0) -> np.ndarray:
        """Distance scaling: gain offset (-3dB * distance) + early reflection multi-tap delay."""
        arr, is_1d = self._ensure_2d(audio)
        if arr.size == 0 or distance <= 0.0:
            return audio

        dist = max(0.0, min(1.0, float(distance)))
        gain_factor = 10.0 ** (-3.0 * dist / 20.0)
        scaled = arr * gain_factor

        # Add early reflection (20ms delay tap)
        delay_samples = int(0.020 * self.sample_rate)
        if scaled.shape[0] > delay_samples:
            reflection = np.zeros_like(scaled)
            reflection[delay_samples:] = scaled[:-delay_samples] * (0.2 * dist)
            scaled = scaled + reflection

        return self._restore_shape(scaled, is_1d)

    def apply_acoustic_preset(self, audio: np.ndarray, preset: str = "CLEAN_ROOM") -> np.ndarray:
        """Apply acoustic environment matching presets."""
        preset_upper = (preset or "CLEAN_ROOM").upper()
        if preset_upper == "LATE_NIGHT_CHILL":
            # Gain -6dB, Low-pass at 8000Hz, subtle pre-delay
            arr = audio * (10.0 ** (-6.0 / 20.0))
            arr = self.apply_low_pass_filter(arr, 8000.0)
            return arr
        elif preset_upper == "OUTDOOR_CAMP":
            # Dead dry, High-pass at 80Hz
            return self.apply_high_pass_filter(audio, 80.0)
        elif preset_upper == "PARTY_CROWD":
            # Dynamic compression / punchy limiter
            arr, is_1d = self._ensure_2d(audio)
            if arr.size == 0:
                return audio
            # Simple soft-knee compressor simulation
            peak = np.max(np.abs(arr))
            if peak > 0.3:
                compressed = np.tanh(arr * 1.5) / 1.2
            else:
                compressed = arr * 1.1
            return self._restore_shape(compressed.astype(np.float32), is_1d)

        return audio

    def render_spatial_voice(
        self,
        raw_audio: np.ndarray,
        spatial_control: dict | None = None
    ) -> np.ndarray:
        """Complete DSP audio rendering pipeline for Spatial Intelligence."""
        if not spatial_control or not isinstance(spatial_control, dict):
            return raw_audio

        audio = raw_audio
        # 1. Proximity Boost
        prox_db = float(spatial_control.get("proximity_boost_db", 0.0))
        if prox_db > 0.0:
            audio = self.apply_proximity_boost(audio, prox_db)

        # 2. Vocal Clarity Boost
        clarity_db = float(spatial_control.get("clarity_boost_db", 0.0))
        if clarity_db > 0.0:
            audio = self.apply_vocal_clarity_boost(audio, clarity_db)

        # 3. Acoustic Environment Preset
        preset = spatial_control.get("reverb_preset", "CLEAN_ROOM")
        if preset and preset != "CLEAN_ROOM":
            audio = self.apply_acoustic_preset(audio, preset)

        # 4. Distance Scaling
        dist = float(spatial_control.get("distance", 0.0))
        if dist > 0.0:
            audio = self.apply_distance_scaling(audio, dist)

        # 5. Stereo Panning
        pan = float(spatial_control.get("panning", 0.0))
        if pan != 0.0:
            audio = self.apply_stereo_panning(audio, pan)

        return audio
