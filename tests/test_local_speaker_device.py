"""tests/test_local_speaker_device.py

TDD：LocalSpeakerDevice 泵行為 + PlaybackDevice Protocol 滿足。
注入假 source + 假 output，無任何硬體依賴。

先寫測試（全紅），再寫實作（全綠）。
"""
from __future__ import annotations

import threading
import time


# ── Helpers ──────────────────────────────────────────────────────────────────

# 任意假 PCM 幀 (值不重要，size 不需等於 3840)
FRAME = b"\x01\x02" * 100


class _FakeSource:
    """吐預設幀串後回 b\"\" 的假 source。"""

    def __init__(self, frames: list[bytes]) -> None:
        self._frames = list(frames)
        self._idx = 0

    def read(self) -> bytes:
        if self._idx < len(self._frames):
            frame = self._frames[self._idx]
            self._idx += 1
            return frame
        return b""


class _InfiniteSource:
    """永遠回相同幀、永不耗盡的假 source（用於 stop() 中止測試）。"""

    def __init__(self, frame: bytes = FRAME) -> None:
        self._frame = frame
        self.reads: int = 0

    def read(self) -> bytes:
        self.reads += 1
        return self._frame


class _FakeOutput:
    """收集所有 write() 呼叫並記錄 close() 的假輸出（write()/close() 介面）。"""

    def __init__(self) -> None:
        self.frames: list[bytes] = []
        self.closed: bool = False

    def write(self, frame: bytes) -> None:
        self.frames.append(frame)

    def close(self) -> None:
        self.closed = True


def _wait_until(condition, timeout: float = 2.0, interval: float = 0.001) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(interval)
    return False


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_pump_writes_all_frames_and_calls_after_once():
    """泵自然耗盡：N 幀依序寫出，after 呼叫恰一次，結束後 is_playing() False，output 被關閉。"""
    from marvin_voice_core.playback_device import LocalSpeakerDevice

    N = 5
    frames = [FRAME] * N
    source = _FakeSource(frames)
    output = _FakeOutput()
    after_calls: list = []

    dev = LocalSpeakerDevice(output=output, frame_duration=0)
    dev.play(source, after=lambda err: after_calls.append(err))

    # 等泵執行緒結束（比輪詢更確定）
    assert dev._thread is not None
    dev._thread.join(timeout=2.0)

    assert output.frames == frames, "幀順序或數量不符"
    assert len(after_calls) == 1, f"after 應呼叫恰一次，實際 {len(after_calls)}"
    assert not dev.is_playing(), "泵結束後 is_playing() 應 False"
    assert output.closed, "泵結束後 output 應被關閉"


def test_stop_aborts_pump_midway():
    """stop() 途中中止：泵停、is_playing() False、output 被關閉、after 不被呼叫。"""
    from marvin_voice_core.playback_device import LocalSpeakerDevice

    source = _InfiniteSource()
    output = _FakeOutput()
    after_calls: list = []

    dev = LocalSpeakerDevice(output=output, frame_duration=0)
    dev.play(source, after=lambda err: after_calls.append(err))

    # 等泵真正開始跑（is_playing True）
    assert _wait_until(lambda: dev.is_playing(), timeout=1.0), "泵未在預期時間內啟動"

    dev.stop()

    assert not dev.is_playing(), "stop() 後 is_playing() 應 False"
    assert output.closed, "stop() 後 output 應被關閉"
    assert len(after_calls) == 0, "stop() 中止不應呼叫 after"


def test_is_playing_and_is_connected_states():
    """is_playing() 與 is_connected() 在各階段的狀態正確。"""
    from marvin_voice_core.playback_device import LocalSpeakerDevice

    source = _InfiniteSource()
    output = _FakeOutput()

    dev = LocalSpeakerDevice(output=output, frame_duration=0)

    assert not dev.is_playing(), "play 前 is_playing() 應 False"
    assert dev.is_connected(), "is_connected() 應恆 True（play 前）"

    dev.play(source)

    assert _wait_until(lambda: dev.is_playing(), timeout=1.0), "play 後 is_playing() 應 True"
    assert dev.is_connected(), "is_connected() 應恆 True（播放中）"

    dev.stop()

    assert not dev.is_playing(), "stop() 後 is_playing() 應 False"
    assert dev.is_connected(), "is_connected() 應恆 True（stop 後）"


