"""SpeakOutcome — proactive 發話結果 log（append-only jsonl）。

每筆紀錄一次 SpeakBus.tick() 的結果（誰贏、信心、reason、後 N 秒有無 STT 回聲），
供：
  - 餓死警報（連 N 天沒贏 → log warning）
  - 利基特化（agent 學自己的 sweet spot context）
  - 離線 replay 驗 bus 機制（頻率 / 衝突 / 餓死）

設計原則（mirror records/agent_recommendations.jsonl）：
  - Append-only：永不改寫既有 record（離線分析必須能 replay）
  - IO 失敗永不傳染 caller（hot path）；只 log warning
  - Reader 跳過壞行（單行 JSON 損毀不毀整檔讀取）
  - schema_version=1：未來新欄位 bump version，舊版 reader 容錯
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

DEFAULT_LOG_PATH = Path("records/speak_outcomes.jsonl")


@dataclass(frozen=True)
class SpeakOutcome:
    """One SpeakBus tick outcome. Append once, never mutate."""
    ts: float                              # tick 時間
    trigger: str                           # "idle_tick" / "post_utterance" / "mood_transition"
    winner: str | None                     # agent name；None = 全部 < MIN_CONFIDENCE
    confidence: float                      # effective confidence（已套 multiplier）
    reason: str                            # 贏的 bid 的 reason 字串
    bid_count: int                         # 收到的 bid 總數（含未達門檻的）
    had_followup_stt: bool                 # tick 後 N 秒內房間有 STT 回聲（弱 quality signal）
    silence_seconds: float                 # tick 時的靜默秒數
    present_speakers: tuple[str, ...] = ()
    schema_version: int = 1                # 紀律：第一筆 record 起算

    def to_jsonline(self) -> str:
        """Serialize to single-line JSON (no trailing newline; writer adds it)."""
        d = asdict(self)
        # tuple → list（JSON 不支援 tuple）
        d["present_speakers"] = list(d["present_speakers"])
        return json.dumps(d, ensure_ascii=False, separators=(",", ":"))


def append_speak_outcome(
    rec: SpeakOutcome,
    path: Path | str = DEFAULT_LOG_PATH,
) -> None:
    """Append one record to jsonl. Never raises — IO error logged as warning."""
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(rec.to_jsonline())
            f.write("\n")
    except Exception as e:
        logger.warning(f"⚠️ [SpeakOutcome] 寫入 {p} 失敗（不阻斷 SpeakBus tick）：{e}")


def read_speak_outcomes(
    path: Path | str = DEFAULT_LOG_PATH,
) -> Iterable[SpeakOutcome]:
    """Yield SpeakOutcome records from jsonl. Skip corrupted lines, log warning.

    Missing file → empty iterator. 不認得的 schema_version 仍試著解析（forward-compat）。
    """
    p = Path(path)
    if not p.exists():
        return
    try:
        with p.open("r", encoding="utf-8") as f:
            for lineno, raw in enumerate(f, start=1):
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    data = json.loads(stripped)
                    # tuple round-trip
                    if "present_speakers" in data and isinstance(data["present_speakers"], list):
                        data["present_speakers"] = tuple(data["present_speakers"])
                    yield SpeakOutcome(**data)
                except (json.JSONDecodeError, TypeError, ValueError) as e:
                    logger.warning(
                        f"⚠️ [SpeakOutcome] {p}:{lineno} 解析失敗，跳過: {e}"
                    )
                    continue
    except Exception as e:
        logger.warning(f"⚠️ [SpeakOutcome] 讀取 {p} 失敗: {e}")
        return
