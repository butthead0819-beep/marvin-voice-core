"""TDD — STT 健康監控

設計：
- STT listener 每處理完一個 chunk 就 touch heartbeat 檔
- check_stt_health.py 讀取 heartbeat mtime，過期 → exit 非 0 + 印警告
- 直播主可手動跑或 cron 定時跑

驗項：
A) HEARTBEAT_PATH 常數定義在 stt_listener
B) touch_heartbeat() 會更新 mtime
C) check_stt_health.py：檔不存在 → exit 2
D) check_stt_health.py：mtime > N 秒 → exit 1
E) check_stt_health.py：mtime < N 秒 → exit 0
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))


def test_heartbeat_path_constant():
    from twitch_stt_listener import HEARTBEAT_PATH
    assert isinstance(HEARTBEAT_PATH, Path)


def test_touch_heartbeat_updates_mtime(tmp_path, monkeypatch):
    import twitch_stt_listener as stt
    hb = tmp_path / "hb"
    monkeypatch.setattr(stt, "HEARTBEAT_PATH", hb)
    stt.touch_heartbeat()
    assert hb.exists()
    mt1 = hb.stat().st_mtime
    time.sleep(0.05)
    stt.touch_heartbeat()
    mt2 = hb.stat().st_mtime
    assert mt2 >= mt1


def test_check_stt_health_missing_file_returns_2(tmp_path):
    script = SCRIPTS_DIR / "check_stt_health.py"
    env = os.environ.copy()
    env["MARVIN_STT_HEARTBEAT"] = str(tmp_path / "missing-hb")
    r = subprocess.run([sys.executable, str(script)], env=env, capture_output=True, text=True)
    assert r.returncode == 2
    assert "missing" in r.stdout.lower() or "missing" in r.stderr.lower() or "找不到" in r.stdout


def test_check_stt_health_stale_returns_1(tmp_path):
    script = SCRIPTS_DIR / "check_stt_health.py"
    hb = tmp_path / "hb"
    hb.write_text("ok")
    # 故意把 mtime 設到 10 分鐘前
    old = time.time() - 600
    os.utime(hb, (old, old))
    env = os.environ.copy()
    env["MARVIN_STT_HEARTBEAT"] = str(hb)
    env["MARVIN_STT_STALE_SECONDS"] = "300"
    r = subprocess.run([sys.executable, str(script)], env=env, capture_output=True, text=True)
    assert r.returncode == 1, f"應 exit 1，得到 {r.returncode}: stdout={r.stdout!r} stderr={r.stderr!r}"
    out = (r.stdout + r.stderr).lower()
    assert "stale" in out or "過期" in r.stdout


def test_check_stt_health_fresh_returns_0(tmp_path):
    script = SCRIPTS_DIR / "check_stt_health.py"
    hb = tmp_path / "hb"
    hb.write_text("ok")
    env = os.environ.copy()
    env["MARVIN_STT_HEARTBEAT"] = str(hb)
    env["MARVIN_STT_STALE_SECONDS"] = "300"
    r = subprocess.run([sys.executable, str(script)], env=env, capture_output=True, text=True)
    assert r.returncode == 0, f"應 exit 0，得到 {r.returncode}: stdout={r.stdout!r} stderr={r.stderr!r}"