def test_runtime_checkable_isinstance():
    """LocalSpeakerDevice 滿足 PlaybackDevice Protocol（runtime_checkable）。"""
    from marvin_voice_core.playback_device import LocalSpeakerDevice
    from protocols import PlaybackDevice

    dev = LocalSpeakerDevice(output=_FakeOutput(), frame_duration=0)
    assert isinstance(dev, PlaybackDevice)


def test_arm_mixer_starts_pump():
    """arm_mixer(source) 委派 play() 啟動泵：幀寫出、is_playing() 最終 False（自然耗盡）。"""
    from marvin_voice_core.playback_device import LocalSpeakerDevice

    N = 4
    source = _FakeSource([FRAME] * N)
    output = _FakeOutput()

    dev = LocalSpeakerDevice(output=output, frame_duration=0)
    dev.arm_mixer(source)

    assert dev._thread is not None
    dev._thread.join(timeout=2.0)

    assert output.frames == [FRAME] * N
    assert not dev.is_playing()


def test_arm_mixer_idempotent_when_already_playing():
    """arm_mixer 已在播時為 no-op：不啟第二個泵、is_playing() 仍 True。"""
    from marvin_voice_core.playback_device import LocalSpeakerDevice

    source1 = _InfiniteSource()
    source2 = _InfiniteSource()
    output = _FakeOutput()

    dev = LocalSpeakerDevice(output=output, frame_duration=0)
    dev.arm_mixer(source1)

    assert _wait_until(lambda: dev.is_playing(), timeout=1.0), "泵未在預期時間內啟動"
    first_thread = dev._thread

    dev.arm_mixer(source2)  # no-op：_playing 守門
    assert dev._thread is first_thread, "arm_mixer 不得啟第二個泵"
    assert dev.is_playing()

    dev.stop()


# ── Timing helpers ────────────────────────────────────────────────────────────

class _BlockingOutput:
    """每次 write() 內 sleep(frame_duration)，模擬阻塞型 sounddevice OutputStream.write()。"""

    def __init__(self, frame_duration: float) -> None:
        self._frame_duration = frame_duration
        self.frames: list[bytes] = []
        self.closed: bool = False

    def write(self, frame: bytes) -> None:
        time.sleep(self._frame_duration)
        self.frames.append(frame)

    def close(self) -> None:
        self.closed = True


# ── Timing tests ──────────────────────────────────────────────────────────────

def test_pump_no_double_timing_with_blocking_output():
    """阻塞輸出（write 內 sleep frame_duration）播 10 幀，總耗時 < 0.32s；
    現況雙重計時約 0.4s → 此測試應為紅；修成 deadline 計時後 ~0.2s → 綠。"""
    from marvin_voice_core.playback_device import LocalSpeakerDevice

    N = 10
    frame_duration = 0.02
    source = _FakeSource([FRAME] * N)
    output = _BlockingOutput(frame_duration)

    dev = LocalSpeakerDevice(output=output, frame_duration=frame_duration)
    start = time.perf_counter()
    dev.play(source)

    assert dev._thread is not None
    dev._thread.join(timeout=5.0)
    elapsed = time.perf_counter() - start

    assert len(output.frames) == N, f"應播出 {N} 幀，實際 {len(output.frames)}"
    assert elapsed < 0.32, (
        f"雙重計時 bug：{N} 幀阻塞輸出總耗時 {elapsed:.3f}s 超過 0.32s 上界"
    )


def test_pump_realtime_pacing_with_instant_output():
    """即時輸出（write 瞬時）播 10 幀，總耗時落在 real-time 節拍區間 0.15~0.35s；
    驗證 deadline 計時有維持節拍、非全速空轉也非雙睡。"""
    from marvin_voice_core.playback_device import LocalSpeakerDevice

    N = 10
    frame_duration = 0.02
    source = _FakeSource([FRAME] * N)
    output = _FakeOutput()

    dev = LocalSpeakerDevice(output=output, frame_duration=frame_duration)
    start = time.perf_counter()
    dev.play(source)

    assert dev._thread is not None
    dev._thread.join(timeout=5.0)
    elapsed = time.perf_counter() - start

    assert len(output.frames) == N, f"應播出 {N} 幀，實際 {len(output.frames)}"
    assert 0.15 <= elapsed <= 0.35, (
        f"即時輸出 {N} 幀耗時 {elapsed:.3f}s 不在 0.15~0.35s 節拍區間"
    )
