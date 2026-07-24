"""
tests/test_audio_stream_batcher.py

TDD：/audio_stream 送出前把多個小 frame 合併成大塊，減少封包數（降低車載 WiFi 對
逐封包時序抖動的敏感度）。純函式測試，不碰 aiohttp/StreamResponse。

驗：
(a) 累積位元組數達 min_bytes 才 yield 一次合併後的 chunk
(b) 上游 close（佇列吐出 None）→ 把殘餘不足 min_bytes 的尾巴 flush 一次後結束
(c) 全部 frame 加總仍不足 min_bytes → 只在收到 None 時 yield 一次
(d) 依序保留所有 bytes（合併不遺漏/不重排）
"""
from __future__ import annotations

import asyncio

import pytest

from marvin_voice_core.audio_stream_batcher import iter_batched_frames


async def _queue_of(frames):
    q: asyncio.Queue = asyncio.Queue()
    for f in frames:
        q.put_nowait(f)
    return q


@pytest.mark.asyncio
async def test_batches_until_min_bytes_reached():
    q = await _queue_of([b"a" * 100, b"b" * 100, b"c" * 100, b"d" * 100, None])
    chunks = [c async for c in iter_batched_frames(q, min_bytes=250)]
    assert chunks == [b"a" * 100 + b"b" * 100 + b"c" * 100, b"d" * 100]


@pytest.mark.asyncio
async def test_flushes_remainder_on_close():
    q = await _queue_of([b"a" * 50, None])
    chunks = [c async for c in iter_batched_frames(q, min_bytes=250)]
    assert chunks == [b"a" * 50]


@pytest.mark.asyncio
async def test_no_frames_before_close_yields_nothing():
    q = await _queue_of([None])
    chunks = [c async for c in iter_batched_frames(q, min_bytes=250)]
    assert chunks == []


@pytest.mark.asyncio
async def test_preserves_byte_order():
    q = await _queue_of([b"\x01\x02", b"\x03\x04", b"\x05\x06", None])
    chunks = [c async for c in iter_batched_frames(q, min_bytes=1000)]
    assert b"".join(chunks) == b"\x01\x02\x03\x04\x05\x06"
