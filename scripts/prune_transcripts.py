"""寬放 ZDR：清除 marvin.db transcripts 表超過 14 天的原文。

為何安全（2026-06-01 查證）：live bot 讀 raw transcript 的最長回看是 profile_compressor
的 7 天，其餘消費端（mood/topic/recall/summarizer）都是分鐘級。14 天 prune 不影響任何
即時行為。跨週的長期語意記憶由向量庫負責（不在此表）。

SQLite DELETE 與運行中的 bot 並發安全（per-call 連線 + 短暫鎖；3am 低活躍）。
輸出 JSON 摘要到 stdout。用法：python scripts/prune_transcripts.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from transcript_store import TranscriptStore  # noqa: E402

DB_PATH = "marvin.db"
RETENTION_DAYS = 14


def main() -> int:
    if not Path(DB_PATH).exists():
        print(f"db not found: {DB_PATH}", file=sys.stderr)
        return 1
    deleted = TranscriptStore(db_path=DB_PATH).prune(retention_days=RETENTION_DAYS)
    print(json.dumps({"db": DB_PATH, "retention_days": RETENTION_DAYS,
                      "deleted_rows": deleted}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
