"""LLM dispatch metrics — append-only jsonl 給 1 週 observation 用 (Plan C9).

每次 _call_llm 完成寫一筆進 records/llm_routing.jsonl:
- ts (epoch seconds)
- route: "bus" | "legacy"
- purpose, speaker, provider, model
- latency_ms, tokens
- success (bool), error (str, "" on success)

Phase 2: 只 bus path 寫 detailed metrics. Legacy path 只記 success/latency
(不細查 provider attribution, 太侵入 gemini_router_llm 902 行).

寫檔失敗 (perm / disk full) 必須 silent return — dispatch 不能因 log 壞掉.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("MarvinBot.LLMBus.Metrics")

_LOG_PATH: Path = Path("records") / "llm_routing.jsonl"


def log_dispatch(
    *,
    route: str,
    purpose: str,
    speaker: Optional[str],
    provider: Optional[str],
    model: Optional[str],
    latency_ms: int,
    tokens: int,
    success: bool,
    error: str = "",
) -> None:
    """Append one jsonl entry. Silent on failure."""
    entry = {
        "ts": time.time(),
        "route": route,
        "purpose": purpose,
        "speaker": speaker,
        "provider": provider,
        "model": model,
        "latency_ms": int(latency_ms),
        "tokens": int(tokens),
        "success": bool(success),
        "error": error,
    }
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        # 寫檔失敗 silent - 不能壞 dispatch
        logger.debug(f"[Metrics] write failed: {e}")
