"""測試 scripts/paid_cost_report.py 付費成本報表工具。

涵蓋：
- 月總和 (est_usd, total_calls, total_tokens)
- 跨月資料排除
- 日分組與 Asia/Taipei 時區切日（UTC 16:30 → 台北隔天 00:30）
- caller 排序
- CLI 輸出
"""
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from scripts.paid_cost_report import generate_report


def test_paid_cost_report_aggregation(tmp_path):
    log_file = tmp_path / "llm_paid_usage.jsonl"

    # 時區切日測試點：
    # 2026-10-01 15:00 UTC = 2026-10-01 23:00 Taipei (Day 1)
    # 2026-10-01 16:30 UTC = 2026-10-02 00:30 Taipei (Day 2)
    # 2026-10-02 04:00 UTC = 2026-10-02 12:00 Taipei (Day 2)
    # 2026-09-30 15:00 UTC = 2026-09-30 23:00 Taipei (上個月，應排除)
    dt1 = datetime(2026, 10, 1, 15, 0, tzinfo=timezone.utc).timestamp()
    dt2 = datetime(2026, 10, 1, 16, 30, tzinfo=timezone.utc).timestamp()
    dt3 = datetime(2026, 10, 2, 4, 0, tzinfo=timezone.utc).timestamp()
    dt_last_month = datetime(2026, 9, 30, 15, 0, tzinfo=timezone.utc).timestamp()

    records = [
        # 當月 Day 1: caller_a
        {"ts": dt1, "caller": "caller_a", "model": "m1", "tokens": 100, "est_usd": 0.01},
        # 當月 Day 2: caller_a (UTC 16:30 -> 台北 10-02 00:30)
        {"ts": dt2, "caller": "caller_a", "model": "m1", "tokens": 200, "est_usd": 0.02},
        # 當月 Day 2: caller_b
        {"ts": dt3, "caller": "caller_b", "model": "m2", "tokens": 300, "est_usd": 0.05},
        # 上個月: caller_b (應被排除)
        {"ts": dt_last_month, "caller": "caller_b", "model": "m2", "tokens": 999, "est_usd": 1.00},
    ]

    log_file.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

    report = generate_report(log_file, month="2026-10")

    # 1. 月總和
    assert report["month"] == "2026-10"
    assert report["total_calls"] == 3
    assert report["total_tokens"] == 600
    assert pytest.approx(report["total_est_usd"], 0.0001) == 0.08

    # 2. 日分組（驗證時區切日）
    daily = report["daily_usd"]
    assert "2026-10-01" in daily
    assert "2026-10-02" in daily
    assert pytest.approx(daily["2026-10-01"], 0.0001) == 0.01
    assert pytest.approx(daily["2026-10-02"], 0.0001) == 0.07  # dt2 (0.02) + dt3 (0.05)

    # 3. caller 排序（依 est_usd 降序）
    callers = report["top_callers"]
    assert len(callers) == 2
    # caller_b: 0.05, 1 筆, 300 tokens
    # caller_a: 0.03, 2 筆, 300 tokens
    assert callers[0]["caller"] == "caller_b"
    assert pytest.approx(callers[0]["est_usd"], 0.0001) == 0.05
    assert callers[0]["calls"] == 1
    assert callers[1]["caller"] == "caller_a"
    assert pytest.approx(callers[1]["est_usd"], 0.0001) == 0.03
    assert callers[1]["calls"] == 2


def test_paid_cost_report_cli(tmp_path):
    log_file = tmp_path / "llm_paid_usage.jsonl"
    dt = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc).timestamp()
    rec = {"ts": dt, "caller": "daily_review", "model": "m1", "tokens": 1000, "est_usd": 0.05}
    log_file.write_text(json.dumps(rec) + "\n", encoding="utf-8")

    script_path = Path(__file__).resolve().parent.parent / "scripts" / "paid_cost_report.py"

    res = subprocess.run(
        [sys.executable, str(script_path), "--path", str(log_file), "--month", "2026-10"],
        capture_output=True,
        text=True,
        check=True,
    )
    stdout = res.stdout
    assert "2026-10" in stdout
    assert "0.05" in stdout
    assert "daily_review" in stdout
