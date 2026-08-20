"""TDD：device/puck_mixer.py buffer 水位追蹤——決定 PREFETCH_SECONDS 該加大還是
減小的依據（2026-08-20，使用者要求「追蹤 RAM 填滿的狀況」）。

驗：
(a) status() 沒連線過時 buffer_fill_pct/buffer_min_fill_pct 都是 None
(b) _loop() 跑起來後，status() 回報目前水位（0~100）
(c) 水位曾經探底（queue.Empty）→ buffer_min_fill_pct 該記到 0
(d) 每次重連（_connect_stream）重置 min，不被上一輪連線的低點污染
(e) _process_rss_kb() 讀不到 /proc（開發機/macOS）回 None，不噴例外
(f) status() 帶 buffer_max_seconds，供人判讀百分比對應幾秒
"""
import queue
import threading
import time
from unittest.mock import MagicMock

import numpy as np
import pytest

from device.puck_mixer import CHANNELS, CHUNK_FRAMES, PREFETCH_CHUNKS, PREFETCH_SECONDS, PuckMixer, _process_rss_kb


class _AliveThread:
    def is_alive(self):
        return True


def test_status_before_any_connection_has_no_buffer_readings():
    mixer = PuckMixer(bt_mac="AA:BB:CC:DD:EE:FF")
    st = mixer.status()
    assert st["buffer_fill_pct"] is None
    assert st["buffer_min_fill_pct"] is None
    assert st["buffer_max_seconds"] == PREFETCH_SECONDS


def test_connect_stream_resets_min_fill_and_exposes_queue(monkeypatch):
    monkeypatch.setattr("device.puck_mixer._make_decoder", lambda url: MagicMock())
    monkeypatch.setattr("device.puck_mixer._reader_loop", lambda proc, q, stop_event: None)

    mixer = PuckMixer(bt_mac="AA:BB:CC:DD:EE:FF")
    mixer._min_fill_frac = 0.42   # 模擬上一輪連線留下的舊水位
    q, reader_thread, reader_stop = mixer._connect_stream()

    assert mixer._queue is q
    assert mixer._min_fill_frac is None   # 新連線，舊水位不該延續


def test_loop_reports_current_fill_and_tracks_minimum(monkeypatch):
    mixer = PuckMixer(bt_mac="AA:BB:CC:DD:EE:FF")
    monkeypatch.setattr(mixer, "_open_pcm", lambda: MagicMock())

    q = queue.Queue(maxsize=PREFETCH_CHUNKS)
    zeros = np.zeros(CHUNK_FRAMES * CHANNELS, dtype=np.int16)
    # 塞 3 個 chunk 進去，讓水位不是 0——之後每讀一個，qsize() 遞減，min 該愈追愈低。
    for _ in range(3):
        q.put(zeros)
    monkeypatch.setattr(mixer, "_connect_stream", lambda: (q, _AliveThread(), threading.Event()))

    t = threading.Thread(target=mixer._loop, daemon=True)
    t.start()
    for _ in range(100):
        if q.empty():
            break
        time.sleep(0.02)
    time.sleep(0.05)   # 讓最後一次 qsize() 採樣（0）真的發生
    mixer._stop_flag.set()
    t.join(timeout=1.0)

    st = mixer.status()
    # 3 個 chunk 陸續被讀走，水位從 (3-1)/max 一路降到 0——min 該是全程最低點。
    assert st["buffer_min_fill_pct"] is not None
    assert st["buffer_min_fill_pct"] < (3.0 / PREFETCH_CHUNKS) * 100.0 + 1.0


def test_process_rss_kb_does_not_raise_when_proc_unavailable(monkeypatch):
    def _boom(*a, **kw):
        raise FileNotFoundError("no /proc on this platform")
    monkeypatch.setattr("builtins.open", _boom)

    assert _process_rss_kb() is None


def test_process_rss_kb_parses_vmrss_line(monkeypatch, tmp_path):
    fake_status = tmp_path / "status"
    fake_status.write_text("Name:\tpython3\nVmRSS:\t   12345 kB\nVmSize:\t 99999 kB\n")

    import builtins
    real_open = builtins.open

    def _fake_open(path, *a, **kw):
        if path == "/proc/self/status":
            return real_open(fake_status, *a, **kw)
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", _fake_open)

    assert _process_rss_kb() == 12345
