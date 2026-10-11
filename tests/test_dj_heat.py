"""TDD: DJ 串場依聊天熱度調整——純函式 is_hot / TopicBank（dj_heat.py）。

2026-09-30 使用者定：熱聊時素材最多但沒人在聽 DJ，應該熱聊時少講（只報歌名）。
2026-10-11 使用者定：拔掉接回話題的 revival，熱聊時改成整段不講話直接
crossfade 進下一首——TopicBank 只剩熱度判斷用的狀態（is_hot）跟 conversation
mode 的已用原句紀錄（is_consumed/mark_consumed）。
"""
from __future__ import annotations

from dj_heat import (
    CONSUMED_MAX_AGE_S,
    DJ_HOT_UTTERANCE_COUNT,
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


# ── TopicBank：conversation mode 已用原句紀錄 ─────────────────────────────

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
