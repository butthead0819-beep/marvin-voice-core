"""測試 scripts/rotate_launchd_logs.py 的 copytruncate 與 gzip 輪替。"""
import gzip
import subprocess
import sys
from pathlib import Path


def test_rotate_launchd_logs_dry_run_and_apply(tmp_path):
    log_file = tmp_path / "bot_stdout.log"
    log_file.write_text("Hello log content line 1\nHello log content line 2\n")

    script_path = Path(__file__).resolve().parent.parent / "scripts" / "rotate_launchd_logs.py"

    # --- 1. Dry run ---
    res_dry = subprocess.run(
        [sys.executable, str(script_path), "--log", str(log_file)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "[DRY-RUN]" in res_dry.stdout or "dry_run" in res_dry.stdout or "會輪替" in res_dry.stdout
    # 原檔大小不變
    assert log_file.stat().st_size > 0
    # 沒有產生 .gz
    assert len(list(tmp_path.glob("*.gz"))) == 0

    # --- 2. Apply ---
    res_apply = subprocess.run(
        [sys.executable, str(script_path), "--log", str(log_file), "--apply"],
        capture_output=True,
        text=True,
        check=True,
    )
    # 原檔仍存在但大小為 0
    assert log_file.exists()
    assert log_file.stat().st_size == 0

    # 產生了 .gz
    gzs = list(tmp_path.glob("bot_stdout.log.*.gz"))
    assert len(gzs) == 1
    with gzip.open(gzs[0], "rt", encoding="utf-8") as f:
        content = f.read()
    assert content == "Hello log content line 1\nHello log content line 2\n"


def test_rotate_launchd_logs_retention_limit(tmp_path):
    """驗證超過 14 份的舊 .gz 會被刪除，保留最新 14 份。"""
    log_file = tmp_path / "bot_stdout.log"
    log_file.write_text("New active log")

    # 預先建立 14 份舊 .gz (日期 20260901 ~ 20260914)
    for day in range(1, 15):
        d_str = f"202609{day:02d}"
        gz_path = tmp_path / f"bot_stdout.log.{d_str}.gz"
        with gzip.open(gz_path, "wt", encoding="utf-8") as f:
            f.write(f"log from {d_str}")

    assert len(list(tmp_path.glob("bot_stdout.log.*.gz"))) == 14

    script_path = Path(__file__).resolve().parent.parent / "scripts" / "rotate_launchd_logs.py"

    # 執行 apply (產生第 15 份，今天例如 20261001)
    subprocess.run(
        [sys.executable, str(script_path), "--log", str(log_file), "--apply"],
        capture_output=True,
        text=True,
        check=True,
    )

    # 總份數依然維持 14 份
    all_gzs = sorted(tmp_path.glob("bot_stdout.log.*.gz"))
    assert len(all_gzs) == 14
    # 最舊的那份 (20260901) 被刪除
    assert not (tmp_path / "bot_stdout.log.20260901.gz").exists()
    # 20260902 還在
    assert (tmp_path / "bot_stdout.log.20260902.gz").exists()


def test_rotate_zero_byte_skipped(tmp_path):
    """0 bytes 的原檔跳過不輪替。"""
    log_file = tmp_path / "empty.log"
    log_file.write_text("")

    script_path = Path(__file__).resolve().parent.parent / "scripts" / "rotate_launchd_logs.py"
    subprocess.run(
        [sys.executable, str(script_path), "--log", str(log_file), "--apply"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert len(list(tmp_path.glob("*.gz"))) == 0
