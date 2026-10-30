"""WakeStreamDetector 多格式輸入支援 — 16k mono puck 路徑不可影響 Discord 48k stereo 路徑。

Discord 路徑（預設參數）必須完全不變；16k mono 路徑是新增的建構參數。
"""
from __future__ import annotations

import pytest

from wake_stream_detector import _STRIDE_BYTES, _WINDOW_BYTES, WakeStreamDetector


class _FakeLoop:
    def __init__(self):
        self.created = []

    def create_task(self, coro):
        self.created.append(coro)
        coro.close()  # 避免 "coroutine was never awaited" 警告


def test_default_params_match_discord_constants():
    wake = WakeStreamDetector(whisper_model=None, on_wake_callback=None, loop=_FakeLoop())
    # 0.5s × 48000 × 2ch × 2B；跟模組常數一致＝Discord 路徑不變
    assert wake._window_bytes == 96000 == _WINDOW_BYTES
    assert wake._stride_bytes == 48000 == _STRIDE_BYTES


def test_16k_mono_params():
    wake = WakeStreamDetector(
        whisper_model=None, on_wake_callback=None, loop=_FakeLoop(),
        input_rate=16000, input_channels=1,
    )
    assert wake._window_bytes == 16000
    assert wake._stride_bytes == 8000


def test_non_multiple_of_16000_raises():
    with pytest.raises(ValueError):
        WakeStreamDetector(
            whisper_model=None, on_wake_callback=None, loop=_FakeLoop(),
            input_rate=22050, input_channels=1,
        )


def test_16k_mono_push_pcm_creates_task_at_window_size():
    loop = _FakeLoop()
    wake = WakeStreamDetector(
        whisper_model=None, on_wake_callback=None, loop=loop,
        input_rate=16000, input_channels=1,
    )
    wake.on_speech_start(user_id=1, first_audio_time=0.0, pre_roll=b"")

    # window_bytes = 16000（500ms @16k mono，16-bit）
    wake.push_pcm(1, b"\x00\x00" * 7999)
    assert len(loop.created) == 0

    wake.push_pcm(1, b"\x00\x00")  # 補到剛好 16000 bytes
    assert len(loop.created) == 1
