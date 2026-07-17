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


# ── artifact_glob：盯產物而非備援路徑的 log ────────────────────────────────
#
# 2026-07-17：dailyreview 誤報 98h 沒跑，實際天天有產出。根因＝review 改成 summon
# 觸發後，launchd 12:05 備援撞 once/day guard 直接跳過 → 只有備援會寫的
# review_cron.log 永遠不更新。看門狗該盯「報告有沒有生出來」，不是「備援有沒有跑」。

def _artifact(tmp, name, age_h=0.0):
    p = tmp / name
    p.write_text("# report\n", encoding="utf-8")
    if age_h:
        old = p.stat().st_mtime - age_h * 3600
        os.utime(p, (old, old))
    return p


def test_fresh_artifact_passes_even_when_log_is_stale(tmp_path):
    """產物是新的 → 不告警，即使備援 log 很舊（就是 dailyreview 的誤報情境）。"""
    log = _log(tmp_path, "review_cron.log", "✅ success\n", age_h=98)
    _artifact(tmp_path, "quality_metrics_2026-07-17.md", age_h=6)
    probs = check_cron_health([{
        "name": "dailyreview", "log": log, "max_age_h": 36,
        "artifact_glob": str(tmp_path / "quality_metrics_*.md"),
    }], now_ts=__import__("time").time())
    assert probs == [], f"產物新鮮不該告警: {probs!r}"


def test_stale_artifact_flagged(tmp_path):
    """產物真的舊了 → 告警（真出事時仍要抓到）。"""
    log = _log(tmp_path, "review_cron.log", "✅ success\n", age_h=1)
    _artifact(tmp_path, "quality_metrics_2026-07-10.md", age_h=80)
    probs = check_cron_health([{
        "name": "dailyreview", "log": log, "max_age_h": 36,
        "artifact_glob": str(tmp_path / "quality_metrics_*.md"),
    }], now_ts=__import__("time").time())
    assert probs and "dailyreview" in probs[0] and "沒產出" in probs[0]


def test_artifact_uses_newest_match(tmp_path):
    """多份產物 → 看最新那份（舊的不該把新的拖下水）。"""
    log = _log(tmp_path, "review_cron.log", "✅ success\n", age_h=1)
    _artifact(tmp_path, "quality_metrics_2026-07-10.md", age_h=200)
    _artifact(tmp_path, "quality_metrics_2026-07-17.md", age_h=2)
    probs = check_cron_health([{
        "name": "dailyreview", "log": log, "max_age_h": 36,
        "artifact_glob": str(tmp_path / "quality_metrics_*.md"),
    }], now_ts=__import__("time").time())
    assert probs == [], f"最新產物新鮮就該過: {probs!r}"


def test_no_artifact_at_all_flagged(tmp_path):
    log = _log(tmp_path, "review_cron.log", "✅ success\n", age_h=1)
    probs = check_cron_health([{
        "name": "dailyreview", "log": log, "max_age_h": 36,
        "artifact_glob": str(tmp_path / "quality_metrics_*.md"),
    }], now_ts=__import__("time").time())
    assert probs and "沒產出" in probs[0]


def test_artifact_check_still_catches_failure_marker(tmp_path):
    """盯產物不代表放過失敗標記：log 有整體失敗仍要報。"""
    log = _log(tmp_path, "review_cron.log", "[run] ❌ all 3 attempts failed\n", age_h=1)
    _artifact(tmp_path, "quality_metrics_2026-07-17.md", age_h=2)
    probs = check_cron_health([{
        "name": "dailyreview", "log": log, "max_age_h": 36,
        "artifact_glob": str(tmp_path / "quality_metrics_*.md"),
    }], now_ts=__import__("time").time())
    assert probs and "失敗標記" in probs[0]


# ── 實際 CHECKS 設定 ──────────────────────────────────────────────────────

def test_dailyreview_check_watches_artifact_not_backup_log():
    from scripts.cron_watchdog import CHECKS
    rev = next(c for c in CHECKS if c["name"] == "dailyreview")
    assert "quality_metrics_" in rev.get("artifact_glob", ""), \
        "dailyreview 應盯產物（summon 觸發時備援 log 不會更新）"


def test_feedbackbatch_removed_from_checks():
    """feedbackbatch 2026-07-09 已退役（plist 改 .disabled）→ 不該再營。"""
    from scripts.cron_watchdog import CHECKS
    assert not any(c["name"] == "feedbackbatch" for c in CHECKS)
