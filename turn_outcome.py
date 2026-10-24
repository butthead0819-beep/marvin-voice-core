"""
turn_outcome.py — 意圖漏斗步驟 1：每輪「有派發」的語音回合寫一筆 outcome，
每次喚醒候選被接受/被守衛擋下寫一筆 gate decision。

只加紀錄，不改任何使用者可見行為；兩個 jsonl 都不准含使用者原文
（raw_text / query）。寫檔失敗一律吞掉，不能影響對話（見 _append）。
"""
from __future__ import annotations

import asyncio
import functools
import json
import logging
import time
from contextvars import ContextVar
from pathlib import Path

logger = logging.getLogger(__name__)

OUTCOMES_PATH = Path("records/turn_outcomes.jsonl")
GATE_PATH = Path("records/turn_gate_decisions.jsonl")

_current: ContextVar[dict | None] = ContextVar("turn_outcome_current", default=None)

GUARD_REASONS = frozenset({
    "guard_double_wake", "guard_response_lock", "guard_storm",
    "guard_echo", "guard_global", "guard_per_speaker",
})
ACCEPT_REASONS = frozenset({
    "wake_word", "text", "window_session", "window_question",
    "bypass_strong_voice", "bypass_intent",
})

_WARN_THROTTLE_S = 3600.0
_last_warn: dict[str, float] = {}


def turn_key(speaker: str, ts: float) -> str:
    return f"{speaker}@{ts:.3f}"


def _append(path: Path, rec: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        now = time.monotonic()
        key = str(path)
        last = _last_warn.get(key)
        if last is None or now - last >= _WARN_THROTTLE_S:
            _last_warn[key] = now
            ident = rec.get("turn_key") or rec.get("turn_id")
            logger.warning(f"[TurnOutcome] 寫檔失敗 path={path} turn_key={ident}")


def gate_reason(was_fast: bool, accepted: bool, hits: list[str],
                 override: str | None, bypass: str | None) -> str | None:
    if override:
        return override
    if accepted:
        return bypass or "wake_word"
    if hits:
        return hits[0]
    if was_fast:
        return "guard_double_wake"
    return None


def discovered_from_reason(reason: str | None) -> str:
    return {
        "window_session": "session",
        "window_question": "question",
        "text": "text",
    }.get(reason, "wake_word")


def write_gate_decision(*, speaker, ts, track, reason, iba_confidence) -> None:
    try:
        if reason is None:
            return
        rec = {
            "ts": time.time(),
            "turn_key": turn_key(speaker, ts),
            "track": track,
            "speaker": speaker,
            "decision": "accept" if reason in ACCEPT_REASONS else "block",
            "reason": reason,
            "window": {"window_session": "session", "window_question": "question"}.get(reason),
            "iba_confidence": iba_confidence,
        }
        _append(GATE_PATH, rec)
    except Exception as e:
        logger.debug(f"[TurnOutcome] write_gate_decision 失敗（忽略）: {e}")


def mark_route(route: str) -> None:
    rec = _current.get()
    if rec is not None and rec["route"] is None:
        rec["route"] = route


def note_winner(bid) -> None:
    rec = _current.get()
    if rec is None:
        return
    rec["intent"] = bid.name
    rec["slots"] = list(getattr(bid, "missing_slots", []) or [])
    mark_route(f"bus:{bid.name}")


def note_gesture_hint(code: str) -> None:
    """LLM 回覆開頭的動作標籤（只記第一個）。回合外呼叫＝no-op。"""
    rec = _current.get()
    if rec is None or rec.get("_gesture_hint") is not None:
        return
    rec["_gesture_hint"] = code


def claim_first_speech() -> dict | None:
    """本回合第一句 Marvin TTS 呼叫這個：第一次回 rec（唯讀用），之後同回合都回 None；回合外回 None。"""
    rec = _current.get()
    if rec is None or rec.get("_speech_claimed"):
        return None
    rec["_speech_claimed"] = True
    return rec


def _finish(rec: dict, *, error: bool = False, cancelled: bool = False) -> None:
    try:
        if rec["route"] is None:
            rec["route"] = "cancelled" if cancelled else ("error" if error else "silent")
        rec["executed"] = (
            not error and not cancelled
            and rec["route"] != "silent"
            and not rec["route"].startswith("dropped:")
        )
        _append(OUTCOMES_PATH, {k: v for k, v in rec.items() if not k.startswith("_")})
    except Exception as e:
        logger.debug(f"[TurnOutcome] _finish 失敗（忽略）: {e}")


async def run_turn(coro, *, speaker: str, ts: float, discovered_by: str, route: str | None = None):
    rec = {
        "turn_id": turn_key(speaker, ts),
        "ts": time.time(),
        "speaker": speaker,
        "discovered_by": discovered_by,
        "route": route,
        "intent": None,
        "slots": [],
        "executed": None,
        "corrected": None,
        "satisfaction_signal": None,
    }
    token = _current.set(rec)
    try:
        await coro
    except asyncio.CancelledError:
        _finish(rec, cancelled=True)
        raise
    except Exception:
        _finish(rec, error=True)
        raise
    else:
        _finish(rec)
    finally:
        _current.reset(token)
    return None


def turn_route(route: str):
    def decorator(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            mark_route(route)
            return await fn(*args, **kwargs)
        return wrapper
    return decorator
