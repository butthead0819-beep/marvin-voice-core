"""DJ 串場觀察紀錄 — append-only jsonl，供一週觀察用 (9/30 使用者定)。

每段串場寫一筆進 records/dj_narration.jsonl，記主題(mode/topic)、素材(ctx/
song_material)、LLM 原始產出、清雜訊結果、最終口白、口白來源、真實秒數。

寫檔失敗 (perm / disk full) 必須 silent return — DJ 生成不能因 log 壞掉。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("MarvinBot.DJ.NarrationLog")

_LOG_PATH: Path = Path("records") / "dj_narration.jsonl"


def log_dj_narration(record: dict) -> None:
    """Append one jsonl entry（自動補 "ts": time.time()）。Silent on failure。"""
    entry = {"ts": time.time(), **record}
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.debug(f"[DJ Narration Log] write failed: {e}")


async def probe_audio_seconds(path: str | None, timeout_s: float = 3.0) -> float | None:
    """ffprobe 量音檔真實秒數；path 空/不存在/ffprobe 失敗/逾時 → None。"""
    if not path or not os.path.exists(path):
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "quiet", "-show_entries", "format=duration",
            "-of", "csv=p=0", path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
    except Exception:
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout_s)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
        return None
    try:
        return round(float(out), 2)
    except Exception:
        return None
