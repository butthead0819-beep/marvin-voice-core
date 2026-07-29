"""TDD: DJ 生活素材的 participant-aware privacy filter。

問題：dj_life_context.recent_life_cores 只看 salience，不看誰在場。
群播時敏感記憶（只有兩人知道的事）會曝光給不相干的人。

修法：加 present_speakers 參數。若某條 entry 帶 participants，
      且不是 present_speakers 的子集，就過濾掉。
      is_sensitive=True 的 entry 若 present_speakers 是 None（未知）→ 也丟掉（保守）。
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


NOW = 1_752_700_000.0


def _ts(days_ago: float) -> str:
    import datetime as _dt
    return _dt.datetime.fromtimestamp(NOW - days_ago * 86400.0).strftime("%Y-%m-%d %H:%M:%S")


def _entry(core: str, salience: str = "中", participants=None, is_sensitive: bool = False):
    """模擬 DiaryEntry，新增 participants 與 is_sensitive 欄位。"""
    e = MagicMock()
    e.ts_str = _ts(1.0)
    e.core = core
    e.salience = salience
    e.participants = participants      # None = 不限（公開事件）
    e.is_sensitive = is_sensitive
    return e


# ── 1. 無 present_speakers 時行為不變（向後相容）────────────────────────────

def test_no_present_speakers_keeps_all_entries():
    from dj_life_context import recent_life_cores
    entries = [
        _entry("大肚準備搬家"),
        _entry("狗與露要去環島"),
    ]
    cores = recent_life_cores(entries, now=NOW)
    assert len(cores) == 2


# ── 2. is_sensitive + present_speakers 未知 → 丟掉（保守原則）──────────────

def test_sensitive_entry_dropped_when_present_speakers_unknown():
    """is_sensitive=True 且 present_speakers=None → 不帶入群播 context。"""
    from dj_life_context import recent_life_cores
    entries = [
        _entry("兩個人之間的秘密", is_sensitive=True),
        _entry("公開的日常"),
    ]
    cores = recent_life_cores(entries, now=NOW, present_speakers=None)
    assert "兩個人之間的秘密" not in cores
    assert "公開的日常" in cores


# ── 3. participants 子集判斷 ─────────────────────────────────────────────────

def test_entry_with_participants_kept_when_all_present():
    """participants 全是在場人 → 帶入。"""
    from dj_life_context import recent_life_cores
    entries = [
        _entry("大肚跟狗與露聊了很久", participants=["大肚", "狗與露"]),
    ]
    present = {"大肚", "狗與露", "Alice"}
    cores = recent_life_cores(entries, now=NOW, present_speakers=present)
    assert "大肚跟狗與露聊了很久" in cores


def test_entry_with_participants_dropped_when_outsider_absent():
    """participants 有人不在場 → 丟掉（避免把私下對話說給不在的人聽）。"""
    from dj_life_context import recent_life_cores
    entries = [
        _entry("大肚跟狗與露的秘密", participants=["大肚", "狗與露"]),
    ]
    present = {"大肚", "Alice"}  # 狗與露 不在
    cores = recent_life_cores(entries, now=NOW, present_speakers=present)
    assert "大肚跟狗與露的秘密" not in cores


def test_entry_without_participants_always_kept():
    """participants=None 代表公開事件，在場任何人都能聽。"""
    from dj_life_context import recent_life_cores
    entries = [
        _entry("今天天氣很好", participants=None),
    ]
    present = {"只有一個人"}
    cores = recent_life_cores(entries, now=NOW, present_speakers=present)
    assert "今天天氣很好" in cores


# ── 4. is_sensitive + participants 明確 → 精確過濾 ──────────────────────────

def test_sensitive_entry_kept_when_all_participants_present():
    """is_sensitive=True 但 participants 全在場 → 允許（在這群人面前說得。）"""
    from dj_life_context import recent_life_cores
    entries = [
        _entry("兩個人的秘密", is_sensitive=True, participants=["大肚", "狗與露"]),
    ]
    present = {"大肚", "狗與露"}
    cores = recent_life_cores(entries, now=NOW, present_speakers=present)
    assert "兩個人的秘密" in cores


def test_sensitive_entry_dropped_when_participant_absent():
    from dj_life_context import recent_life_cores
    entries = [
        _entry("兩個人的秘密", is_sensitive=True, participants=["大肚", "狗與露"]),
    ]
    present = {"大肚"}  # 狗與露 不在
    cores = recent_life_cores(entries, now=NOW, present_speakers=present)
    assert "兩個人的秘密" not in cores


# ── 5. 舊有的 salience 低過濾仍然有效（迴歸）────────────────────────────────

def test_low_salience_still_filtered_regardless_of_participants():
    from dj_life_context import recent_life_cores
    entries = [
        _entry("無聊聊天", salience="低", participants=None),
    ]
    cores = recent_life_cores(entries, now=NOW, present_speakers={"大肚"})
    assert "無聊聊天" not in cores
