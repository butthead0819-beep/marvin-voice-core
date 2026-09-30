"""TDD: DJ 串場依聊天熱度調整——純函式 is_hot / TopicBank（dj_heat.py）。

2026-09-30 使用者定：熱聊時素材最多但沒人在聽 DJ，應該熱聊時少講（只報歌名），
降溫時把剛聊的話題拿出來接回去。
"""
from __future__ import annotations

import pytest

from dj_heat import BANK_MAX_AGE_S, LINE_MAX_CHARS, TopicBank, is_hot

NOW = 1_759_200_000.0


def _entry(speaker: str, text: str, age_s: float = 0.0) -> dict:
    return {"timestamp": NOW - age_s, "speaker": speaker, "text": text}


# ── is_hot ───────────────────────────────────────────────────────────────

def test_single_person_never_hot_even_with_many_utterances():
    entries = [_entry("大肚", f"訊息{i}", age_s=i) for i in range(10)]
    assert is_hot(entries, n_online=1, now=NOW) is False


def test_two_online_four_recent_utterances_is_hot():
    entries = [_entry("大肚", "a", 10), _entry("狗與露", "b", 20),
               _entry("大肚", "c", 30), _entry("狗與露", "d", 40)]
    assert is_hot(entries, n_online=2, now=NOW) is True


def test_two_online_three_recent_utterances_not_hot():
    entries = [_entry("大肚", "a", 10), _entry("狗與露", "b", 20), _entry("大肚", "c", 30)]
    assert is_hot(entries, n_online=2, now=NOW) is False


def test_marvin_speaker_not_counted():
    entries = [_entry("Marvin", "a", 1), _entry("Marvin推薦", "b", 2),
               _entry("大肚", "c", 3), _entry("狗與露", "d", 4)]
    assert is_hot(entries, n_online=2, now=NOW) is False


def test_utterances_outside_window_not_counted():
    entries = [_entry("大肚", "a", 200), _entry("狗與露", "b", 210),
               _entry("大肚", "c", 220), _entry("狗與露", "d", 5)]
    assert is_hot(entries, n_online=2, now=NOW) is False


def test_entries_not_list_is_not_hot():
    assert is_hot(None, n_online=5, now=NOW) is False
    assert is_hot("garbage", n_online=5, now=NOW) is False


# ── TopicBank ────────────────────────────────────────────────────────────

def test_snapshot_then_take_formats_lines():
    bank = TopicBank()
    entries = [_entry("大肚", "今天好累喔", 5)]
    bank.snapshot(entries, NOW)
    lines = bank.take(NOW)
    assert lines == ["大肚：「今天好累喔」"]


def test_line_truncated_to_max_chars():
    bank = TopicBank()
    long_text = "字" * 40
    bank.snapshot([_entry("大肚", long_text, 1)], NOW)
    lines = bank.take(NOW)
    assert len(lines) == 1
    inner = lines[0].split("：「", 1)[1].rstrip("」")
    assert inner == long_text[:LINE_MAX_CHARS]


def test_only_keeps_last_max_lines():
    entries = [_entry(f"人{i}", f"話{i}", age_s=(20 - i)) for i in range(20)]
    bank = TopicBank()
    bank.snapshot(entries, NOW)
    lines = bank.take(NOW)
    assert len(lines) == 8
    assert lines[-1] == "人19：「話19」"


def test_take_twice_returns_empty_second_time():
    bank = TopicBank()
    bank.snapshot([_entry("大肚", "hi", 1)], NOW)
    assert bank.take(NOW) != []
    assert bank.take(NOW) == []


def test_take_after_max_age_expires_returns_empty():
    bank = TopicBank()
    bank.snapshot([_entry("大肚", "hi", 1)], NOW)
    assert bank.take(NOW + BANK_MAX_AGE_S + 1) == []


def test_snapshot_with_no_usable_lines_does_not_overwrite_existing():
    bank = TopicBank()
    bank.snapshot([_entry("大肚", "hi", 1)], NOW)
    bank.snapshot([], NOW + 5)
    bank.snapshot([_entry("Marvin", "只有機器人講話", 1)], NOW + 5)
    lines = bank.take(NOW + 5)
    assert lines == ["大肚：「hi」"]
