"""Volatile Results Phase 0 影子量測（2026-06-13）。

目的：用真實 utterance 量測「volatile 串流 STT」的兩個決策數字，
決定要不要做 Phase 1（語意斷句）/ Phase 2（volatile 喚醒 arm）：
1. stable_ms vs audio_ms — 文字多早趨穩（語意斷句可省的尾巴）
2. n_revisions — 假設翻盤率（投機下游的風險定價）
3. wake_first_ms — 喚醒詞句中多早可見（arm 的潛在提前量）

做法：取樣 utterance 的 WAV 副本 → stream_stt_shadow_bin 以實時節奏重播
（progressiveTranscription preset）→ 解析 volatile 時間線 → 寫
records/volatile_shadow.jsonl → 刪 WAV 副本（不留存使用者音訊）。

零管線影響：fire-and-forget、單飛閘（同時最多一個重播，控 CPU）、
env VOLATILE_SHADOW 閘控 + 首呼 log（J2 教訓）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import shutil
import time
from pathlib import Path
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

DEFAULT_OUT_PATH = Path("records/volatile_shadow.jsonl")
_BIN = "./stream_stt_shadow_bin"
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})

_logged_once = False
_bg_tasks: set = set()
_inflight = False  # 單飛閘：重播以實時節奏跑（~音訊長度），避免並行吃 CPU


def shadow_enabled() -> bool:
    return os.environ.get("VOLATILE_SHADOW", "").strip().lower() in _TRUE_VALUES


def should_sample(rng: Callable[[], float] = random.random) -> bool:
    try:
        rate = float(os.environ.get("VOLATILE_SHADOW_RATE", "0.2"))
    except ValueError:
        rate = 0.2
    return rng() < rate


# ── pure core ────────────────────────────────────────────────────────────────

def parse_events(lines: list[str]) -> tuple[list[dict], dict]:
    """stream_stt_shadow_bin stdout → (events, done_info)。壞行安全跳過。"""
    events: list[dict] = []
    done: dict = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("__DONE__"):
            try:
                done = json.loads(line[len("__DONE__"):].strip())
            except Exception:
                pass
            continue
        try:
            ev = json.loads(line)
            if "t_ms" in ev and "text" in ev:
                events.append(ev)
        except Exception:
            continue
    return events, done


def _norm(text: str) -> str:
    return text.replace(" ", "").replace("，", "").replace("。", "")


def analyze_timeline(events: list[dict], audio_ms: int) -> dict:
    """volatile 時間線 → 決策指標。"""
    from utils import WAKE_WORDS_LIST

    final_text = events[-1]["text"] if events else ""
    stable_ms: Optional[int] = None
    n_revisions = 0
    wake_first_ms: Optional[int] = None

    prev = ""
    for ev in events:
        cur = ev["text"]
        if _norm(cur) != _norm(prev):
            stable_ms = ev["t_ms"]
            # 翻盤：新文字不是舊文字的延伸（修改了已輸出的部分）
            if prev and not _norm(cur).startswith(_norm(prev)):
                n_revisions += 1
        if wake_first_ms is None:
            low = cur.lower()
            if any(w.lower() in low for w in WAKE_WORDS_LIST):
                wake_first_ms = ev["t_ms"]
        prev = cur

    return {
        "final_text": final_text,
        "stable_ms": stable_ms,
        "n_events": len(events),
        "n_revisions": n_revisions,
        "wake_first_ms": wake_first_ms,
        "audio_ms": audio_ms,
    }


# ── IO shell ─────────────────────────────────────────────────────────────────

async def _exec_replay(wav_path: str) -> list[str]:
    """跑重播 bin，回傳 stdout 行。timeout = 充裕上限（實時重播 ≈ 音訊長度）。"""
    proc = await asyncio.create_subprocess_exec(
        _BIN, wav_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env={**os.environ, "STT_LOCALE": os.environ.get("STT_LOCALE", "zh-TW")},
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=40.0)
    return stdout.decode("utf-8", errors="ignore").splitlines()


async def run_replay(wav_copy_path: str, speaker: str, pipeline_text: str, engine: str, *,
                     exec_fn: Callable[[str], Awaitable[list[str]]] | None = None,
                     out_path: Path = DEFAULT_OUT_PATH) -> None:
    """重播 + 記錄一筆。絕不 raise；結束時必刪 WAV 副本（不留存音訊）。"""
    global _inflight
    stats: dict = {}
    error: Optional[str] = None
    try:
        lines = await (exec_fn or _exec_replay)(wav_copy_path)
        events, done = parse_events(lines)
        stats = analyze_timeline(events, audio_ms=int(done.get("audio_ms", 0)))
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
    finally:
        _inflight = False
        try:
            os.remove(wav_copy_path)
        except OSError:
            pass
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": time.time(),
                "speaker": speaker,
                "pipeline_text": pipeline_text,
                "engine": engine,
                "error": error,
                **stats,
            }, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning(f"[VolatileShadow] 寫記錄失敗: {e}")


def maybe_shadow(wav_path: str, speaker: str, pipeline_text: str, engine: str) -> None:
    """主管線唯一入口：env 閘 + 抽樣 + 單飛 + WAV 副本 + fire-and-forget。

    必須在 running event loop 內呼叫（_process_stt_hybrid coroutine）。
    """
    global _logged_once, _inflight
    if not _logged_once:
        _logged_once = True
        logger.info(
            f"[VolatileShadow] shadow={'ON' if shadow_enabled() else 'OFF'} "
            f"rate={os.environ.get('VOLATILE_SHADOW_RATE', '0.2')}"
        )
    if not shadow_enabled() or _inflight or not should_sample():
        return
    try:
        copy_path = f"/tmp/volatile_shadow_{time.time_ns()}.wav"
        shutil.copy(wav_path, copy_path)
    except OSError:
        return
    _inflight = True
    try:
        task = asyncio.create_task(run_replay(copy_path, speaker, pipeline_text, engine))
        _bg_tasks.add(task)
        task.add_done_callback(_bg_tasks.discard)
    except RuntimeError:
        _inflight = False
        try:
            os.remove(copy_path)
        except OSError:
            pass
