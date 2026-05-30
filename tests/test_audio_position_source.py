"""PositionTrackingAudioSource — 數 read() 推算播放位置（熱切換游標）。

discord.py 不給 sample-accurate 播放位置；voice send thread 每 20ms 呼叫
一次 read()，數次數即可推算 stream1 播到第幾秒，作為中途插 TTS 的切換點。
"""
from __future__ import annotations

import discord

from audio_position_source import FRAME_MS, PositionTrackingAudioSource


class _FakeSource(discord.AudioSource):
    """測試用假音源：read() 依序吐 chunks，吐完回 b''（串流結束）。"""
    def __init__(self, chunks, opus=False):
        self._chunks = list(chunks)
        self._opus = opus
        self.cleaned = False

    def read(self):
        return self._chunks.pop(0) if self._chunks else b""

    def is_opus(self):
        return self._opus

    def cleanup(self):
        self.cleaned = True


def test_read_forwards_wrapped_data():
    src = PositionTrackingAudioSource(_FakeSource([b"aaa", b"bbb"]))
    assert src.read() == b"aaa"
    assert src.read() == b"bbb"


def test_read_counts_each_nonempty_frame():
    src = PositionTrackingAudioSource(_FakeSource([b"x"] * 5))
    for _ in range(5):
        src.read()
    assert src.frames_played == 5


def test_empty_read_does_not_count():
    """空 bytes = 串流結束，不該計入位置（避免結束後位置繼續漂）。"""
    src = PositionTrackingAudioSource(_FakeSource([b"x", b"x"]))
    src.read(); src.read()       # 2 幀
    src.read(); src.read()       # 2 個 b''（結束）
    assert src.frames_played == 2


def test_position_seconds_is_frames_times_20ms():
    src = PositionTrackingAudioSource(_FakeSource([b"x"] * 50))
    for _ in range(50):
        src.read()
    # 50 幀 × 20ms = 1.0s
    assert src.position_seconds == 50 * FRAME_MS / 1000.0
    assert src.position_seconds == 1.0


def test_position_starts_at_zero():
    src = PositionTrackingAudioSource(_FakeSource([b"x"]))
    assert src.position_seconds == 0.0


def test_is_opus_forwarded():
    assert PositionTrackingAudioSource(_FakeSource([], opus=True)).is_opus() is True
    assert PositionTrackingAudioSource(_FakeSource([], opus=False)).is_opus() is False


def test_cleanup_forwarded():
    inner = _FakeSource([])
    PositionTrackingAudioSource(inner).cleanup()
    assert inner.cleaned is True


def test_is_discord_audiosource_subclass():
    """discord.py vc.play() 內部 isinstance(source, AudioSource) 檢查，必須是子類。"""
    src = PositionTrackingAudioSource(_FakeSource([]))
    assert isinstance(src, discord.AudioSource)
