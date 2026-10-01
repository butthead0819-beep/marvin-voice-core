"""
scripts/prune_retention.py — Phase 1 Stage D 資料保留期限清理工具。

依據 Jack 2026-10-01 拍板之保留規則：
1. speaker_topic_graph: 30 天 (created_at < now-30d)
2. session_summaries: 30 天 (created_at < now-30d)
3. tasks: status IN ('done', 'cancelled') 且 created_at < now-30d；pending 永久保留
4. records/daily/: 符合 ^(stt_|topic_stats_)?\\d{4}-\\d{2}-\\d{2}\\.(log|json)$ 且日期 < 今天(Asia/Taipei)-14 天
5. voice_presence.jsonl: ts < now-90d（原子改寫，不留 .bak）

預設為 dry-run，只有加 --apply 才會執行刪除。
遵守 memory_sandbox.active()：沙盒中強制為 no-op。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import memory_sandbox  # noqa: E402

TAIPEI_TZ = timezone(timedelta(hours=8))
DAILY_PATTERN = re.compile(r"^(?:stt_|topic_stats_)?(\d{4}-\d{2}-\d{2})\.(?:log|json)$")


def prune_speaker_topic_graph(conn: sqlite3.Connection, cutoff_ts: float, apply: bool) -> dict:
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*), MIN(created_at) FROM speaker_topic_graph WHERE created_at < ?", (cutoff_ts,))
    matched, oldest = cur.fetchone()
    deleted = 0
    if apply and matched:
        cur.execute("DELETE FROM speaker_topic_graph WHERE created_at < ?", (cutoff_ts,))
        conn.commit()
        deleted = cur.rowcount
    return {"matched": matched or 0, "oldest": oldest, "deleted": deleted}


def prune_session_summaries(conn: sqlite3.Connection, cutoff_ts: float, apply: bool) -> dict:
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*), MIN(created_at) FROM session_summaries WHERE created_at < ?", (cutoff_ts,))
    matched, oldest = cur.fetchone()
    deleted = 0
    if apply and matched:
        cur.execute("DELETE FROM session_summaries WHERE created_at < ?", (cutoff_ts,))
        conn.commit()
        deleted = cur.rowcount
    return {"matched": matched or 0, "oldest": oldest, "deleted": deleted}


def prune_tasks(conn: sqlite3.Connection, cutoff_ts: float, apply: bool) -> dict:
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*), MIN(created_at) FROM tasks WHERE status IN ('done', 'cancelled') AND created_at < ?",
        (cutoff_ts,),
    )
    matched, oldest = cur.fetchone()
    deleted = 0
    if apply and matched:
        cur.execute(
            "DELETE FROM tasks WHERE status IN ('done', 'cancelled') AND created_at < ?",
            (cutoff_ts,),
        )
        conn.commit()
        deleted = cur.rowcount
    return {"matched": matched or 0, "oldest": oldest, "deleted": deleted}


def prune_daily_records(daily_dir: Path, today_taipei: date, retention_days: int, apply: bool) -> dict:
    if not daily_dir.exists():
        return {"matched": 0, "oldest": None, "deleted": 0}

    cutoff_date = today_taipei - timedelta(days=retention_days)
    expired_files: list[tuple[date, Path]] = []

    for item in daily_dir.iterdir():
        if not item.is_file():
            continue
        m = DAILY_PATTERN.match(item.name)
        if not m:
            continue
        date_str = m.group(1)
        try:
            fdate = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        if fdate < cutoff_date:
            expired_files.append((fdate, item))

    matched = len(expired_files)
    oldest = min((f[0].isoformat() for f in expired_files), default=None)
    deleted = 0

    if apply and matched:
        for _, path in expired_files:
            try:
                path.unlink()
                deleted += 1
            except OSError:
                pass

    return {"matched": matched, "oldest": oldest, "deleted": deleted}


def prune_presence_log(presence_path: Path, cutoff_ts: float, apply: bool) -> dict:
    if not presence_path.exists():
        return {"matched": 0, "oldest": None, "deleted": 0}

    retained_lines: list[str] = []
    matched = 0
    oldest_ts: float | None = None

    with presence_path.open("r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                rec = json.loads(stripped)
            except Exception:
                retained_lines.append(line)
                continue

            ts = rec.get("ts")
            if isinstance(ts, (int, float)) and ts < cutoff_ts:
                matched += 1
                if oldest_ts is None or ts < oldest_ts:
                    oldest_ts = ts
            else:
                retained_lines.append(line)

    deleted = 0
    if apply and matched:
        # 不留 .bak：每天跑都備份一份會把過期資料永久留下，清理白做（第一次清理前的備份由營運者手動做）
        tmp_path = presence_path.with_suffix(presence_path.suffix + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            for line in retained_lines:
                f.write(line if line.endswith("\n") else line + "\n")
        os.replace(tmp_path, presence_path)
        deleted = matched

    return {"matched": matched, "oldest": oldest_ts, "deleted": deleted}


def prune_all(
    db_path: Path,
    daily_dir: Path,
    presence_path: Path,
    now: float | None = None,
    apply: bool = False,
) -> dict:
    if memory_sandbox.active():
        apply = False

    effective_now = time.time() if now is None else float(now)
    cutoff_30d = effective_now - 30 * 86400
    cutoff_90d = effective_now - 90 * 86400
    today_taipei = datetime.fromtimestamp(effective_now, tz=TAIPEI_TZ).date()

    db_res_stg = {"matched": 0, "oldest": None, "deleted": 0}
    db_res_sum = {"matched": 0, "oldest": None, "deleted": 0}
    db_res_tasks = {"matched": 0, "oldest": None, "deleted": 0}

    if db_path.exists():
        conn = sqlite3.connect(db_path)
        try:
            db_res_stg = prune_speaker_topic_graph(conn, cutoff_30d, apply)
            db_res_sum = prune_session_summaries(conn, cutoff_30d, apply)
            db_res_tasks = prune_tasks(conn, cutoff_30d, apply)
        finally:
            conn.close()

    daily_res = prune_daily_records(daily_dir, today_taipei, retention_days=14, apply=apply)
    presence_res = prune_presence_log(presence_path, cutoff_90d, apply=apply)

    return {
        "dry_run": not apply,
        "speaker_topic_graph": db_res_stg,
        "session_summaries": db_res_sum,
        "tasks": db_res_tasks,
        "daily_files": daily_res,
        "voice_presence": presence_res,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 1 Stage D: 資料保留清理腳本")
    parser.add_argument("--db", default="marvin.db", help="SQLite DB 路徑 (預設: marvin.db)")
    parser.add_argument("--daily-dir", default="records/daily", help="每日紀錄目錄 (預設: records/daily)")
    parser.add_argument("--presence", default="data/voice_presence.jsonl", help="語音進出紀錄 (預設: data/voice_presence.jsonl)")
    parser.add_argument("--now", type=float, default=None, help="覆寫當前時間戳 (測試用)")
    parser.add_argument("--apply", action="store_true", help="確認執行刪除 (預設 dry-run)")
    args = parser.parse_args(argv)

    summary = prune_all(
        db_path=Path(args.db),
        daily_dir=Path(args.daily_dir),
        presence_path=Path(args.presence),
        now=args.now,
        apply=args.apply,
    )
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
