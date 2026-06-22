"""Voice pipeline stage timing — measure VAD→STT→Cleaner→Intent latency.

ContextVar-based: stages don't pass timing through function signatures.
`asyncio.create_task` copies the current context (Python 3.7+ guarantee),
so once `start()` is called inside an async frame, `mark()` / `emit()` from
downstream awaits and tasks see the same dict.

Note: `loop.call_soon_threadsafe` (used by sink to bridge thread → async)
does NOT propagate context. So `start()` must be called INSIDE the async
entry (process_audio_slice), not in the sync sink thread.

Output line shape:
  [STAGE_TIMING] speaker=狗與露 sttstart=12ms sttdone=487ms cleanerdone=1203ms intentdispatched=1208ms total=1208ms text='播放周杰倫的稻香'

Grep + awk friendly: `grep STAGE_TIMING bot_stdout.log | awk ...`
"""
from __future__ import annotations

import contextvars
import json
import os
import time

# Durable 落盤：emit() 除了印 log 行（grep/awk 友善）也 append 一行結構化 jsonl，
# 讓 queue_wait / cleaner 段延遲能跨重啟累積、像 judge_outcomes 一樣可 jq/pandas 撈。
# （2026-06-22：log 行重啟即沖、無法做延遲分布分析。）
_TIMING_LOG = "records/pipeline_timing.jsonl"

# dequeued / question_done 是 cleaner_done 之前的中間打點：把舊的單一「cleaner」段
# （= cleaner_done − stt_done）拆成 排隊(stt_done→dequeued) / 等問句(dequeued→
# question_done) / 真清洗(question_done→cleaner_done)，避免 queue-wait + evt.wait 被
# 誤算進 cleaner LLM（2026-06-05：日報「cleaner p50 7s」其實多半是排隊）。
_STAGES = ("stt_start", "stt_done", "dequeued", "question_done", "cleaner_done", "intent_dispatched")

_timing: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "pipeline_timing", default=None
)


def start() -> dict:
    """Begin a new timing record at the current async frame. Idempotent per task."""
    d: dict = {"endpoint": time.monotonic()}
    _timing.set(d)
    return d


def mark(stage: str) -> None:
    """Record a stage timestamp; no-op if no timing context started."""
    d = _timing.get()
    if d is not None:
        d[stage] = time.monotonic()


def build_timing_row(d: dict | None, speaker: str, text: str, suffix: str = "") -> dict | None:
    """Pure: timing dict → 結構化 jsonl row（無 context 回 None）。

    `stages` 是各階段 ms-from-endpoint 絕對值；分析端自行算 delta
    （queue_wait = dequeued − stt_done、cleaner = cleaner_done − dequeued）。
    """
    if d is None or "endpoint" not in d:
        return None
    ep = d["endpoint"]
    stages = {s: round((d[s] - ep) * 1000, 1) for s in _STAGES if s in d}
    total_end = d.get("intent_dispatched", max(d.values()) if stages else ep)
    route = suffix.strip()
    if route.startswith("route="):
        route = route[len("route="):]
    return {
        "ts": time.time(),
        "speaker": speaker,
        "text": (text or "")[:40],
        "route": route,
        "stages": stages,
        "total_ms": round((total_end - ep) * 1000, 1),
    }


def _append_timing_jsonl(row: dict) -> None:
    """Append 一行到 records/pipeline_timing.jsonl；遙測寫檔絕不可炸斷 dispatch。

    沿用 judge_outcomes 同套防污染：pytest 下（PYTEST_CURRENT_TEST）一律跳過，
    避免測試把合成資料灌進 prod 延遲遙測。
    """
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    try:
        os.makedirs("records", exist_ok=True)
        with open(_TIMING_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def emit(speaker: str, text: str, suffix: str = "") -> None:
    """Print one [STAGE_TIMING] line + append durable jsonl. Silent if no timing context."""
    d = _timing.get()
    if d is None or "endpoint" not in d:
        return
    ep = d["endpoint"]
    parts = []
    for s in _STAGES:
        if s in d:
            tag = s.replace("_", "")
            parts.append(f"{tag}={(d[s] - ep) * 1000:.0f}ms")
    total_end = d.get("intent_dispatched", time.monotonic())
    total_ms = (total_end - ep) * 1000
    snippet = (text or "")[:40]
    print(
        f"[STAGE_TIMING] speaker={speaker} {' '.join(parts)} "
        f"total={total_ms:.0f}ms text={snippet!r}{suffix}",
        flush=True,
    )
    row = build_timing_row(d, speaker, text, suffix)
    if row is not None:
        _append_timing_jsonl(row)


def snapshot() -> dict | None:
    """Read-only access to current timing dict (for tests + queue forwarding)."""
    return _timing.get()


def restore(d: dict | None) -> None:
    """Re-attach a timing dict captured by snapshot() in a different async task.

    ContextVar doesn't propagate across asyncio.Queue boundaries; producer stashes
    snapshot() into the queue item, consumer calls restore() after queue.get(),
    so downstream emit() sees the same endpoint and marks set by producer.
    None is a no-op (handle legacy queue items without timing).
    """
    if d is not None:
        _timing.set(d)
