"""car puck mk2 刷碟 SFX：Pi edge 端用 queue_next() 讀到的下一首 peek PCM 本地合成
scratch 音效，crossfade() 點火時疊進 A/B mix（見 project_car_puck_mk2 記憶 + 對照
Mac 端 cogs/music_cog.py _synthesize_dynamic_scratch 的等效邏輯）。"""
import numpy as np
import pytest

from device.puck_mixer import CHANNELS, CHUNK_FRAMES, PuckMixer, _mix_scratch, _read_chunk_deck


def _stereo_chunk(n_frames: int, value: int) -> np.ndarray:
    return np.full(n_frames * CHANNELS, value, dtype=np.int16)


class _FakeStdout:
    def __init__(self, data: bytes):
        self._data = data
        self._pos = 0

    def read(self, n: int) -> bytes:
        chunk = self._data[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk


class _FakeProc:
    def __init__(self, n_frames: int, value: int):
        arr = _stereo_chunk(n_frames, value)
        self.stdout = _FakeStdout(arr.tobytes())


# ---- _mix_scratch ----

def test_mix_scratch_none_returns_unchanged():
    mixed = _stereo_chunk(CHUNK_FRAMES, 100)
    out, pos = _mix_scratch(mixed, None, 0, 0.1)
    assert np.array_equal(out, mixed)
    assert pos is None


def test_mix_scratch_overlays_with_gain():
    mixed = np.zeros(CHUNK_FRAMES * CHANNELS, dtype=np.int16)
    scratch = np.full(CHUNK_FRAMES * CHANNELS, 1000, dtype=np.int16)
    out, pos = _mix_scratch(mixed, scratch, 0, 0.1)
    assert np.all(out == 100)  # 1000 * 0.1
    assert pos == CHUNK_FRAMES * CHANNELS  # 呼叫端會拿這個 pos 跟 len(scratch) 比對判斷播完


def test_mix_scratch_advances_position_and_reports_not_done():
    mixed = np.zeros(CHUNK_FRAMES * CHANNELS, dtype=np.int16)
    scratch = np.full(CHUNK_FRAMES * CHANNELS * 3, 500, dtype=np.int16)
    out, pos = _mix_scratch(mixed, scratch, 0, 1.0)
    assert pos == CHUNK_FRAMES * CHANNELS
    assert np.all(out == 500)


def test_mix_scratch_pads_final_partial_chunk():
    mixed = np.zeros(CHUNK_FRAMES * CHANNELS, dtype=np.int16)
    tail_len = 10
    scratch = np.full(tail_len, 800, dtype=np.int16)
    out, pos = _mix_scratch(mixed, scratch, 0, 1.0)
    assert pos >= len(scratch)  # 播完了，呼叫端該清 armed 狀態
    assert np.all(out[:tail_len] == 800)
    assert np.all(out[tail_len:] == 0)


# ---- _read_chunk_deck ----

def test_read_chunk_deck_drains_peek_buf_before_proc():
    proc_frames = _stereo_chunk(CHUNK_FRAMES, 999)  # 不該被讀到
    deck = {"proc": _FakeProc(CHUNK_FRAMES, 999), "peek_buf": _stereo_chunk(CHUNK_FRAMES, 1)}
    out = _read_chunk_deck(deck)
    assert np.all(out == 1)
    assert deck["peek_buf"] is None or len(deck["peek_buf"]) == 0


def test_read_chunk_deck_falls_back_to_proc_when_peek_empty():
    deck = {"proc": _FakeProc(CHUNK_FRAMES, 7), "peek_buf": None}
    out = _read_chunk_deck(deck)
    assert np.all(out == 7)


def test_read_chunk_deck_combines_short_peek_with_proc_read():
    peek_samples = 10 * CHANNELS  # 10 frames，不足一個 chunk
    deck = {
        "proc": _FakeProc(CHUNK_FRAMES, 2),
        "peek_buf": _stereo_chunk(10, 1),
    }
    out = _read_chunk_deck(deck)
    assert len(out) == CHUNK_FRAMES * CHANNELS
    assert np.all(out[:peek_samples] == 1)
    assert np.all(out[peek_samples:] == 2)
    assert deck["peek_buf"] is None


# ---- crossfade() 點火時武裝 scratch ----

def test_crossfade_arms_scratch_when_deck_b_has_scratch_pcm():
    mixer = PuckMixer(bt_mac="AA:BB:CC:DD:EE:FF")
    scratch = np.full(1000, 500, dtype=np.int16)
    mixer._deck_b = {"url": "u", "proc": None, "peek_buf": None, "scratch_pcm": scratch}
    mixer.crossfade(duration_s=4.0)
    assert mixer._scratch_samples is not None
    assert np.array_equal(mixer._scratch_samples, scratch)
    assert mixer._scratch_pos == 0


def test_crossfade_no_scratch_when_deck_b_has_none():
    mixer = PuckMixer(bt_mac="AA:BB:CC:DD:EE:FF")
    mixer._deck_b = {"url": "u", "proc": None, "peek_buf": None, "scratch_pcm": None}
    mixer.crossfade(duration_s=4.0)
    assert mixer._scratch_samples is None


def test_crossfade_raises_without_deck_b():
    mixer = PuckMixer(bt_mac="AA:BB:CC:DD:EE:FF")
    with pytest.raises(RuntimeError):
        mixer.crossfade()
