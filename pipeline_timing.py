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

# E1：主 LLM 路徑分段打點 stage 清單（IntentBus 結束 → 問句開場 → 主 LLM 組 prompt →
# 雲端首字 → 第一句），跟 _STAGES（VAD→STT→Cleaner→Intent）是兩條獨立量測軸，
# 各自的 emit() / emit_llm() 分開印行、分開落 jsonl（kind 欄區分）。
_LLM_STAGES = ("dequeued", "opener_start", "opener_done", "llm_start", "ctx_done",
               "search_done", "rpm_done", "first_chunk", "first_sentence")


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


def mark_once(stage: str) -> None:
    """同 mark，但已有該 stage 就不覆蓋（多個 return 路徑都呼叫也只記第一次）。"""
    d = _timing.get()
    if d is not None and stage not in d:
        d[stage] = time.monotonic()


def note(key: str, value: str) -> None:
    """記一個非時間戳欄位（目前只有 llm_route）；無 context no-op。"""
    d = _timing.get()
    if d is not None:
        d[key] = value


def detach() -> None:
    """讓目前 task 之後的 mark/mark_once 不再寫進共用 dict（給 speculative prefetch
    用：它是自己的 task，不該把 llm_start/first_chunk 寫進主路徑那份 timing dict）。"""
    _timing.set(None)


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


def build_llm_row(d: dict | None, speaker: str) -> dict | None:
    """純函式：主 LLM 路徑 timing dict → 結構化 jsonl row（無 context / 無 endpoint 回 None）。

    route 來源：d["llm_route"]（note() 寫入的 "cloud" / "cloud→fallback" / "stream_llm"）；
    沒有 llm_route 且沒有 llm_start → 這段 timing 其實是 speculative prefetch 殘留
    （detach() 本該擋掉，雙重保險）→ "prefetch"；有 llm_start 但沒 llm_route → "?"。
    """
    if d is None or "endpoint" not in d:
        return None
    ep = d["endpoint"]
    route = d.get("llm_route") or ("prefetch" if "llm_start" not in d else "?")
    return {
        "ts": time.time(),
        "kind": "llm",
        "speaker": speaker,
        "route": route,
        "stages": {s: round((d[s] - ep) * 1000, 1) for s in _LLM_STAGES if s in d},
    }


def emit_llm(speaker: str) -> None:
    """Print 一行 [LLM_STAGE] + append jsonl（kind=llm）。遙測絕不可炸斷 dispatch。"""
    try:
        d = _timing.get()
        row = build_llm_row(d, speaker)
        if row is None:
            return
        ep = d["endpoint"]
        parts = []
        for s in _LLM_STAGES:
            if s in d:
                tag = s.replace("_", "")
                parts.append(f"{tag}={(d[s] - ep) * 1000:.0f}ms")
        print(
            f"[LLM_STAGE] speaker={speaker} route={row['route']} " + " ".join(parts),
            flush=True,
        )
        _append_timing_jsonl(row)
    except Exception:
        pass


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
