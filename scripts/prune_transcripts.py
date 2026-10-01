"""寬放 ZDR：清除 marvin.db transcripts 表超過 14 天的原文。

為何安全（2026-06-01 查證）：live bot 讀 raw transcript 的最長回看是 profile_compressor
的 7 天，其餘消費端（mood/topic/recall/summarizer）都是分鐘級。14 天 prune 不影響任何
即時行為。跨週的長期語意記憶由向量庫負責（不在此表）。

SQLite DELETE 與運行中的 bot 並發安全（per-call 連線 + 短暫鎖；3am 低活躍）。
輸出 JSON 摘要到 stdout。用法：python scripts/prune_transcripts.py [--apply]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from transcript_store import TranscriptStore  # noqa: E402

DB_PATH = "marvin.db"
RETENTION_DAYS = 14


def main() -> int:
    parser = argparse.ArgumentParser(description="清除 marvin.db transcripts 超過指定天數的逐字稿")
    parser.add_argument("--db", default=DB_PATH, help="SQLite 資料庫路徑")
    parser.add_argument("--days", type=int, default=RETENTION_DAYS, help="保留天數（預設 14）")
    parser.add_argument("--apply", action="store_true", help="確認執行刪除（預設為 dry-run）")
    args = parser.parse_args()

    if not Path(args.db).exists():
        print(f"db not found: {args.db}", file=sys.stderr)
        return 1

    cutoff = time.time() - args.days * 86400

    if not args.apply:
        conn = sqlite3.connect(args.db)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*), MIN(timestamp) FROM transcripts WHERE timestamp < ?", (cutoff,))
        matched_count, oldest_ts = cur.fetchone()
        conn.close()
        print(
            json.dumps(
                {
                    "db": args.db,
                    "retention_days": args.days,
                    "dry_run": True,
                    "matched_rows": matched_count or 0,
                    "oldest_timestamp": oldest_ts,
                },
                ensure_ascii=False,
            )
        )
    else:
        deleted = TranscriptStore(db_path=args.db).prune(retention_days=args.days)
        print(
            json.dumps(
                {
                    "db": args.db,
                    "retention_days": args.days,
                    "dry_run": False,
                    "deleted_rows": deleted,
                },
                ensure_ascii=False,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
