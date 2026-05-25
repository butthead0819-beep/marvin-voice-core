"""TDD: analyze_daily_feedback.py 自動偵測 dominant guild_id。

Bug 2026-05-25:
- transcripts table 99.93% 資料在 guild_id=1133088321254461552
- make_transcript_fetcher 預設 guild_id=0
- 結果 fetcher 拿不到任何 utt → analyzer 全部 fallback "silence in window" conf=0.4
- 全卡在 T1 threshold 0.5 之下 → music_memory.recommendations.feedback 永遠空
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from scripts.analyze_daily_feedback import detect_dominant_guild_id


def _seed_db(path: Path, rows: list[tuple[int, int]]) -> None:
    """rows: [(guild_id, count), ...]"""
    conn = sqlite3.connect(str(path))
    conn.execute("""
        CREATE TABLE transcripts (
            id INTEGER PRIMARY KEY,
            speaker TEXT, guild_id INTEGER, channel_id INTEGER,
            text TEXT, timestamp REAL
        )
    """)
    for gid, cnt in rows:
        for i in range(cnt):
            conn.execute(
                "INSERT INTO transcripts (speaker, guild_id, channel_id, text, timestamp) VALUES (?, ?, ?, ?, ?)",
                ("X", gid, 0, "hi", float(i)),
            )
    conn.commit()
    conn.close()


def test_detect_picks_guild_with_most_rows(tmp_path):
    db = tmp_path / "m.db"
    _seed_db(db, [(0, 6), (1133088321254461552, 9125)])
    assert detect_dominant_guild_id(str(db)) == 1133088321254461552


def test_detect_returns_zero_on_empty_table(tmp_path):
    db = tmp_path / "m.db"
    _seed_db(db, [])
    assert detect_dominant_guild_id(str(db)) == 0


def test_detect_returns_zero_on_missing_db(tmp_path):
    """DB 檔不存在 → 0（安全 fallback，不 raise）。"""
    assert detect_dominant_guild_id(str(tmp_path / "nope.db")) == 0


def test_detect_returns_zero_on_missing_table(tmp_path):
    """DB 存在但沒有 transcripts table → 0。"""
    db = tmp_path / "m.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE other (x INTEGER)")
    conn.commit()
    conn.close()
    assert detect_dominant_guild_id(str(db)) == 0


def test_detect_breaks_tie_by_largest_guild_id(tmp_path):
    """同筆數時取較大的 guild_id（穩定優先序，避免遇到 guild_id=0 髒資料）。"""
    db = tmp_path / "m.db"
    _seed_db(db, [(0, 5), (999, 5)])
    assert detect_dominant_guild_id(str(db)) == 999
