"""Recommendation event log — append-only jsonl for offline feedback analysis.

5/21 slice 延伸（2026-05-20）：主動推薦（music curation / topic suggestion /
reminder / vision comment ...）的證據留檔，供隔日 NightlyFeedbackBatch 跑
per-type FeedbackAnalyzer 解析 user 反應。

Design notes:
- `reason_internal` vs `explanation_uttered` 故意兩欄分開：
    - reason_internal: machine-readable 特徵字串（給 analyzer 抽特徵）
    - explanation_uttered: TTS 真講出去的 quip（給人類審視 / Marvin 自答用）
- IO 失敗永不傳染 caller（caller 在 wake path 上）；只 log warning
- Reader 跳過壞行（單行 JSON 損毀不毀整檔讀取）
- Append-only：永不改寫既有 record（離線分析必須能 replay）
"""
from __future__ import annotations

import datetime
import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

DEFAULT_LOG_PATH = Path("records/agent_recommendations.jsonl")

# Asia/Taipei timezone — bot deploys in 台灣，所有 time bucketing 用本地時間。
_TPE_TZ = datetime.timezone(datetime.timedelta(hours=8))


def time_of_day_bucket(unix_ts: float) -> str:
    """把 unix ts 轉成 morning / afternoon / evening / night 四 bucket。

    邊界（左閉右開，UTC+8 本地時）：
      05:00 ≤ morning   < 11:00
      11:00 ≤ afternoon < 17:00
      17:00 ≤ evening   < 22:00
      22:00 ≤ night     < 05:00 (跨日)

    給離線 analyzer 分析「不同時段推薦的反應 pattern」用。
    """
    hour = datetime.datetime.fromtimestamp(unix_ts, tz=_TPE_TZ).hour
    if 5 <= hour < 11:
        return "morning"
    if 11 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 22:
        return "evening"
    return "night"


@dataclass(frozen=True)
class Recommendation:
    """One proactive recommendation event. Append once, never mutate."""
    ts: float                          # unix timestamp
    agent: str                         # "music" / "topic" / "vision" / "reminder" / ...
    speaker: str                       # 推薦對誰
    trigger: str                       # "queue_empty" / "ambient_cold" / ...
    selected: str                      # 具體推薦內容（歌名 / 話題 / 提醒文字）
    reason_internal: str               # 特徵字串，給離線 analyzer 抽
    explanation_uttered: str           # TTS 真講的 quip（≤30 chars 建議）；可空字串
    feedback_window_s: int             # 多久內的 utt 算 feedback（per-agent）
    channel_state: dict[str, Any] = field(default_factory=dict)

    def to_jsonline(self) -> str:
        """Serialize to single-line JSON (no trailing newline; writer adds it)."""
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"))


def append_recommendation(
    rec: Recommendation,
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
        logger.warning(f"⚠️ [Recommendation] 寫入 {p} 失敗（不阻斷推薦流程）：{e}")


def read_recommendations(
    path: Path | str = DEFAULT_LOG_PATH,
) -> Iterable[Recommendation]:
    """Yield Recommendation records from jsonl. Skip corrupted lines, log warning.

    Missing file → empty iterator (first-run safety for offline batch).
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
                    yield Recommendation(**data)
                except (json.JSONDecodeError, TypeError, ValueError) as e:
                    logger.warning(
                        f"⚠️ [Recommendation] {p}:{lineno} 解析失敗，跳過: {e}"
                    )
                    continue
    except Exception as e:
        logger.warning(f"⚠️ [Recommendation] 讀取 {p} 失敗: {e}")
        return
