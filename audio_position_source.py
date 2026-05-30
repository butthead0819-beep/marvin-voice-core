"""PositionTrackingAudioSource — 包一層 discord.AudioSource 數播放位置。

播歌中途插 TTS 的熱切換需要知道 stream1 播到第幾秒，但 discord.py 不給
sample-accurate 位置。voice send thread 每 20ms 呼叫一次 read()，數次數推算。

精度：±幾十 ms（discord/network jitter buffer），對「切換點」夠用——接縫
本來就被 TTS ducking onset + 低音量遮掩，不需 sample 準確。
"""
from __future__ import annotations

import discord

FRAME_MS = 20  # discord voice send thread 每幀固定 20ms


class PositionTrackingAudioSource(discord.AudioSource):
    def __init__(self, wrapped: discord.AudioSource, initial_offset: float = 0.0):
        # initial_offset：此源在歌曲中的起始絕對秒數。熱切換的 stream2 seek 到歌曲
        # target 秒，position 要報「絕對」位置（target + 已播）；否則下次插話的 -ss
        # 會用相對位置倒帶（2026-05-30 迴圈 bug）。
        self._wrapped = wrapped
        self._frames = 0
        self._initial_offset = initial_offset

    def read(self) -> bytes:
        data = self._wrapped.read()
        if data:  # 空 bytes = 串流結束，不計數（避免結束後位置續漂）
            self._frames += 1
        return data

    def is_opus(self) -> bool:
        return self._wrapped.is_opus()

    def cleanup(self) -> None:
        self._wrapped.cleanup()

    @property
    def frames_played(self) -> int:
        return self._frames

    @property
    def position_seconds(self) -> float:
        return self._initial_offset + self._frames * FRAME_MS / 1000.0
