"""RescueOutcomeLogger — IntentBus rescue_outcome_sink 的 JSONL 實作。

無 dedup（每筆 rescue 結果都對 daily ritual 有分析價值）；無 rotation（
records/ 目錄日累積，由 daily ritual 讀完後 archive，與 judge_outcomes 同款）。

Caller 注意：write 在 sync 路徑跑，但 disk IO 在 voice_controller 的場景
（每次 rescue 一筆 = 數秒一次）下沒有 latency 顧慮，不需 background task。
"""
from __future__ import annotations

import json
from pathlib import Path


class RescueOutcomeLogger:
    def __init__(self, jsonl_path: Path | str):
        self.jsonl_path = Path(jsonl_path)

    def write(self, record: dict) -> None:
        self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        with self.jsonl_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
