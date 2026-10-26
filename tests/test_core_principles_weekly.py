"""核心規範週報（docs/MARVIN_CORE_PRINCIPLES.md §4 / G6）純函式測試。期望值一律寫死。"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import core_principles_weekly as w  # noqa: E402

S, E = 1000.0, 2000.0


def test_percentile_nearest_rank():
    assert w.percentile(list(range(1, 11)), 0.5) == 6
    assert w.percentile(list(range(1, 11)), 0.9) == 10
    assert w.percentile([3.0], 0.9) == 3.0
    assert w.percentile([], 0.5) is None


def test_window_includes_start_excludes_end():
    rows = [{"ts": 1000.0, "metric": "recall", "correct": True},
            {"ts": 2000.0, "metric": "recall", "correct": False}]
    assert w.memory_recall(rows, S, E) == {"n": 1, "hit": 1, "rate": 1.0}


def test_memory_recall_empty():
    assert w.memory_recall([{"ts": 1500, "metric": "speech"}], S, E) == {"n": 0}


def test_marvin_skip_rate_splits_marvin_and_human_and_ignores_non_play():
    plays = [
        {"ts": 1100, "type": "play", "requested_by": "Marvin推薦（點給大家）"},
        {"ts": 1200, "type": "play", "requested_by": "Marvin推薦（點給大家）"},
        {"ts": 1300, "type": "play", "requested_by": "狗與露"},
        {"ts": 1400, "type": "end", "requested_by": "Marvin推薦（點給大家）"},
    ]
    skips = [{"ts": 1150, "requested_by": "Marvin推薦（點給大家）"},
             {"ts": 1350, "requested_by": "狗與露"}]
    r = w.marvin_skip_rate(plays, skips, S, E)
    assert r["n"] == 2 and r["hit"] == 1 and r["rate"] == 0.5
    assert r["human"] == {"n": 1, "hit": 1, "rate": 1.0}


def test_marvin_skip_rate_empty():
    assert w.marvin_skip_rate([], [], S, E)["n"] == 0


def test_pick_lanes_memory_share_excludes_discovery():
    recs = [{"ts": 1100, "agent": "music", "channel_state": {"lane": "discovery"}},
            {"ts": 1100, "agent": "music", "channel_state": {"lane": "discovery"}},
            {"ts": 1100, "agent": "music", "channel_state": {"lane": "long_tail"}},
            {"ts": 1100, "agent": "music", "channel_state": {"lane": "spotlight"}},
            {"ts": 1100, "agent": "dj", "channel_state": {"lane": "spotlight"}}]
    r = w.pick_lanes(recs, S, E)
    assert r["n"] == 4
    assert r["lanes"] == {"discovery": 2, "long_tail": 1, "spotlight": 1}
    assert r["memory_direct_share"] == 0.5


def _sp(**kw):
    base = {"ts": 1500, "metric": "speech", "kind": "proactive_topic", "src": "tts",
            "verdict": "play", "dur_s": 3.0, "chars": 10, "hot_chat": False,
            "n_speaking": 0, "requested": False, "kind_inferred": False}
    base.update(kw)
    return base


def test_hot_chat_unrequested_counts_only_unrequested_played_in_hot_chat():
    rows = [_sp(hot_chat=True),                       # 計
            _sp(hot_chat=True, requested=True),       # 被請求，不計
            _sp(hot_chat=None),                       # 熱度未知，不計
            _sp(hot_chat=False, n_speaking=2),        # 不計主數，但算「開口時有人在講」
            _sp(hot_chat=True, verdict="drop", dur_s=0.0),
            _sp(hot_chat=True, verdict="drop_to_text", dur_s=0.0, kind_inferred=True)]
    r = w.hot_chat_unrequested(rows, S, E)
    assert r["n"] == 3          # 主動且有播出的列數（分母）
    assert r["hot"] == 1
    assert r["over_speech"] == 1
    assert r["blocked"] == 2


def test_hot_chat_unrequested_empty():
    assert w.hot_chat_unrequested([], S, E) == {"n": 0}


def test_wake_latency_parses_local_string_timestamp_and_skips_none():
    ts = datetime(2026, 10, 20, 12, 0, 0, tzinfo=w.TPE).timestamp()
    fb = [{"timestamp": "2026-10-20 12:00:00", "wake_latency_sec": 2.0},
          {"timestamp": "2026-10-20 12:00:00", "wake_latency_sec": None},
          {"timestamp": "2026-10-20 12:00:00", "wake_latency_sec": 8.0},
          {"timestamp": "壞掉", "wake_latency_sec": 1.0}]
    r = w.wake_latency(fb, ts - 1, ts + 1)
    assert r == {"n": 2, "p50": 8.0, "p90": 8.0}


def test_false_responding():
    rows = [{"ts": 1100, "metric": "false_responding", "was_false": True},
            {"ts": 1200, "metric": "false_responding", "was_false": False}]
    assert w.false_responding(rows, S, E) == {"n": 2, "hit": 1, "rate": 0.5}


def test_proactive_voice_splits_requested_and_per_presence_hour():
    rows = [_sp(dur_s=2.0), _sp(dur_s=6.0), _sp(dur_s=None, kind_inferred=True),
            _sp(verdict="drop", dur_s=0.0),
            _sp(requested=True, dur_s=10.0)]
    r = w.proactive_voice(rows, S, E, presence_hours=2)
    assert r["n"] == 3
    assert r["p95"] == 6.0
    assert r["total_s"] == 8.0
    assert r["per_presence_hour"] == 4.0
    assert r["inferred_share"] == 1 / 3
    assert r["requested"] == {"n": 1, "p95": 10.0}


def test_proactive_voice_empty():
    r = w.proactive_voice([], S, E, presence_hours=0)
    assert r["n"] == 0 and r["requested"] == {"n": 0}


def test_presence_hours_distinct_local_hour_and_skips_empty():
    h = datetime(2026, 10, 20, 21, 0, 0, tzinfo=w.TPE).timestamp()
    rows = [{"ts": h + 10, "metric": "stt_confidence", "empty": False},
            {"ts": h + 600, "metric": "stt_confidence", "empty": False},
            {"ts": h + 3700, "metric": "stt_confidence", "empty": True},
            {"ts": h + 7300, "metric": "stt_confidence", "empty": False}]
    assert w.presence_hours(rows, h, h + 10000) == 2


def test_memory_checks_with_data():
    rows = [
        {"ts": 1100, "event": "asked", "speaker": "狗與露", "item": "張雨生"},
        {"ts": 1150, "event": "answer", "speaker": "狗與露", "item": "張雨生", "answer": "yes"},
        {"ts": 1200, "event": "asked", "speaker": "大肚", "item": "周杰倫"},
        {"ts": 1250, "event": "answer", "speaker": "大肚", "item": "周杰倫", "answer": "no"},
        {"ts": 1300, "event": "answer", "speaker": "大肚", "item": "周杰倫", "answer": "unclear"},
    ]
    r = w.memory_checks(rows, S, E)
    assert r == {"n": 2, "hit": 1, "rate": 0.5, "asked": 2, "answered": 2}


def test_memory_checks_no_data():
    assert w.memory_checks([], S, E) == {"n": 0, "asked": 0, "answered": 0}
    rows = [{"ts": 1100, "event": "asked", "speaker": "狗與露", "item": "張雨生"}]
    assert w.memory_checks(rows, S, E) == {"n": 0, "asked": 1, "answered": 0}


def _empty_week(presence=0):
    return w.compute({}, S, E) | {"presence_hours": presence}


def test_render_small_sample_and_no_data_marks():
    md = w.render(_empty_week(3), _empty_week(0), "2026-10-19", "2026-10-26")
    assert "樣本不足" in md
    assert "無資料" in md
    assert "| — |" in md
    assert "連續兩週無資料" in md and "1 記憶正確率" in md.split("連續兩週無資料")[1]
    assert "量測點未接（G3）" in md
    assert "1b 現場確認記憶" in md


def test_render_pass_fail_marks():
    cur = _empty_week(20)
    cur["skip"] = {"n": 100, "hit": 3, "rate": 0.03, "human": {"n": 0}}
    cur["latency"] = {"n": 10, "p50": 6.9, "p90": 13.9}
    md = w.render(cur, _empty_week(20), "2026-10-19", "2026-10-26")
    assert "樣本不足" not in md
    skip_line = next(l for l in md.splitlines() if "推薦 skip 率" in l)
    assert "3.0% (3/100)" in skip_line and "✅" in skip_line
    lat_line = next(l for l in md.splitlines() if "喚醒延遲" in l)
    assert "❌" in lat_line


def test_main_writes_report(tmp_path):
    rec = tmp_path / "records"
    rec.mkdir(exist_ok=True)
    ts = datetime(2026, 10, 22, 21, 0, 0, tzinfo=w.TPE).timestamp()
    (rec / "quality_metrics.jsonl").write_text(
        json.dumps({"ts": ts, "metric": "recall", "correct": True}) + "\n壞行\n", encoding="utf-8")
    w.main(["--root", str(tmp_path), "--end", "2026-10-26"])
    out = rec / "core_principles_weekly_2026-10-26.md"
    assert out.exists()
    assert "100.0% (1/1)" in out.read_text(encoding="utf-8")
