"""TDD: 防線① 外部心跳 probe — 「該有輸出而沒輸出」的主動偵測。

設計要點：
  - probe 不進 bot 資料路徑（STT 直跑 bin、TTS 直打微軟）→ 天生零遙測污染
    （Alice probe 污染 82% 的前車之鑑：不進 pipeline 就不會污染）
  - 告警走 Discord REST（bot token 直打 HTTP API），bot 凍住也送得到
  - 告警去重：同 failure signature 6h 內不重發；恢復時發 recovered
"""
from __future__ import annotations

import json
import time

from scripts.pipeline_heartbeat_probe import (
    check_heartbeat_fresh,
    decide_alert,
    run_checks,
)


# ── heartbeat staleness ──────────────────────────────────────────────────────

def test_heartbeat_fresh_when_recent(tmp_path):
    p = tmp_path / "heartbeat.json"
    p.write_text(json.dumps({"ts": time.time()}))
    ok, detail = check_heartbeat_fresh(p, max_age_s=120)
    assert ok


def test_heartbeat_stale_when_old(tmp_path):
    p = tmp_path / "heartbeat.json"
    p.write_text(json.dumps({"ts": time.time() - 999}))
    ok, detail = check_heartbeat_fresh(p, max_age_s=120)
    assert not ok
    assert "999" in detail or "stale" in detail.lower()


def test_heartbeat_missing_file_is_failure(tmp_path):
    ok, detail = check_heartbeat_fresh(tmp_path / "nope.json", max_age_s=120)
    assert not ok


# ── run_checks 組裝 ──────────────────────────────────────────────────────────

def test_run_checks_collects_failures():
    checks = [
        ("stt", lambda: (True, "ok")),
        ("tts", lambda: (False, "no audio bytes")),
    ]
    failures = run_checks(checks)
    assert failures == [("tts", "no audio bytes")]


def test_run_checks_check_exception_counts_as_failure():
    """probe 自己的 bug 不能靜默——check 炸掉視同該層 fail。"""
    def boom():
        raise RuntimeError("probe bug")
    failures = run_checks([("stt", boom)])
    assert len(failures) == 1 and failures[0][0] == "stt"


# ── 告警去重狀態機 ────────────────────────────────────────────────────────────

def test_decide_alert_first_failure_alerts(tmp_path):
    state = tmp_path / "state.json"
    action = decide_alert(state, failures=[("tts", "x")], realert_after_s=6 * 3600)
    assert action == "alert"


def test_decide_alert_same_signature_within_window_suppressed(tmp_path):
    state = tmp_path / "state.json"
    decide_alert(state, failures=[("tts", "x")], realert_after_s=6 * 3600)
    action = decide_alert(state, failures=[("tts", "x")], realert_after_s=6 * 3600)
    assert action == "suppress"


def test_decide_alert_new_signature_alerts_again(tmp_path):
    state = tmp_path / "state.json"
    decide_alert(state, failures=[("tts", "x")], realert_after_s=6 * 3600)
    action = decide_alert(state, failures=[("stt", "y")], realert_after_s=6 * 3600)
    assert action == "alert"


def test_decide_alert_recovery_after_failure_notifies(tmp_path):
    state = tmp_path / "state.json"
    decide_alert(state, failures=[("tts", "x")], realert_after_s=6 * 3600)
    action = decide_alert(state, failures=[], realert_after_s=6 * 3600)
    assert action == "recovered"


def test_decide_alert_all_green_steady_state_silent(tmp_path):
    state = tmp_path / "state.json"
    action = decide_alert(state, failures=[], realert_after_s=6 * 3600)
    assert action == "silent"
