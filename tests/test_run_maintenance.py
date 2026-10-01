"""測試 scripts/run_maintenance.py 與 cron_watchdog 整合。"""
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest


def test_run_maintenance_all_success(monkeypatch, capsys):
    from scripts.run_maintenance import run_maintenance

    called_cmds = []

    def mock_run(cmd, capture_output=True, text=True, timeout=None):
        called_cmds.append(cmd)
        res = MagicMock()
        res.returncode = 0
        res.stdout = "line 1\nstep output summary\n"
        res.stderr = ""
        return res

    monkeypatch.setattr(subprocess, "run", mock_run)

    rc = run_maintenance(apply=True)
    assert rc == 0
    assert len(called_cmds) == 4
    # 每一步都帶有 --apply
    for cmd in called_cmds:
        assert "--apply" in cmd

    captured = capsys.readouterr().out
    assert "[maintenance] ✅ done" in captured
    assert "step output summary" in captured


def test_run_maintenance_partial_failure_continues_and_exits_1(monkeypatch, capsys):
    from scripts.run_maintenance import run_maintenance

    called_steps = []

    def mock_run(cmd, capture_output=True, text=True, timeout=None):
        script_name = Path(cmd[1]).name
        called_steps.append(script_name)
        res = MagicMock()
        if "prune_transcripts" in script_name:
            res.returncode = 1
            res.stdout = "error details"
        else:
            res.returncode = 0
            res.stdout = "ok"
        res.stderr = ""
        return res

    monkeypatch.setattr(subprocess, "run", mock_run)

    rc = run_maintenance(apply=False)
    assert rc == 1
    # 儘管第二步失敗，四步仍都跑完
    assert len(called_steps) == 4

    captured = capsys.readouterr().out
    assert "all attempts failed: prune_transcripts" in captured


def test_cron_watchdog_includes_maintenance(tmp_path):
    from scripts.cron_watchdog import check_cron_health, CHECKS

    # 確認 CHECKS 包含 maintenance
    m_check = next((c for c in CHECKS if c["name"] == "maintenance"), None)
    assert m_check is not None
    assert m_check["max_age_h"] == 36

    # 模擬 log 過舊
    log_file = tmp_path / "maintenance_cron.log"
    log_file.write_text("ok")
    # 設 mtime 為 40 小時前
    now = 1000000.0
    old_time = now - 40 * 3600
    import os
    os.utime(log_file, (old_time, old_time))

    custom_checks = [{"name": "maintenance", "log": str(log_file), "max_age_h": 36}]
    problems = check_cron_health(custom_checks, now_ts=now)
    assert len(problems) == 1
    assert "maintenance" in problems[0]
    assert "沒更新" in problems[0]
