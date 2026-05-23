"""tests/run_phase1_m7_standalone.py — M7 cost_alarm + baseline_analyzer

Run: venv_simon/bin/python tests/run_phase1_m7_standalone.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 動態 import 兩個 script 模組
import importlib.util

def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

ROOT = Path(__file__).resolve().parent.parent
cost_alarm = _load_module(ROOT / "scripts" / "phase1_cost_alarm.py", "cost_alarm")
analyze_baseline = _load_module(ROOT / "scripts" / "phase1_analyze_baseline.py", "analyze_baseline")


PASSED = 0
FAILED = 0
FAILURES = []


def run(name, fn):
    global PASSED, FAILED
    try:
        fn()
        print(f"  ✓ {name}")
        PASSED += 1
    except AssertionError as e:
        print(f"  ✗ {name}: {e}")
        FAILURES.append((name, traceback.format_exc()))
        FAILED += 1
    except Exception as e:
        print(f"  ✗ {name} ERROR: {type(e).__name__}: {e}")
        FAILURES.append((name, traceback.format_exc()))
        FAILED += 1


# ── cost_alarm tests ─────────────────────────────────────────────────────────

def _write_paid_log(tmp_path: Path, rows: list[dict]) -> Path:
    p = tmp_path / "llm_paid_usage.jsonl"
    with p.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return p


def t_cost_alarm_empty_log():
    """空 log → today/month 都 0、不 alarm。"""
    with tempfile.TemporaryDirectory() as td:
        log = _write_paid_log(Path(td), [])
        r = cost_alarm.analyze(log)
        assert r["today"]["total_usd"] == 0
        assert r["today"]["calls"] == 0
        assert not r["today"]["alarm"]
        assert not r["month"]["alarm"]


def t_cost_alarm_today_breakdown():
    now = time.time()
    with tempfile.TemporaryDirectory() as td:
        log = _write_paid_log(Path(td), [
            {"ts": now, "caller": "daily_review", "model": "gemini-2.5-pro", "tokens": 100, "est_usd": 0.5},
            {"ts": now, "caller": "marvin_fallback", "model": "gemini-flash", "tokens": 50, "est_usd": 0.1},
            {"ts": now, "caller": "daily_review", "model": "gemini-2.5-pro", "tokens": 80, "est_usd": 0.3},
        ])
        r = cost_alarm.analyze(log)
        assert r["today"]["calls"] == 3
        assert abs(r["today"]["total_usd"] - 0.9) < 0.01, r["today"]
        # daily_review aggregated
        assert abs(r["today"]["per_caller"]["daily_review"] - 0.8) < 0.01


def t_cost_alarm_daily_threshold_trips():
    now = time.time()
    with tempfile.TemporaryDirectory() as td:
        log = _write_paid_log(Path(td), [
            {"ts": now, "caller": "x", "model": "m", "tokens": 1, "est_usd": 3.0},
        ])
        r = cost_alarm.analyze(log, daily_alarm_usd=2.0)
        assert r["today"]["alarm"] is True, "3 USD > 2 USD threshold 應 alarm"


def t_cost_alarm_yesterday_not_in_today():
    """昨天的 cost 不算今天、但算本月。"""
    yesterday = (datetime.now() - timedelta(days=1)).replace(hour=12).timestamp()
    with tempfile.TemporaryDirectory() as td:
        log = _write_paid_log(Path(td), [
            {"ts": yesterday, "caller": "x", "model": "m", "tokens": 1, "est_usd": 1.0},
        ])
        r = cost_alarm.analyze(log)
        assert r["today"]["total_usd"] == 0, "昨天 entry 不該算今日"
        # 看是不是本月之內（如果跨月則 month_total 也 0）
        # robust 寫法：只 assert today=0
        assert r["today"]["calls"] == 0


# ── baseline_analyzer tests ──────────────────────────────────────────────────

def _write_presence_log(tmp_path: Path, events: list[dict]) -> Path:
    p = tmp_path / "voice_presence.jsonl"
    with p.open("w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    return p


def t_baseline_no_log():
    with tempfile.TemporaryDirectory() as td:
        r = analyze_baseline.analyze(Path(td) / "nope.jsonl", days=30)
        assert "error" in r


def t_baseline_empty_log():
    with tempfile.TemporaryDirectory() as td:
        log = _write_presence_log(Path(td), [])
        r = analyze_baseline.analyze(log, days=30)
        assert "error" in r


def t_baseline_simple_join_leave():
    """單一 user 一次 join + leave → 10 分鐘 active。"""
    now = time.time()
    with tempfile.TemporaryDirectory() as td:
        log = _write_presence_log(Path(td), [
            {"ts": now - 3600, "user_id": "u1", "user_name": "alice",
             "event": "join", "channel_id": "c1", "is_bot": False, "guild_id": "g1", "channel_name": "general"},
            {"ts": now - 3000, "user_id": "u1", "user_name": "alice",
             "event": "leave", "channel_id": "c1", "is_bot": False, "guild_id": "g1", "channel_name": "general"},
        ])
        r = analyze_baseline.analyze(log, days=30)
        assert r["overall"]["unique_users"] == 1
        assert r["per_user"]["u1"]["total_minutes"] == 10.0
        assert r["per_user"]["u1"]["display_name"] == "alice"


def t_baseline_filters_bots():
    """is_bot=True 的 user 不該被算進來。"""
    now = time.time()
    with tempfile.TemporaryDirectory() as td:
        log = _write_presence_log(Path(td), [
            {"ts": now - 600, "user_id": "bot1", "user_name": "Marvin",
             "event": "join", "channel_id": "c1", "is_bot": True, "guild_id": "g1", "channel_name": "general"},
            {"ts": now - 60, "user_id": "bot1", "user_name": "Marvin",
             "event": "leave", "channel_id": "c1", "is_bot": True, "guild_id": "g1", "channel_name": "general"},
            {"ts": now - 500, "user_id": "u1", "user_name": "alice",
             "event": "join", "channel_id": "c1", "is_bot": False, "guild_id": "g1", "channel_name": "general"},
            {"ts": now - 200, "user_id": "u1", "user_name": "alice",
             "event": "leave", "channel_id": "c1", "is_bot": False, "guild_id": "g1", "channel_name": "general"},
        ])
        r = analyze_baseline.analyze(log, days=30)
        assert "bot1" not in r["per_user"], "bot 不該入 per_user"
        assert "u1" in r["per_user"]


def t_baseline_dangling_open_session():
    """user join 但沒 leave → 算到「現在」。"""
    now = time.time()
    with tempfile.TemporaryDirectory() as td:
        log = _write_presence_log(Path(td), [
            {"ts": now - 600, "user_id": "u1", "user_name": "alice",
             "event": "join", "channel_id": "c1", "is_bot": False, "guild_id": "g1", "channel_name": "general"},
        ])
        r = analyze_baseline.analyze(log, days=30)
        # 約 10 分鐘 (now - 600s) — 容忍 ±0.2 min
        assert 9.5 <= r["per_user"]["u1"]["total_minutes"] <= 10.5, \
            f"dangling 算 ~10min, got {r['per_user']['u1']['total_minutes']}"


def t_baseline_move_split_session():
    """move 視為舊 channel leave + 新 channel join。"""
    now = time.time()
    with tempfile.TemporaryDirectory() as td:
        log = _write_presence_log(Path(td), [
            {"ts": now - 600, "user_id": "u1", "user_name": "alice",
             "event": "join", "channel_id": "c1", "is_bot": False, "guild_id": "g1", "channel_name": "general"},
            {"ts": now - 300, "user_id": "u1", "user_name": "alice",
             "event": "move", "channel_id": "c2", "is_bot": False, "guild_id": "g1", "channel_name": "music"},
            {"ts": now - 60, "user_id": "u1", "user_name": "alice",
             "event": "leave", "channel_id": "c2", "is_bot": False, "guild_id": "g1", "channel_name": "music"},
        ])
        r = analyze_baseline.analyze(log, days=30)
        # 5 min in c1 + 4 min in c2 = 9 min total
        assert 8.5 <= r["per_user"]["u1"]["total_minutes"] <= 9.5, \
            f"expect ~9min, got {r['per_user']['u1']['total_minutes']}"


def t_baseline_window_days_filter():
    """--days 1 → 只看最近 1 天 events、舊 events 不入。"""
    now = time.time()
    old_day = now - 5 * 86400  # 5 天前
    with tempfile.TemporaryDirectory() as td:
        log = _write_presence_log(Path(td), [
            {"ts": old_day, "user_id": "u_old", "user_name": "bob",
             "event": "join", "channel_id": "c1", "is_bot": False, "guild_id": "g1", "channel_name": "general"},
            {"ts": old_day + 600, "user_id": "u_old", "user_name": "bob",
             "event": "leave", "channel_id": "c1", "is_bot": False, "guild_id": "g1", "channel_name": "general"},
            {"ts": now - 600, "user_id": "u_new", "user_name": "alice",
             "event": "join", "channel_id": "c1", "is_bot": False, "guild_id": "g1", "channel_name": "general"},
            {"ts": now - 60, "user_id": "u_new", "user_name": "alice",
             "event": "leave", "channel_id": "c1", "is_bot": False, "guild_id": "g1", "channel_name": "general"},
        ])
        r = analyze_baseline.analyze(log, days=1)
        assert "u_old" not in r["per_user"], "5 天前 user 不該在 days=1 window 內"
        assert "u_new" in r["per_user"]


def t_baseline_multiple_days_active():
    """User 跨多日，active_days 應正確計算。"""
    now_dt = datetime.now(timezone.utc)
    # Day 1 (10:00 UTC), Day 2 (15:00 UTC)
    day1_join = (now_dt - timedelta(days=2)).replace(hour=10, minute=0, second=0, microsecond=0).timestamp()
    day2_join = (now_dt - timedelta(days=1)).replace(hour=15, minute=0, second=0, microsecond=0).timestamp()
    with tempfile.TemporaryDirectory() as td:
        log = _write_presence_log(Path(td), [
            {"ts": day1_join, "user_id": "u1", "user_name": "alice",
             "event": "join", "channel_id": "c1", "is_bot": False, "guild_id": "g1", "channel_name": "general"},
            {"ts": day1_join + 600, "user_id": "u1", "user_name": "alice",
             "event": "leave", "channel_id": "c1", "is_bot": False, "guild_id": "g1", "channel_name": "general"},
            {"ts": day2_join, "user_id": "u1", "user_name": "alice",
             "event": "join", "channel_id": "c1", "is_bot": False, "guild_id": "g1", "channel_name": "general"},
            {"ts": day2_join + 1200, "user_id": "u1", "user_name": "alice",
             "event": "leave", "channel_id": "c1", "is_bot": False, "guild_id": "g1", "channel_name": "general"},
        ])
        r = analyze_baseline.analyze(log, days=30)
        assert r["per_user"]["u1"]["active_days"] == 2
        assert 28 <= r["per_user"]["u1"]["total_minutes"] <= 32  # 10 + 20 = 30


def t_baseline_per_day_active_users():
    """per_day_active_users 應該按日彙整 unique users。"""
    now_dt = datetime.now(timezone.utc)
    today_join = now_dt.replace(hour=12, minute=0, second=0, microsecond=0).timestamp()
    with tempfile.TemporaryDirectory() as td:
        log = _write_presence_log(Path(td), [
            {"ts": today_join, "user_id": "u1", "user_name": "alice",
             "event": "join", "channel_id": "c1", "is_bot": False, "guild_id": "g1", "channel_name": "general"},
            {"ts": today_join + 600, "user_id": "u1", "user_name": "alice",
             "event": "leave", "channel_id": "c1", "is_bot": False, "guild_id": "g1", "channel_name": "general"},
            {"ts": today_join + 60, "user_id": "u2", "user_name": "bob",
             "event": "join", "channel_id": "c1", "is_bot": False, "guild_id": "g1", "channel_name": "general"},
            {"ts": today_join + 700, "user_id": "u2", "user_name": "bob",
             "event": "leave", "channel_id": "c1", "is_bot": False, "guild_id": "g1", "channel_name": "general"},
        ])
        r = analyze_baseline.analyze(log, days=30)
        today_key = datetime.fromtimestamp(today_join, tz=timezone.utc).strftime("%Y-%m-%d")
        users_today = r["per_day_active_users"].get(today_key, [])
        assert "alice" in users_today and "bob" in users_today


# ── Run ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== M7 cost_alarm + baseline_analyzer standalone tests ===\n")

    print("cost_alarm:")
    run("empty log → no alarm", t_cost_alarm_empty_log)
    run("today breakdown by caller", t_cost_alarm_today_breakdown)
    run("daily threshold trips alarm", t_cost_alarm_daily_threshold_trips)
    run("yesterday not in today", t_cost_alarm_yesterday_not_in_today)
    print()

    print("baseline_analyzer:")
    run("no log file → error", t_baseline_no_log)
    run("empty log → error", t_baseline_empty_log)
    run("simple join/leave 10min", t_baseline_simple_join_leave)
    run("filters is_bot=True", t_baseline_filters_bots)
    run("dangling open session counted", t_baseline_dangling_open_session)
    run("move splits session", t_baseline_move_split_session)
    run("--days window filters", t_baseline_window_days_filter)
    run("multi-day active_days correct", t_baseline_multiple_days_active)
    run("per_day_active_users aggregates", t_baseline_per_day_active_users)

    print()
    print(f"=== Results: {PASSED} passed, {FAILED} failed ===")
    if FAILED:
        print("\n--- Failures ---")
        for name, tb in FAILURES:
            print(f"\n{name}:")
            print(tb)
        sys.exit(1)
