"""每日任務看門狗：log 新鮮度 + 失敗標記檢查。"""
import os
from scripts.cron_watchdog import check_cron_health


def _log(tmp, name, text, age_h=0.0):
    p = tmp / name
    p.write_text(text, encoding="utf-8")
    if age_h:
        old = (p.stat().st_mtime) - age_h * 3600
        os.utime(p, (old, old))
    return str(p)


def test_healthy_fresh_success_log_no_problem(tmp_path):
    log = _log(tmp_path, "ok.log", "INFO ...\n[task] ✅ success\n", age_h=1)
    assert check_cron_health([{"name": "ok", "log": log, "max_age_h": 36}], now_ts=__import__("time").time()) == []


def test_stale_log_flagged(tmp_path):
    log = _log(tmp_path, "old.log", "✅ success\n", age_h=50)
    probs = check_cron_health([{"name": "slice", "log": log, "max_age_h": 36}], now_ts=__import__("time").time())
    assert probs and "slice" in probs[0] and "沒更新" in probs[0]


def test_failure_marker_flagged(tmp_path):
    log = _log(tmp_path, "rev.log", "⚠ attempt 3 exit=1\n[run] ❌ all 3 attempts failed\n", age_h=1)
    probs = check_cron_health([{"name": "review", "log": log, "max_age_h": 36}], now_ts=__import__("time").time())
    assert probs and "review" in probs[0]


def test_missing_log_flagged(tmp_path):
    probs = check_cron_health([{"name": "gone", "log": str(tmp_path / "nope.log"), "max_age_h": 36}], now_ts=__import__("time").time())
    assert probs and "不存在" in probs[0]
