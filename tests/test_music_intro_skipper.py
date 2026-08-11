"""music_intro_skipper.pick_intro_skip_start（TDD）：LRC 同步歌詞 → 前奏結束點。"""
import pytest

import music_intro_skipper as mis


def _lrc(*lines):
    return "\n".join(f"[{int(ts // 60):02d}:{ts % 60:05.2f}]{text}" for ts, text in lines)


def test_long_intro_skips_to_first_lyric_minus_lead_in():
    lrc = _lrc((15.0, "第一句歌詞"), (20.0, "第二句歌詞"))
    result = mis.pick_intro_skip_start(lrc, duration=200.0)
    assert result == pytest.approx(13.5)  # 15.0 - 1.5s 前導


def test_short_intro_returns_none():
    lrc = _lrc((5.0, "很快就開唱"), (10.0, "第二句"))
    assert mis.pick_intro_skip_start(lrc, duration=200.0) is None  # < 10s 門檻


def test_no_lrc_returns_none():
    assert mis.pick_intro_skip_start(None, duration=200.0) is None
    assert mis.pick_intro_skip_start("", duration=200.0) is None


def test_no_duration_returns_none():
    lrc = _lrc((15.0, "第一句歌詞"))
    assert mis.pick_intro_skip_start(lrc, duration=None) is None
    assert mis.pick_intro_skip_start(lrc, duration=0) is None


def test_skip_too_close_to_end_returns_none():
    # 60s 曲子，第一句歌詞在 40s → 起播點 38.5s，離結尾只剩 21.5s (< min_remaining=30) → 放棄
    lrc = _lrc((40.0, "很晚才開唱"))
    assert mis.pick_intro_skip_start(lrc, duration=60.0) is None


def test_empty_lyric_lines_skipped_when_finding_first_real_line():
    lrc = _lrc((0.0, ""), (5.0, ""), (15.0, "第一句實質歌詞"))
    result = mis.pick_intro_skip_start(lrc, duration=200.0)
    assert result == pytest.approx(13.5)


def test_no_real_lyric_lines_returns_none():
    lrc = _lrc((0.0, ""), (5.0, ""))
    assert mis.pick_intro_skip_start(lrc, duration=200.0) is None
