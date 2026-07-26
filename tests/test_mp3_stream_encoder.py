"""
tests/test_mp3_stream_encoder.py

TDD：Mp3StreamEncoder——/audio_stream 車載端點用的即時 MP3 編碼包裝（lameenc 包裝）。

驗：
(a) 餵足夠長度的 PCM（真 sine wave，非任意 bytes）→ encode()+flush() 合併後開頭是
    MP3 frame sync（0xFF 後接 3 個 1 bit，即 byte[1] & 0xE0 == 0xE0）
(b) 壓縮率：128kbps 輸出遠小於原始 PCM bytes 數（驗證真的有壓縮，不是原樣回傳）
(c) 空 bytes 輸入 → 回傳空 bytes，不呼叫底層 encoder 噴例外
(d) mono（channels=1）設定也能正常編碼吐出有效 MP3 sync
(e) flush() 在只餵過少量樣本（不足一個 MP3 frame）時，仍能吐出殘留資料而不是空
"""
from __future__ import annotations

import math
import struct

from marvin_voice_core.mp3_stream_encoder import Mp3StreamEncoder


def _sine_pcm(*, rate: int, channels: int, seconds: float, freq: float = 440.0) -> bytes:
    n = int(rate * seconds)
    samples = []
    for i in range(n):
        v = int(3000 * math.sin(2 * math.pi * freq * i / rate))
        samples.extend([v] * channels)
    return struct.pack("<%dh" % len(samples), *samples)


def test_encode_and_flush_produce_valid_mp3_frame_sync():
    enc = Mp3StreamEncoder(rate=48000, channels=2, bitrate_kbps=128)
    pcm = _sine_pcm(rate=48000, channels=2, seconds=0.2)

    out = enc.encode(pcm) + enc.flush()

    assert len(out) > 0
    assert out[0] == 0xFF and (out[1] & 0xE0) == 0xE0, "輸出開頭不是MP3 frame sync"


def test_encode_significantly_reduces_size():
    enc = Mp3StreamEncoder(rate=48000, channels=2, bitrate_kbps=128)
    pcm = _sine_pcm(rate=48000, channels=2, seconds=0.5)

    out = enc.encode(pcm) + enc.flush()

    assert len(out) < len(pcm) / 5, "128kbps輸出應該遠小於原始PCM(約1/12)，沒壓縮到"


def test_empty_input_returns_empty_bytes():
    enc = Mp3StreamEncoder(rate=48000, channels=2)

    assert enc.encode(b"") == b""


def test_mono_channel_configuration_encodes_successfully():
    enc = Mp3StreamEncoder(rate=48000, channels=1, bitrate_kbps=64)
    pcm = _sine_pcm(rate=48000, channels=1, seconds=0.2)

    out = enc.encode(pcm) + enc.flush()

    assert len(out) > 0
    assert out[0] == 0xFF and (out[1] & 0xE0) == 0xE0


def test_flush_drains_leftover_samples_shorter_than_one_frame():
    enc = Mp3StreamEncoder(rate=48000, channels=2, bitrate_kbps=128)
    # 故意餵遠不足一個 1152-sample MP3 frame 的量
    pcm = _sine_pcm(rate=48000, channels=2, seconds=0.005)

    mid = enc.encode(pcm)
    tail = enc.flush()

    assert len(mid) + len(tail) > 0, "太短的輸入flush()後應該還是能吐出殘留資料"
