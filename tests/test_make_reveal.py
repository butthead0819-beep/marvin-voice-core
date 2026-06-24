"""make_reveal v0.1（靜態 EKG PNG）行為測試。

驗證：自動選窗 + 引言品質閘（擋 STT 糊字）+ 平淡夜退場 + 端到端真跡 PNG/json。
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from make_reveal import (
    _quote_quality_ok,
    build_reveal,
    curate_reel,
    make_reveal_from_db,
)

CLEAN_A = "今天那個會議真的有夠久"   # 11，乾淨
CLEAN_B = "對啊我整個快睡著了啦"     # 10，乾淨
GARBAGE_REPEAT = "嗯嗯嗯嗯嗯嗯嗯嗯嗯嗯"  # 10 同字（STT 糊字）
GARBAGE_LAUGH = "哈哈哈哈哈哈哈哈哈哈"   # 10 同字


# ── 引言品質閘 ─────────────────────────────────────────────────────
def test_quote_quality_rejects_repeated_char():
    assert _quote_quality_ok(GARBAGE_REPEAT) is False
    assert _quote_quality_ok(GARBAGE_LAUGH) is False


def test_quote_quality_rejects_comma_hallucination():
    assert _quote_quality_ok("嗨馬文,嗨馬文,嗨馬文") is False


def test_quote_quality_rejects_pure_punct():
    assert _quote_quality_ok("。。。！！？") is False


def test_quote_quality_rejects_too_short():
    assert _quote_quality_ok("嗨") is False


def test_quote_quality_accepts_real_sentence():
    assert _quote_quality_ok(CLEAN_A) is True
    assert _quote_quality_ok(CLEAN_B) is True


# ── curate_reel ───────────────────────────────────────────────────
def test_curate_reel_flat_night_none():
    rows = [("A", "嗯嗯", 100.0), ("B", "好", 130.0)]  # 太短、無搶話
    assert curate_reel(rows) is None


def test_curate_reel_picks_window_and_clean_quote():
    rows = [("A", CLEAN_A, 100.0), ("B", CLEAN_B, 101.0)]
    reel = curate_reel(rows)
    assert reel is not None
    start, end = reel.window
    assert start <= reel.hero_ts <= end
    assert reel.quote == CLEAN_A          # 取最熱搶話事件第一句乾淨引言
    assert reel.activity_track            # 底層發言密度非空
    assert reel.peaks                     # 至少一個搶話峰標記


def test_curate_reel_filters_bot_lines():
    # Marvin 自己的句不該灌發言密度
    rows = [("馬文", CLEAN_A, 90.0), ("A", CLEAN_A, 100.0), ("B", CLEAN_B, 101.0)]
    reel = curate_reel(rows)
    assert reel is not None
    total = sum(n for _, n in reel.activity_track)
    assert total == 2                     # 只算 A、B，不算馬文


def test_curate_reel_rejects_garbage_quote_window():
    # 有搶話事件（兩句都 ≥8 字成峰），但全是 STT 糊字 → 選不出乾淨引言 → None
    rows = [("A", GARBAGE_REPEAT, 100.0), ("B", GARBAGE_LAUGH, 101.0)]
    assert curate_reel(rows) is None


# ── 端到端真跡（slow）──────────────────────────────────────────────
@pytest.mark.slow
def test_build_reveal_smoke_produces_png_and_json(tmp_path):
    rows = [("A", CLEAN_A, 100.0), ("B", CLEAN_B, 101.0)]
    out = build_reveal(rows, str(tmp_path), date_label="2026-06-24")
    assert out is not None
    png, js = out
    assert png.endswith(".png")
    import os
    assert os.path.getsize(png) > 0
    data = json.loads(open(js, encoding="utf-8").read())
    assert "window" in data and "hero" in data and "activity_track" in data
    assert data["hero"]["quote"] == CLEAN_A


@pytest.mark.slow
def test_make_reveal_from_db_smoke(tmp_path):
    import datetime
    start_str = "2026-06-24T22:00:00"
    end_str = "2026-06-24T22:00:05"
    base = datetime.datetime.fromisoformat(start_str).timestamp()  # 與 _db_rows 同 tz 解讀
    db = str(tmp_path / "t.db")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE transcripts (speaker TEXT, text TEXT, timestamp REAL)")
    con.executemany("INSERT INTO transcripts VALUES (?,?,?)",
                    [("A", CLEAN_A, base + 1), ("B", CLEAN_B, base + 2)])
    con.commit()
    con.close()
    out = make_reveal_from_db(db, start_str, end_str, str(tmp_path))
    assert out is not None
    png, _ = out
    import os
    assert os.path.getsize(png) > 0


def test_make_reveal_from_db_no_rows_none(tmp_path):
    db = str(tmp_path / "empty.db")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE transcripts (speaker TEXT, text TEXT, timestamp REAL)")
    con.commit()
    con.close()
    assert make_reveal_from_db(db, "2026-06-24T22:00:00",
                               "2026-06-24T22:00:05", str(tmp_path)) is None
