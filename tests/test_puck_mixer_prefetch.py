"""car puck mk2 prefetch buffer：把 ffmpeg 解碼跟 PCM 播放迴圈解耦，網路/解碼卡頓
不該直接讓 _loop() 的即時寫入被卡住（2026-08-17 BMW 實機「1秒斷續+追趕」根因，
見 project_car_puck_mk2_pi_zero2w_bt_mixer_validated 記憶）。"""
import queue
import threading
import time

import numpy as np

import device.puck_mixer as puck_mixer
from device.puck_mixer import CHANNELS, CHUNK_FRAMES, PREFETCH_CHUNKS, _read_chunk_deck, _reader_loop


def _stereo_chunk(n_frames: int, value: int) -> np.ndarray:
    return np.full(n_frames * CHANNELS, value, dtype=np.int16)


# ---- _read_chunk_deck 走 queue（有 queue 就不該碰 proc） ----

def test_read_chunk_deck_prefers_queue_over_proc():
    q = queue.Queue()
    q.put(_stereo_chunk(CHUNK_FRAMES, 5))
    deck = {"proc": None, "peek_buf": None, "queue": q}  # proc=None，碰了就炸
    out = _read_chunk_deck(deck)
    assert np.all(out == 5)


def test_read_chunk_deck_queue_empty_returns_silence_without_hanging(monkeypatch):
    monkeypatch.setattr(puck_mixer, "_QUEUE_GET_TIMEOUT_S", 0.02)
    deck = {"proc": None, "peek_buf": None, "queue": queue.Queue()}
    start = time.monotonic()
    out = _read_chunk_deck(deck)
    elapsed = time.monotonic() - start
    assert np.all(out == 0)
    assert len(out) == CHUNK_FRAMES * CHANNELS
    assert elapsed < 0.5  # 有界，不會卡死播放迴圈


def test_read_chunk_deck_still_drains_peek_buf_first_when_queue_present():
    q = queue.Queue()
    q.put(_stereo_chunk(CHUNK_FRAMES, 999))  # 不該被讀到
    deck = {"proc": None, "peek_buf": _stereo_chunk(CHUNK_FRAMES, 1), "queue": q}
    out = _read_chunk_deck(deck)
    assert np.all(out == 1)


# ---- _reader_loop：背景 thread 把 proc 讀出的 chunk 塞進 queue，直到被喊停 ----

class _FakeStdout:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def read(self, n: int) -> bytes:
        if not self._chunks:
            return b""
        return self._chunks.pop(0).tobytes()


class _FakeProc:
    def __init__(self, chunks):
        self.stdout = _FakeStdout(chunks)


def test_reader_loop_fills_queue_from_proc():
    chunks = [_stereo_chunk(CHUNK_FRAMES, 1), _stereo_chunk(CHUNK_FRAMES, 2)]
    proc = _FakeProc(chunks)
    q = queue.Queue(maxsize=PREFETCH_CHUNKS)
    stop_event = threading.Event()

    t = threading.Thread(target=_reader_loop, args=(proc, q, stop_event), daemon=True)
    t.start()
    first = q.get(timeout=1.0)
    second = q.get(timeout=1.0)
    stop_event.set()
    t.join(timeout=1.0)

    assert np.all(first == 1)
    assert np.all(second == 2)


def test_reader_loop_stops_on_event_without_reading_more():
    proc = _FakeProc([_stereo_chunk(CHUNK_FRAMES, 1)] * 5)
    q = queue.Queue(maxsize=1)  # 容量 1，逼 reader 很快就要 block 在 put()
    stop_event = threading.Event()

    t = threading.Thread(target=_reader_loop, args=(proc, q, stop_event), daemon=True)
    t.start()
    time.sleep(0.05)
    stop_event.set()
    t.join(timeout=1.0)

    assert not t.is_alive()


def test_reader_loop_exits_quietly_on_read_exception():
    class _BoomStdout:
        def read(self, n):
            raise OSError("broken pipe")

    class _BoomProc:
        stdout = _BoomStdout()

    q = queue.Queue()
    stop_event = threading.Event()
    t = threading.Thread(target=_reader_loop, args=(_BoomProc(), q, stop_event), daemon=True)
    t.start()
    t.join(timeout=1.0)

    assert not t.is_alive()
    assert q.empty()
