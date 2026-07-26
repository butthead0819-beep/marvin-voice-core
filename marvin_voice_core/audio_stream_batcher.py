"""/audio_stream 送出前合併小 frame：降低封包數對車載 WiFi 時序抖動的敏感度。

StreamSpeakerOutput 逐 20ms frame（3840 bytes）廣播，若照樣逐幀 resp.write() 會變成
每秒 50 次 HTTP chunk，對 ESP32 端 WiFi 時序抖動很敏感。這裡改成累積到門檻位元組數
才送一次，犧牲一點延遲換取封包數大幅下降，不動 mixer/pump 端 20ms 節奏。
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator


async def iter_batched_frames(q: asyncio.Queue, *, min_bytes: int) -> AsyncIterator[bytes]:
    """讀佇列直到湊滿 min_bytes 才 yield 合併後的 chunk；收到 None（上游關閉）把殘餘尾巴
    flush 一次後結束（沒有殘餘就不多 yield 一次空 chunk）。"""
    buf = bytearray()
    while True:
        frame = await q.get()
        if frame is None:
            if buf:
                yield bytes(buf)
            return
        buf += frame
        if len(buf) >= min_bytes:
            yield bytes(buf)
            buf.clear()


async def iter_batched_encoded_frames(q: asyncio.Queue, encoder, *, min_bytes: int) -> AsyncIterator[bytes]:
    """跟 iter_batched_frames 邏輯相同，差別是每個原始 PCM frame 先經過 encoder.encode()
    轉成壓縮 bytes 才累積（見 mp3_stream_encoder.py）；收到 None 時額外呼叫 encoder.flush()
    把 lame 內部殘留、還沒湊滿一個 MP3 frame 的樣本吐乾淨再一起送出。"""
    buf = bytearray()
    while True:
        frame = await q.get()
        if frame is None:
            tail = encoder.flush()
            if tail:
                buf += tail
            if buf:
                yield bytes(buf)
            return
        encoded = encoder.encode(frame)
        if encoded:
            buf += encoded
        if len(buf) >= min_bytes:
            yield bytes(buf)
            buf.clear()
