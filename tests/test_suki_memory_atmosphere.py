"""SukiMemory 氣氛校正持久化測試（Lane A2）。

對應 atmosphere_corrections 資料表 + 兩個對稱方法：
  - record_atmosphere_correction(snapshot_ts, label, speaker=None) -> None
  - get_atmosphere_calibration() -> {label_counts, recent_corrections}
"""

import time
import pytest

from suki_memory import MemoryManager


@pytest.fixture
def mem(tmp_path):
    """每個測試開一份乾淨的 SQLite + JSON。"""
    db = str(tmp_path / "test.db")
    jpath = str(tmp_path / "test_memory.json")
    return MemoryManager(db_path=db, json_compat_path=jpath)


# ── 基本寫入 / 讀回 ────────────────────────────────────────────────────────────

def test_record_correction_persists(mem):
    """寫一筆校正，get_atmosphere_calibration 應在 recent_corrections 看得到。"""
    ts = time.time()
    mem.record_atmosphere_correction(ts, "too_loud", "alice")

    calib = mem.get_atmosphere_calibration()
    assert "recent_corrections" in calib
    assert len(calib["recent_corrections"]) == 1

    row = calib["recent_corrections"][0]
    assert row["snapshot_ts"] == ts
    assert row["label"] == "too_loud"
    assert row["speaker"] == "alice"
    assert "created_ts" in row


# ── 聚合 label_counts ──────────────────────────────────────────────────────────

def test_label_counts_aggregate(mem):
    """3 筆 too_loud + 1 筆 too_sharp → label_counts 反應正確。"""
    ts = time.time()
    for _ in range(3):
        mem.record_atmosphere_correction(ts, "too_loud", "alice")
    mem.record_atmosphere_correction(ts, "too_sharp", "bob")

    calib = mem.get_atmosphere_calibration()
    assert calib["label_counts"] == {"too_loud": 3, "too_sharp": 1}


# ── recent_corrections 上限 ────────────────────────────────────────────────────

def test_recent_corrections_limit(mem):
    """寫 60 筆 → 只回傳最近 50 筆。"""
    base_ts = time.time()
    for i in range(60):
        mem.record_atmosphere_correction(base_ts + i, "too_jolly", "alice")

    calib = mem.get_atmosphere_calibration()
    assert len(calib["recent_corrections"]) == 50


# ── 排序：最新先 ──────────────────────────────────────────────────────────────

def test_recent_corrections_ordering(mem):
    """recent_corrections 必須以 created_ts DESC 排序（最新先）。"""
    mem.record_atmosphere_correction(1.0, "too_loud", "alice")
    time.sleep(0.01)  # 確保 created_ts 不同
    mem.record_atmosphere_correction(2.0, "too_sharp", "bob")
    time.sleep(0.01)
    mem.record_atmosphere_correction(3.0, "too_jolly", "carol")

    calib = mem.get_atmosphere_calibration()
    rows = calib["recent_corrections"]
    assert rows[0]["label"] == "too_jolly"
    assert rows[1]["label"] == "too_sharp"
    assert rows[2]["label"] == "too_loud"


# ── 空資料庫 ──────────────────────────────────────────────────────────────────

def test_get_calibration_empty_when_no_corrections(mem):
    """完全沒有校正時，回傳乾淨的空結構。"""
    calib = mem.get_atmosphere_calibration()
    assert calib == {"label_counts": {}, "recent_corrections": []}


# ── speaker 為可選 ────────────────────────────────────────────────────────────

def test_speaker_optional(mem):
    """未傳 speaker → 存 NULL，回傳 None。"""
    ts = time.time()
    mem.record_atmosphere_correction(ts, "too_loud")

    calib = mem.get_atmosphere_calibration()
    assert len(calib["recent_corrections"]) == 1
    assert calib["recent_corrections"][0]["speaker"] is None
