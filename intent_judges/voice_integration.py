"""Shadow-mode race + telemetry helpers for voice_controller integration.

voice_controller 只需呼叫 `run_shadow_race(...)` 一次（fire-and-forget），
不影響現行 intent_bus dispatch；判斷與資料蒐集分離。

Shadow 設計：
  - J1 跑 raw STT regex（純 sync，幾乎免費）
  - J3 用 **precomputed cleaner**：cleaner_call 直接回傳 caller 已 clean 的字串，
    零額外 LLM cost
  - J2（small LLM rewriter）暫不啟，收夠 J1 hit rate 數據後再加

所有 race / 寫檔失敗都被吞 —— shadow 絕不能影響語音主路徑。
"""
from __future__ import annotations

import itertools
import logging
import time
from dataclasses import replace
from pathlib import Path

from intent_bus import IntentAgent, IntentContext
from intent_judges.cleaner_judge import cleaner_judge
from intent_judges.race import JudgeSpec, race
from intent_judges.regex_judge import regex_judge
from intent_judges.telemetry import write_race_outcome

logger = logging.getLogger("intent_judges.voice_integration")

DEFAULT_OUTCOME_PATH = Path("records/judge_outcomes.jsonl")

_J1_THRESHOLD = 0.90
_J3_THRESHOLD = 0.30  # 對齊 IntentBus.MIN_CONFIDENCE

_uid_counter = itertools.count()


def new_utterance_id(speaker: str) -> str:
    """`<ns>-<seq>-<speaker[:8]>`。ns + process-local counter，緊迴圈也 unique。"""
    ts_ns = time.time_ns()
    seq = next(_uid_counter)
    safe = (speaker or "anon")[:8] or "anon"
    return f"{ts_ns}-{seq}-{safe}"


def make_shadow_specs(
    raw_text: str,
    cleaned_text: str,
    agents: list[IntentAgent],
    *,
    j1_threshold: float = _J1_THRESHOLD,
    j3_threshold: float = _J3_THRESHOLD,
) -> list[JudgeSpec]:
    """產生 [J1 regex on raw, J3 cleaner-precomputed]。

    caller 通常傳的 ctx 已是 cleaned-text ctx（voice_controller 的 _bus_ctx），
    所以 spec wrapper 內部都用 `replace(ctx, query=raw_text, raw_text=raw_text)`
    重寫成 raw-ctx —— J1 才真的跑在 raw STT 上，J3 的 reason log 也對齊。

    J3 的 cleaner_call 是 closure：直接回傳 caller 已 clean 過的 cleaned_text，零 LLM。
    """
    async def _j1(ctx: IntentContext):
        raw_ctx = replace(ctx, query=raw_text, raw_text=raw_text)
        return regex_judge(raw_ctx, agents)

    async def _precomputed_cleaner(_ctx: IntentContext) -> str:
        return cleaned_text

    async def _j3(ctx: IntentContext):
        raw_ctx = replace(ctx, query=raw_text, raw_text=raw_text)
        return await cleaner_judge(raw_ctx, agents, cleaner_call=_precomputed_cleaner)

    return [
        JudgeSpec(_j1, threshold=j1_threshold, name="j1_regex"),
        JudgeSpec(_j3, threshold=j3_threshold, name="j3_cleaner_precomputed"),
    ]


async def run_shadow_race(
    *,
    ctx: IntentContext,
    raw_text: str,
    cleaned_text: str,
    agents: list[IntentAgent],
    utterance_id: str,
    outcome_path: Path = DEFAULT_OUTCOME_PATH,
) -> None:
    """跑 shadow race + 寫 outcome jsonl，任何例外都吞掉。

    從 voice_controller 用 `asyncio.create_task(...)` fire-and-forget；不影響
    現行 dispatch 路徑。
    """
    try:
        specs = make_shadow_specs(raw_text, cleaned_text, agents)
        result = await race(ctx, specs)
    except Exception:
        logger.exception("[shadow-race] race coordinator failed; suppressed")
        return
    try:
        write_race_outcome(outcome_path, utterance_id, ctx, result)
    except Exception:
        logger.exception("[shadow-race] outcome write failed; suppressed")
