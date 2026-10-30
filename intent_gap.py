"""Intent gap detection — bus 沒贏家 + 有 intent 訊號時的紀錄 + ack 流程。

Phase A 範圍：只負責「偵測 gap → 寫 records/agent_gaps.jsonl → 播模板 ack」。
不嘗試完成任務（那是 Phase C，要 openclaw tool calling）。

模組分工（之後步驟逐步加入此檔）：
- IntentGapRecord  ← 本檔，schema dataclass
- build_intent_manifest()  ← 步驟 2
- gap_logger / dedup       ← 步驟 4
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


logger = logging.getLogger("cogs.voice_controller.intent_gap")


@dataclass(frozen=True)
class IntentGapRecord:
    """有 intent 訊號但無 agent 命中的紀錄。寫入 records/agent_gaps.jsonl。

    schema_version=1 從第一筆 record 算起 — 未來 consumer（daily ritual /
    Claude Code 補 agent）演進欄位時，沒版本欄就只能猜。

    UNKNOWN intent_type 是 classifier failure 的合法狀態：保留 raw_query
    給 daily ritual 看，但不播 ack（避免講錯話）。
    """
    utterance_id: str
    ts: float
    speaker: str
    mode: str
    raw_query: str
    cleaned_query: str
    intent_type: str  # classifier 失敗時為 "UNKNOWN"
    slots: dict[str, Any]
    nearest_agent: str | None
    nearest_distance: float | None
    ack_text: str | None  # None = 沒播 ack（UNKNOWN / 5min dedup skipped）
    acknowledged: bool
    schema_version: int = 1
    # 8/18 新增：query-type 意圖要接 web 查詢還是讀 Marvin 自己的 state，執行工具完全
    # 不同（見 project memory：分開才不用每次現查現猜）。只在「使用者在問資訊」時
    # 才填；action 類指令（播放/控制/社交）跟 UNKNOWN 一律 null。
    #   "web"  — 需要外部查詢（天氣/知識/時事，答案不在 bot 自己身上）
    #   "core" — Marvin/bot 自己的狀態（現在幾點/現在播什麼/系統狀態）
    query_domain: str | None = None

    def to_jsonl(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> IntentGapRecord:
        return cls(**d)


# ─────────────────────────────────────────────────────────────────────────────
# GapLogger — append-only JSONL writer + in-memory (speaker, intent_type) ack
# dedup cache。Process restart 會清空 cache（5min 視窗短，可接受）。
# JSONL 永遠寫（包含 UNKNOWN / dedup-skipped record，daily ritual 才看得到頻率）。
# ─────────────────────────────────────────────────────────────────────────────


class GapLogger:
    def __init__(self, jsonl_path: Path | str, dedup_window_s: float = 300.0):
        self.jsonl_path = Path(jsonl_path)
        self.dedup_window_s = dedup_window_s
        self._last_ack: dict[tuple[str, str], float] = {}

    def write(self, record: IntentGapRecord) -> None:
        self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        with self.jsonl_path.open("a", encoding="utf-8") as f:
            f.write(record.to_jsonl() + "\n")

    def should_ack(self, speaker: str, intent_type: str, now: float) -> bool:
        """同 (speaker, intent_type) 5min 內已 ack 過 → False。caller 自己保證
        UNKNOWN 不該叫這個。"""
        last = self._last_ack.get((speaker, intent_type))
        if last is None:
            return True
        return (now - last) >= self.dedup_window_s

    def mark_acked(self, speaker: str, intent_type: str, now: float) -> None:
        self._last_ack[(speaker, intent_type)] = now
        # 順手清超過 2x window 的 stale entry，避免 dict 無限長大。
        cutoff = now - self.dedup_window_s * 2
        self._last_ack = {k: v for k, v in self._last_ack.items() if v >= cutoff}
