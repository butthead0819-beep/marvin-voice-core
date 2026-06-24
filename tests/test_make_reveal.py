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
    refine_topics,
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
    assert reel.topic_peaks               # 至少一個「有主題」紅點


def test_curate_reel_topic_dots_capped_at_5():
    # 7 個有主題的搶話事件 → 紅點最多 5 個
    rows = []
    for i in range(7):
        base = 100.0 + i * 100
        rows += [(f"A{i}", CLEAN_A, base), (f"B{i}", CLEAN_B, base + 1)]
    reel = curate_reel(rows)
    assert reel is not None
    assert len(reel.topic_peaks) == 5     # 每晚最多 5 個


def test_curate_reel_excludes_topicless_peaks():
    # 一個有主題事件 + 一個全糊字事件 → 只有有主題的進紅點
    rows = [
        ("A", CLEAN_A, 100.0), ("B", CLEAN_B, 101.0),            # 有主題
        ("C", GARBAGE_REPEAT, 300.0), ("D", GARBAGE_LAUGH, 301.0),  # 無主題（糊字）
    ]
    reel = curate_reel(rows)
    assert reel is not None
    assert len(reel.topic_peaks) == 1
    assert reel.topic_peaks[0][0] == 100.0


def test_curate_reel_includes_songs_in_window():
    rows = [("A", CLEAN_A, 100.0), ("B", CLEAN_B, 101.0)]
    songs = [(100.5, "A", "周杰倫 - 晴天"), (999999.0, "B", "視窗外的歌")]
    reel = curate_reel(rows, song_requests=songs)
    titles = [t for _ts, _u, t in reel.songs]
    assert "周杰倫 - 晴天" in titles
    assert "視窗外的歌" not in titles     # 視窗外的點歌不標


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


# ── refine_topics（LLM 精煉主題，可注入、全防禦）────────────────────
def test_refine_topics_uses_injected_llm():
    quotes = ["嗯神經啊那是獵血", "可是我是在跟巴黑講他的馬要夠聰明"]
    fake = lambda _sys, _user: "獵血遊戲\n馬要夠聰明"
    out = refine_topics(quotes, text_fn=fake)
    assert out == ["獵血遊戲", "馬要夠聰明"]


def test_refine_topics_strips_numbering():
    fake = lambda _s, _u: "1. 獵血\n2. 國防預算"
    assert refine_topics(["a", "b"], text_fn=fake) == ["獵血", "國防預算"]


def test_refine_topics_no_text_fn_falls_back_to_truncated():
    long_q = "那個那個我都那個我都 OK我都有交代交代那些年輕人設計師"
    out = refine_topics([long_q], text_fn=None)
    assert out[0].endswith("…") and len(out[0]) <= 13


def test_refine_topics_llm_error_falls_back():
    def boom(_s, _u):
        raise RuntimeError("LLM down")
    assert refine_topics(["獵血那是"], text_fn=boom) == ["獵血那是"]


def test_refine_topics_count_mismatch_falls_back():
    # LLM 回的行數對不上 → 不冒險，退回原句
    fake = lambda _s, _u: "只有一行"
    assert refine_topics(["a句子", "b句子"], text_fn=fake) == ["a句子", "b句子"]


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
    assert "topic_peaks" in data and "songs" in data
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
