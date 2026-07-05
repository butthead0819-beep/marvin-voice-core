"""tests/test_smoke_local_speaker.py

TDD：LocalMixingAudioSource → MixerPlaybackAdapter → LocalSpeakerDevice
整條本機輸出 smoke 路徑，注入假 output，無音訊硬體依賴。

先紅（scripts/smoke_local_speaker.py 不存在）→ 再綠（實作後）。
"""
from __future__ import annotations

import math
import threading
import time

import numpy as np

from local_mixing_source import (
    LocalMixingAudioSource,
    MixerPlaybackAdapter,
    ensure_mixer_playing,
    FRAME_BYTES_S16,
    FRAME_SAMPLES,
)
from marvin_voice_core.playback_device import LocalSpeakerDevice


# ── Helpers ───────────────────────────────────────────────────────────────────

class _FrameCollector:
    """Thread-safe frame collector: write(frame) 收幀、close() 記錄。"""

    def __init__(self) -> None:
        self._frames: list[bytes] = []
        self._lock = threading.Lock()
        self.closed: bool = False

    def write(self, frame: bytes) -> None:
        with self._lock:
            self._frames.append(bytes(frame))

    def close(self) -> None:
        self.closed = True

    def count(self) -> int:
        with self._lock:
            return len(self._frames)

    def snapshot(self) -> list[bytes]:
        with self._lock:
            return list(self._frames)


def _wait_until(condition, timeout: float = 5.0, interval: float = 0.001) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(interval)
    return False


def _make_tts_f32(duration_s: float = 0.1, amplitude: float = 0.3) -> np.ndarray:
    """建 48k 立體聲 f32 interleaved 正弦波 buffer（值域 [-1, 1]）。"""
    n_samples = int(duration_s * 48000) * 2  # stereo interleaved
    t = np.linspace(0, duration_s, n_samples, endpoint=False, dtype=np.float32)
    return (np.sin(2 * np.pi * 440 * t) * amplitude).astype(np.float32)


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_smoke_e2e_s16le_frames_flow_through_chain():
    """真鏈端到端：f32 TTS → mixer → MixerPlaybackAdapter → LocalSpeakerDevice(注入) → s16le 3840B 幀依序流出。"""
    buf = _make_tts_f32(duration_s=0.1, amplitude=0.3)
    expected_tts_frames = math.ceil(buf.size / FRAME_SAMPLES)  # ceil(9600 / 1920) = 5

    mixer = LocalMixingAudioSource()
    pushed = mixer.push_tts(buf)
    assert pushed, "push_tts 應回 True（buffer 遠小於 30s cap）"

    collector = _FrameCollector()
    device = LocalSpeakerDevice(output=collector, frame_duration=0)
    armed = ensure_mixer_playing(device, lambda: MixerPlaybackAdapter(mixer))
    assert armed, "ensure_mixer_playing 應 arm 成功"

    # 等到收到 TTS 幀 + 至少 1 幀 silence（TTS 已完全流過）
    target = expected_tts_frames + 1
    ok = _wait_until(lambda: collector.count() >= target, timeout=5.0)
    device.stop()

    assert ok, f"期待 ≥{target} 幀但 timeout，只收到 {collector.count()} 幀"

    frames = collector.snapshot()
    assert mixer.is_opus() is False, "mixer.is_opus() 必須 False"
    assert all(len(f) == FRAME_BYTES_S16 for f in frames), (
        f"每幀應為 {FRAME_BYTES_S16} bytes，但有：{set(len(f) for f in frames)}"
    )
    assert len(frames) >= expected_tts_frames, (
        f"幀數應 ≥ {expected_tts_frames}（TTS frames），實際 {len(frames)}"
    )

    silence = b"\x00" * FRAME_BYTES_S16
    assert any(f != silence for f in frames[:expected_tts_frames + 2]), (
        "前幾幀應含非零 s16le（TTS 內容有流過去）"
    )


def test_play_f32_through_local_chain_dry():
    """smoke_local_speaker.play_f32_through_local_chain 可用注入輸出跑 dry，回傳正確 3840B 幀。"""
    from scripts.smoke_local_speaker import play_f32_through_local_chain

    buf = _make_tts_f32(duration_s=0.06, amplitude=0.25)
    expected_tts_frames = math.ceil(buf.size / FRAME_SAMPLES)

    collector = _FrameCollector()
    play_f32_through_local_chain(buf, output=collector, duration_s=0.1, frame_duration=0)

    frames = collector.snapshot()
    assert all(len(f) == FRAME_BYTES_S16 for f in frames), (
        f"每幀應為 {FRAME_BYTES_S16} bytes，但有：{set(len(f) for f in frames)}"
    )
    assert len(frames) >= expected_tts_frames, (
        f"幀數應 ≥ {expected_tts_frames}，實際 {len(frames)}"
    )
    silence = b"\x00" * FRAME_BYTES_S16
    assert any(f != silence for f in frames), "應有非零幀（TTS 內容流過）"
