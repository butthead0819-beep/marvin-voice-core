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

from marvin_voice_core.audio_stream_batcher import iter_batched_encoded_frames, iter_batched_frames


async def _queue_of(frames):
    q: asyncio.Queue = asyncio.Queue()
    for f in frames:
        q.put_nowait(f)
    return q


class _FakeEncoder:
    """假 encoder：encode() 原樣回傳輸入（方便驗證累積/flush 邏輯，不牽涉真的 lame）。"""

    def __init__(self, flush_tail: bytes = b""):
        self._flush_tail = flush_tail
        self.flushed = False

    def encode(self, frame: bytes) -> bytes:
        return frame

    def flush(self) -> bytes:
        self.flushed = True
        return self._flush_tail


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


@pytest.mark.asyncio
async def test_encoded_batches_pass_frames_through_encoder_before_accumulating():
    q = await _queue_of([b"a" * 100, b"b" * 100, b"c" * 100, None])
    enc = _FakeEncoder()
    chunks = [c async for c in iter_batched_encoded_frames(q, enc, min_bytes=250)]
    assert b"".join(chunks) == b"a" * 100 + b"b" * 100 + b"c" * 100


@pytest.mark.asyncio
async def test_encoded_flush_tail_appended_on_close():
    q = await _queue_of([b"a" * 50, None])
    enc = _FakeEncoder(flush_tail=b"TAIL")
    chunks = [c async for c in iter_batched_encoded_frames(q, enc, min_bytes=250)]
    assert chunks == [b"a" * 50 + b"TAIL"]
    assert enc.flushed


@pytest.mark.asyncio
async def test_encoded_flush_with_no_leftover_still_yields_tail_only():
    q = await _queue_of([None])
    enc = _FakeEncoder(flush_tail=b"TAIL")
    chunks = [c async for c in iter_batched_encoded_frames(q, enc, min_bytes=250)]
    assert chunks == [b"TAIL"]


@pytest.mark.asyncio
async def test_encoded_no_flush_tail_and_no_leftover_yields_nothing():
    q = await _queue_of([None])
    enc = _FakeEncoder(flush_tail=b"")
    chunks = [c async for c in iter_batched_encoded_frames(q, enc, min_bytes=250)]
    assert chunks == []


@pytest.mark.asyncio
async def test_encoded_empty_encode_output_does_not_trigger_early_yield():
    """encode() 回傳空 bytes（lame 內部樣本還沒湊滿一個 frame）時不該誤判成該 yield。"""
    q = await _queue_of([b"", b"", b"x" * 300, None])
    enc = _FakeEncoder()
    chunks = [c async for c in iter_batched_encoded_frames(q, enc, min_bytes=250)]
    assert chunks == [b"x" * 300]
