"""youtube_heatmap.pick_highlight_start（TDD）：yt-dlp heatmap → 精華片段起點。"""
import pytest

import youtube_heatmap as yh


def _heatmap(*values, seg_len=10.0):
    out = []
    for i, v in enumerate(values):
        out.append({"start_time": i * seg_len, "end_time": (i + 1) * seg_len, "value": v})
    return out


def test_picks_highest_value_segment_start():
    heatmap = _heatmap(0.3, 0.9, 0.5, 0.4, 0.2, 0.3, 0.2, 0.1, 0.1, 0.1)  # 10 段, 100s
    result = yh.pick_highlight_start(heatmap, duration=100.0)
    assert result == 10.0  # 第 2 段（index 1）value 最高 → start_time=10


def test_no_heatmap_returns_none():
    assert yh.pick_highlight_start(None, duration=200.0) is None
    assert yh.pick_highlight_start([], duration=200.0) is None


def test_no_duration_returns_none():
    heatmap = _heatmap(0.3, 0.9, 0.5)
    assert yh.pick_highlight_start(heatmap, duration=None) is None
    assert yh.pick_highlight_start(heatmap, duration=0) is None


def test_song_too_short_returns_none():
    heatmap = _heatmap(0.3, 0.9, 0.5)
    assert yh.pick_highlight_start(heatmap, duration=60.0) is None  # < 90s 門檻


def test_highlight_too_close_to_end_returns_none():
    # 100s 曲子，最高熱度那段起點在 80s，離結尾只剩 20s (< min_remaining=45) → 放棄
    heatmap = [
        {"start_time": 0.0, "end_time": 80.0, "value": 0.3},
        {"start_time": 80.0, "end_time": 100.0, "value": 0.9},
    ]
    assert yh.pick_highlight_start(heatmap, duration=100.0) is None


def test_disabled_flag_returns_none(monkeypatch):
    monkeypatch.setenv("MARVIN_HIGHLIGHT_START", "0")
    heatmap = _heatmap(0.3, 0.9, 0.5, 0.4, 0.2, 0.3, 0.2, 0.1, 0.1, 0.1)
    assert yh.pick_highlight_start(heatmap, duration=100.0) is None


def test_missing_start_time_returns_none():
    heatmap = [{"end_time": 10.0, "value": 0.9}]
    assert yh.pick_highlight_start(heatmap, duration=200.0) is None
