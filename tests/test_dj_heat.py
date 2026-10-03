"""TDD: DJ 串場依聊天熱度調整——純函式 is_hot / TopicBank（dj_heat.py）。

2026-09-30 使用者定：熱聊時素材最多但沒人在聽 DJ，應該熱聊時少講（只報歌名），
降溫時把剛聊的話題拿出來接回去。
"""
from __future__ import annotations

import pytest

from dj_heat import (
    BANK_MAX_AGE_S,
    CONSUMED_MAX_AGE_S,
    DJ_HOT_UTTERANCE_COUNT,
    LINE_MAX_CHARS,
    TopicBank,
    is_hot,
)

NOW = 1_759_200_000.0


def _entry(speaker: str, text: str, age_s: float = 0.0) -> dict:
    return {"timestamp": NOW - age_s, "speaker": speaker, "text": text}


def _alternating(n: int) -> list:
    speakers = ["大肚", "狗與露"]
    return [_entry(speakers[i % 2], f"訊息{i}", age_s=i) for i in range(n)]


# ── is_hot ───────────────────────────────────────────────────────────────

def test_single_person_never_hot_even_with_many_utterances():
    entries = [_entry("大肚", f"訊息{i}", age_s=i) for i in range(10)]
    assert is_hot(entries, n_online=1, now=NOW) is False


def test_dj_hot_threshold_is_20():
    assert DJ_HOT_UTTERANCE_COUNT == 20


def test_two_online_threshold_recent_utterances_is_hot():
    entries = _alternating(DJ_HOT_UTTERANCE_COUNT)
    assert is_hot(entries, n_online=2, now=NOW) is True


def test_two_online_below_threshold_not_hot():
    entries = _alternating(DJ_HOT_UTTERANCE_COUNT - 1)
    assert is_hot(entries, n_online=2, now=NOW) is False


def test_marvin_speaker_not_counted():
    # 真人發言數剛好低於門檻；若把 Marvin 的發言也算進去會達到門檻。
    entries = _alternating(DJ_HOT_UTTERANCE_COUNT - 1)
    entries.append(_entry("Marvin", "a", 1))
    entries.append(_entry("Marvin推薦", "b", 2))
    assert is_hot(entries, n_online=2, now=NOW) is False


def test_utterances_outside_window_not_counted():
    # 窗內真人發言剛好低於門檻；窗外幾句若算進去會達到門檻。
    entries = _alternating(DJ_HOT_UTTERANCE_COUNT - 1)
    entries.append(_entry("大肚", "a", 200))
    entries.append(_entry("狗與露", "b", 210))
    entries.append(_entry("大肚", "c", 220))
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


# ── 同一段原句只用一次（10/2 錄音：「紅玉怪客」連用 3 段）──────────────────

def test_take_then_snapshot_same_entries_does_not_repeat():
    bank = TopicBank()
    entries = [_entry("大肚", "紅玉怪客", 5), _entry("狗與露", "吸血劍", 3)]
    bank.snapshot(entries, NOW)
    assert bank.take(NOW) != []
    bank.snapshot(entries, NOW + 10)
    assert bank.take(NOW + 10) == []


def test_snapshot_after_take_only_keeps_new_lines():
    bank = TopicBank()
    e1, e2 = _entry("大肚", "第一句", 20), _entry("狗與露", "第二句", 10)
    bank.snapshot([e1, e2], NOW)
    bank.take(NOW)
    e3 = _entry("showay", "第三句", 1)
    bank.snapshot([e1, e2, e3], NOW)
    assert bank.take(NOW) == ["showay：「第三句」"]


def test_expired_take_does_not_mark_consumed(monkeypatch):
    # 拉長清除門檻：預設兩者都是 900 秒，過期那刻標記的紀錄會被同時清掉、測不出差別
    import dj_heat
    monkeypatch.setattr(dj_heat, "CONSUMED_MAX_AGE_S", 10 * BANK_MAX_AGE_S)
    bank = TopicBank()
    entries = [_entry("大肚", "hi", 1)]
    bank.snapshot(entries, NOW)
    assert bank.take(NOW + BANK_MAX_AGE_S + 1) == []
    bank.snapshot(entries, NOW)
    assert bank.take(NOW) == ["大肚：「hi」"]


def test_mark_consumed_and_is_consumed():
    bank = TopicBank()
    used, unused = _entry("大肚", "用過", 1), _entry("狗與露", "沒用過", 2)
    bank.mark_consumed([used, {"speaker": "x", "text": "沒時戳"}, "garbage"], NOW)
    assert bank.is_consumed(used) is True
    assert bank.is_consumed(unused) is False


def test_consumed_records_pruned_after_max_age():
    bank = TopicBank()
    old = _entry("大肚", "舊句", 0)
    bank.mark_consumed([old], NOW)
    later = NOW + CONSUMED_MAX_AGE_S + 1
    bank.mark_consumed([{"timestamp": later, "speaker": "狗與露", "text": "新句"}], later)
    assert bank.is_consumed(old) is False
