"""PuckVADStream 連續音訊切句器 — 用假 VAD 驗證狀態機，不載 ONNX。

假 VAD：process() 直接吃預先排好的機率佇列，每收到一個 512-sample
window 回傳佇列裡的下一個值；驗證邏輯本身不依賴真的 Silero 推理。
"""
from __future__ import annotations

import numpy as np

from puck_vad_stream import PuckVADConfig, PuckVADStream, WINDOW_SAMPLES


class FakeVAD:
    """process() 吃任意長度 PCM，依 512-sample window 切，回傳對應機率。"""

    def __init__(self, probs):
        self._probs = list(probs)
        self._idx = 0

    def process(self, pcm16: np.ndarray) -> list[float]:
        n_windows = len(pcm16) // WINDOW_SAMPLES
        out = []
        for _ in range(n_windows):
            out.append(self._probs[self._idx])
            self._idx += 1
        return out


def _window(value=100) -> np.ndarray:
    return np.full(WINDOW_SAMPLES, value, dtype=np.int16)


def _feed_n_windows(stream: PuckVADStream, n: int) -> None:
    stream.feed(np.concatenate([_window() for _ in range(n)]))


def test_seven_high_windows_then_drop_no_start():
    starts = []
    probs = [0.9] * 7 + [0.1]
    vad = FakeVAD(probs)
    stream = PuckVADStream(vad, on_speech_start=lambda pre_roll: starts.append(pre_roll))
    _feed_n_windows(stream, len(probs))
    assert starts == []


def test_eight_high_windows_triggers_start_with_correct_pre_roll_len():
    starts = []
    # 前面先餵 5 個 IDLE 低機率 window，再餵 8 個高機率觸發
    probs = [0.1] * 5 + [0.9] * 8
    vad = FakeVAD(probs)
    stream = PuckVADStream(vad, on_speech_start=lambda pre_roll: starts.append(pre_roll))
    _feed_n_windows(stream, len(probs))

    assert len(starts) == 1
    # pre_roll = min(5, pre_roll_windows=13) 個 IDLE window + 8 個候選 window
    expected_windows = min(5, 13) + 8
    assert len(starts[0]) == expected_windows * WINDOW_SAMPLES * 2  # int16 = 2 bytes


def test_speaking_21_silence_no_cut_22_cuts():
    ends = []
    probs = [0.9] * 8 + [0.1] * 21
    vad = FakeVAD(probs)
    stream = PuckVADStream(vad, on_speech_end=lambda seg, reason: ends.append((seg, reason)))
    _feed_n_windows(stream, len(probs))
    assert ends == []

    # 補第 22 個靜音 window
    stream.vad = FakeVAD([0.1])
    _feed_n_windows(stream, 1)

    assert len(ends) == 1
    _segment_pcm, reason = ends[0]
    assert reason == "silence"


def test_speech_end_segment_length_correct():
    ends = []
    probs = [0.9] * 8 + [0.1] * 22
    vad = FakeVAD(probs)
    stream = PuckVADStream(vad, on_speech_end=lambda seg, reason: ends.append((seg, reason)))
    _feed_n_windows(stream, len(probs))

    assert len(ends) == 1
    segment_pcm, reason = ends[0]
    assert reason == "silence"
    # pre_roll bundle = 8 個候選 window，之後講話中每個 window(含靜音) append 一筆 → 22 個
    expected_samples = (8 + 22) * WINDOW_SAMPLES
    assert len(segment_pcm) == expected_samples * 2


def test_mid_range_prob_does_not_accumulate_silence():
    ends = []
    # 講話中混入 0.4（介於 end_prob=0.35 與 start_prob=0.5 之間）→ 不累積靜音
    probs = [0.9] * 8 + [0.1] * 10 + [0.4] + [0.1] * 21
    vad = FakeVAD(probs)
    stream = PuckVADStream(vad, on_speech_end=lambda seg, reason: ends.append((seg, reason)))
    _feed_n_windows(stream, len(probs))
    # 0.4 重置了靜音計數，所以到結尾只累積 21 個靜音，尚未達 22 → 不切
    assert ends == []

    vad2 = FakeVAD([0.1])
    stream.vad = vad2
    _feed_n_windows(stream, 1)
    assert len(ends) == 1
    assert ends[0][1] == "silence"


