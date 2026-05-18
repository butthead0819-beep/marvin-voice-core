"""TDD：LatencyTracker.LatencyMarks — wake/llm/sentence/audio 分階段時間 ms 計算。"""
from __future__ import annotations

import pytest

from latency_tracker import LatencyMarks


def test_mark_wake_resets_state_and_records_speaker():
    m = LatencyMarks()
    m.mark_wake("Alice", now=100.0)
    assert m.speaker == "Alice"
    assert m.wake_ts == 100.0
    assert m.llm_start_ts is None
    assert m.first_sentence_ts is None


def test_mark_llm_start_records_when_wake_set():
    m = LatencyMarks()
    m.mark_wake("Alice", now=100.0)
    m.mark_llm_start(now=100.5)
    assert m.llm_start_ts == 100.5


def test_mark_llm_start_silently_skips_when_no_wake():
    m = LatencyMarks()
    m.mark_llm_start(now=100.5)
    assert m.llm_start_ts is None


def test_mark_first_sentence_returns_stage1_dict():
    m = LatencyMarks()
    m.mark_wake("Alice", now=100.0)
    m.mark_llm_start(now=100.5)
    d = m.mark_first_sentence(now=101.2)
    assert d is not None
    assert d["speaker"] == "Alice"
    assert d["wake_to_llm_ms"] == pytest.approx(500.0)
    assert d["llm_to_sentence_ms"] == pytest.approx(700.0)
    assert m.first_sentence_ts == 101.2


def test_mark_first_sentence_returns_none_without_llm_start():
    m = LatencyMarks()
    m.mark_wake("Alice", now=100.0)
    assert m.mark_first_sentence(now=101.0) is None


def test_mark_first_sentence_returns_none_without_wake():
    m = LatencyMarks()
    assert m.mark_first_sentence(now=101.0) is None


def test_mark_first_audio_returns_stage2_dict_and_resets():
    m = LatencyMarks()
    m.mark_wake("Alice", now=100.0)
    m.mark_llm_start(now=100.5)
    m.mark_first_sentence(now=101.2)
    d = m.mark_first_audio_and_consume(now=102.0)
    assert d is not None
    assert d["speaker"] == "Alice"
    assert d["sentence_to_audio_ms"] == pytest.approx(800.0)
    assert d["total_wake_to_audio_ms"] == pytest.approx(2000.0)
    # State reset 後再呼叫應該回 None
    assert m.mark_first_audio_and_consume(now=103.0) is None
    assert m.speaker is None
    assert m.wake_ts is None


def test_mark_first_audio_returns_none_without_first_sentence():
    m = LatencyMarks()
    m.mark_wake("Alice", now=100.0)
    m.mark_llm_start(now=100.5)
    assert m.mark_first_audio_and_consume(now=102.0) is None


def test_new_wake_overwrites_pending_state():
    """Last wake wins — 第二輪 wake 把第一輪的中途 state 清掉。"""
    m = LatencyMarks()
    m.mark_wake("Alice", now=100.0)
    m.mark_llm_start(now=100.5)
    # Alice 還沒走完，Bob 喚醒 → Alice state 被覆蓋
    m.mark_wake("Bob", now=200.0)
    assert m.speaker == "Bob"
    assert m.wake_ts == 200.0
    assert m.llm_start_ts is None
    assert m.first_sentence_ts is None


def test_full_cycle_end_to_end():
    m = LatencyMarks()
    m.mark_wake("狗與露", now=1000.000)
    m.mark_llm_start(now=1000.123)  # 123ms
    d1 = m.mark_first_sentence(now=1001.234)  # +1.111s
    d2 = m.mark_first_audio_and_consume(now=1001.834)  # +600ms

    assert d1["wake_to_llm_ms"] == pytest.approx(123.0)
    assert d1["llm_to_sentence_ms"] == pytest.approx(1111.0)
    assert d2["sentence_to_audio_ms"] == pytest.approx(600.0)
    assert d2["total_wake_to_audio_ms"] == pytest.approx(1834.0)
