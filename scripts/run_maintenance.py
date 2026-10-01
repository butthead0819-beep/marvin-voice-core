"""
scripts/run_maintenance.py — 每日維護排程入口（03:00 由 launchd 呼叫）。

依序執行四個清理步驟：
1. scrub_improvement_raw.py
2. prune_transcripts.py
3. prune_retention.py
4. rotate_launchd_logs.py

若帶 --apply 則傳給每一步（預設 dry-run）。
任何一步失敗會繼續跑完後續步驟，但最後會印出 FAIL_MARKER 並 exit 1。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

STEPS = [
    ("scrub_improvement_raw", "scrub_improvement_raw.py"),
    ("prune_transcripts", "prune_transcripts.py"),
    ("prune_retention", "prune_retention.py"),
    ("rotate_launchd_logs", "rotate_launchd_logs.py"),
]


def run_maintenance(apply: bool = False) -> int:
    scripts_dir = Path(__file__).resolve().parent
    failed_steps: list[str] = []

    for name, script_file in STEPS:
        script_path = scripts_dir / script_file
        cmd = [sys.executable, str(script_path)]
        if apply:
            cmd.append("--apply")

        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=600,
            )
            rc = res.returncode
            # 抓取 stdout 最後非空白行
            stdout_lines = [line.strip() for line in (res.stdout or "").splitlines() if line.strip()]
            last_line = stdout_lines[-1] if stdout_lines else "(no output)"
            print(f"[maintenance] {name} rc={rc} {last_line}")

            if rc != 0:
                failed_steps.append(name)
                if res.stderr:
                    print(f"[maintenance] {name} stderr: {res.stderr.strip()}", file=sys.stderr)
        except Exception as e:
            print(f"[maintenance] {name} exception: {e}", file=sys.stderr)
            failed_steps.append(name)

    if failed_steps:
        # 印出 cron_watchdog.FAIL_MARKERS 認得的失敗標記
        print(f"all attempts failed: {', '.join(failed_steps)}")
        return 1

    print("[maintenance] ✅ done")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Marvin 每日維護入口")
    parser.add_argument("--apply", action="store_true", help="確認執行清理與輪替（預設 dry-run）")
    args = parser.parse_args(argv)

    return run_maintenance(apply=args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