def test_max_len_force_cuts():
    cfg = PuckVADConfig(max_speech_windows=10)
    ends = []
    probs = [0.9] * 8 + [0.9] * 10  # 8 觸發開始，之後 10 個講話 window 達上限
    vad = FakeVAD(probs)
    stream = PuckVADStream(vad, config=cfg, on_speech_end=lambda seg, reason: ends.append((seg, reason)))
    _feed_n_windows(stream, len(probs))

    assert len(ends) == 1
    assert ends[0][1] == "max_len"


def test_set_suppressed_during_speech_ends_immediately():
    ends = []
    starts = []
    probs = [0.9] * 8 + [0.9] * 5
    vad = FakeVAD(probs)
    stream = PuckVADStream(
        vad,
        on_speech_start=lambda pre_roll: starts.append(pre_roll),
        on_speech_end=lambda seg, reason: ends.append((seg, reason)),
    )
    _feed_n_windows(stream, len(probs))
    assert len(starts) == 1
    assert ends == []

    stream.set_suppressed(True)
    assert len(ends) == 1
    assert ends[0][1] == "suppressed"


def test_high_prob_during_suppression_does_not_trigger_start():
    starts = []
    probs = [0.9] * 20
    vad = FakeVAD(probs)
    stream = PuckVADStream(vad, on_speech_start=lambda pre_roll: starts.append(pre_roll))
    stream.set_suppressed(True)
    _feed_n_windows(stream, len(probs))
    assert starts == []


def test_unsuppress_allows_trigger_again():
    starts = []
    probs = [0.9] * 20
    vad = FakeVAD(probs)
    stream = PuckVADStream(vad, on_speech_start=lambda pre_roll: starts.append(pre_roll))

    stream.set_suppressed(True)
    stream.set_suppressed(False)
    _feed_n_windows(stream, len(probs))
    assert len(starts) == 1


def test_stats_counts_correct():
    probs = [0.9] * 8 + [0.1] * 22
    vad = FakeVAD(probs)
    stream = PuckVADStream(vad)
    _feed_n_windows(stream, len(probs))

    stats = stream.stats
    assert stats["windows"] == len(probs)
    assert stats["speech_windows"] == 22  # SPEAKING 狀態下的 window 數（含靜音段）
    assert stats["segments"] == 1


def test_unaligned_feed_lengths_match_full_feed():
    probs = [0.9] * 8 + [0.1] * 22

    def run_with_feed_sizes(sizes):
        vad = FakeVAD(probs)
        events = []
        stream = PuckVADStream(
            vad,
            on_speech_start=lambda pre_roll: events.append(("start", len(pre_roll))),
            on_speech_end=lambda seg, reason: events.append(("end", reason, len(seg))),
        )
        total = sum(sizes)
        windows = [_window() for _ in range(len(probs))]
        all_samples = np.concatenate(windows)
        assert total == len(all_samples)
        offset = 0
        for size in sizes:
            stream.feed(all_samples[offset:offset + size])
            offset += size
        return events

    n_total_samples = len(probs) * WINDOW_SAMPLES
    full = run_with_feed_sizes([n_total_samples])

    # 300, 700 不對齊 512 的切法餵完同一組資料
    chunk_sizes = []
    remaining = n_total_samples
    pattern = [300, 700]
    i = 0
    while remaining > 0:
        size = min(pattern[i % 2], remaining)
        chunk_sizes.append(size)
        remaining -= size
        i += 1

    chunked = run_with_feed_sizes(chunk_sizes)

    assert full == chunked
