"""tests/test_local_speaker_device.py

TDD：LocalSpeakerDevice 泵行為 + PlaybackDevice Protocol 滿足。
注入假 source + 假 output，無任何硬體依賴。

先寫測試（全紅），再寫實作（全綠）。
"""
from __future__ import annotations

import threading
import time
from unittest import mock


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


class _IdleThenFrames:
    """前段回 b""(idle)、接著回真幀、之後永遠 idle——模擬 mixer arm 時 TTS 還沒 push。"""

    def __init__(self) -> None:
        self._seq = [b"", b"", b"", FRAME, FRAME]
        self._i = 0

    def read(self) -> bytes:
        if self._i < len(self._seq):
            f = self._seq[self._i]
            self._i += 1
            return f
        return b""  # 之後永遠 idle


def test_arm_mixer_persistent_survives_idle_empty_reads():
    """arm_mixer(persistent)：source 前段回 b""（mixer arm 時 TTS 還沒 push）不讓泵退出——
    持位置寫靜音、之後真幀仍播出、is_playing 維持 True，只有 stop() 才中止。
    修 on-demand mixer idle b"" 殺泵 → re-arm race 丟 TTS → 本機永久沉默 的根因。"""
    from marvin_voice_core.playback_device import LocalSpeakerDevice

    src = _IdleThenFrames()
    out = _FakeOutput()
    dev = LocalSpeakerDevice(output=out, frame_duration=0.001)
    dev.arm_mixer(src)  # persistent=True

    assert _wait_until(lambda: FRAME in out.frames), "persistent 泵應在 idle b\"\" 後仍播出真幀"
    assert dev.is_playing() is True, "idle b\"\" 不該讓 persistent 泵退出"
    assert not out.closed, "persistent 泵運行中 output 不該被關閉"

    dev.stop()
    assert dev.is_playing() is False
    assert out.closed


def test_arm_mixer_honors_persistent_false_and_exits_when_idle():
    """PTT 最小化：LocalSpeakerDevice(persistent=False) 時 arm_mixer 起的泵在 source
    耗盡（b""）後退出，不常駐空轉——瀏覽器衛星 idle CPU→0（Pi 預設 persistent=True 不變）。"""
    from marvin_voice_core.playback_device import LocalSpeakerDevice

    src = _FakeSource([FRAME, FRAME])   # 兩幀後回 b""
    out = _FakeOutput()
    dev = LocalSpeakerDevice(output=out, frame_duration=0, persistent=False)
    dev.arm_mixer(src)

    assert dev._thread is not None
    dev._thread.join(timeout=2.0)
    assert dev.is_playing() is False, "persistent=False：source 耗盡後泵應退出"
    assert out.closed
    assert out.frames == [FRAME, FRAME]


def test_arm_mixer_default_persistent_true_survives_idle():
    """回歸：不傳 persistent＝預設 True（Pi 常駐喇叭行為不變）。"""
    from marvin_voice_core.playback_device import LocalSpeakerDevice

    src = _IdleThenFrames()
    out = _FakeOutput()
    dev = LocalSpeakerDevice(output=out, frame_duration=0.001)   # 預設 persistent=True
    dev.arm_mixer(src)
    assert _wait_until(lambda: FRAME in out.frames)
    assert dev.is_playing() is True, "預設仍 persistent，idle b\"\" 不退出"
    dev.stop()


def test_non_persistent_play_still_exits_on_empty_read():
    """對照：一般 play()（非 persistent）遇 b"" 仍照舊耗盡退出（不回歸）。"""
    from marvin_voice_core.playback_device import LocalSpeakerDevice

    src = _FakeSource([FRAME, FRAME])
    out = _FakeOutput()
    dev = LocalSpeakerDevice(output=out, frame_duration=0)
    dev.play(src)  # persistent 預設 False

    assert dev._thread is not None
    dev._thread.join(timeout=2.0)
    assert dev.is_playing() is False
    assert out.closed
    assert out.frames == [FRAME, FRAME]


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
    """arm_mixer(source) 委派 play(persistent) 啟動泵：真幀依序寫出，之後 source 耗盡回 b""
    也**不退出**（on-demand mixer 語意，等下一段），只有 stop() 才中止。"""
    from marvin_voice_core.playback_device import LocalSpeakerDevice

    N = 4
    source = _FakeSource([FRAME] * N)
    output = _FakeOutput()

    dev = LocalSpeakerDevice(output=output, frame_duration=0.001)
    dev.arm_mixer(source)

    assert _wait_until(lambda: output.frames[:N] == [FRAME] * N), "真幀應依序寫出"
    assert dev.is_playing() is True, "persistent 泵不因 source 耗盡而退出"

    dev.stop()
    assert not dev.is_playing()
    assert output.closed


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

class _SleepSpyTime:
    """替換 playback_device 命名空間裡的 time：perf_counter 走真的，sleep 只記錄
    「被要求睡多久」、不真睡。

    測 _pump 主動要求的 sleep 量、而非牆鐘耗時 —— 前者對 CI runner 負載免疫：
    排程抖動只會讓 perf_counter 讀到更晚 → remaining 更負 → 要求的 sleep 更少，
    絕不會誤判成 regression；而雙重計時 bug（write 已阻塞一個 frame、_pump 又睡
    一個）是結構性的，每輪照樣多要求睡 ~frame_duration，跟負載無關。
    """

    def __init__(self) -> None:
        self.requested: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.requested.append(seconds)

    def perf_counter(self) -> float:
        return time.perf_counter()


def test_pump_no_double_timing_with_blocking_output():
    """阻塞輸出（write 內 sleep frame_duration）下，_pump 不得在 write 已耗掉一個
    frame 之後又睡一個 frame（雙重計時 bug → 總耗時逼近正確的兩倍）。

    舊版拿牆鐘耗時比預算，在共用 CI runner 上 flaky（排程抖動可讓 excess 爆表）。
    改成攔截 _pump 要求的 sleep：write 已阻塞滿一個 frame_duration → 每輪 deadline
    早就到，正確的 _pump 一律不會再要求 sleep（total == 0）；雙重計時 bug 則會讓
    要求的 sleep 總量逼近 N×frame_duration。此判斷完全不受 runner 負載影響。"""
    from marvin_voice_core import playback_device

    N = 10
    frame_duration = 0.02

    spy = _SleepSpyTime()
    output = _BlockingOutput(frame_duration)  # write 走 test 模組自己的 time，真的阻塞
    dev = playback_device.LocalSpeakerDevice(output=output, frame_duration=frame_duration)

    with mock.patch.object(playback_device, "time", spy):
        dev.play(_FakeSource([FRAME] * N))
        assert dev._thread is not None
        dev._thread.join(timeout=5.0)

    assert not dev._thread.is_alive(), "泵執行緒未在 5s 內結束"
    assert len(output.frames) == N, f"應播出 {N} 幀，實際 {len(output.frames)}"

    total_requested = sum(s for s in spy.requested if s > 0)
    assert total_requested < N * frame_duration * 0.5, (
        f"_pump 在阻塞式 write 已耗滿 frame 後仍要求 sleep 共 {total_requested:.4f}s"
        f"（各輪：{[round(s, 4) for s in spy.requested]}）— 疑似雙重計時 regression"
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
