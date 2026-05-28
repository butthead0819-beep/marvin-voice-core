"""Intent augmentation — LLM 擴 regex pattern 的純函式層。

職責分四塊：
- extract_schemas_from_class : 動態實例化 agent → 抓真實 IntentSchema（resolve f-string）
- make_augment_prompt        : schema → LLM user prompt
- parse_augment_response     : LLM JSON → AugmentResult
- format_report              : list[AugmentSuggestion] → markdown

不打 LLM；script (scripts/augment_intent_patterns.py) 拿這些函式拼起來。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger("intent_augmentation")


@dataclass(frozen=True)
class SchemaInfo:
    """從 IntentSchema 摘出的不可變描述，便於餵 prompt + report。"""
    agent_name: str
    intent_name: str
    confidence: float
    patterns: tuple[str, ...]
    reason_template: str


@dataclass(frozen=True)
class AugmentResult:
    paraphrases: tuple[str, ...]
    suggested_regex: str | None


@dataclass(frozen=True)
class AugmentSuggestion:
    schema: SchemaInfo
    paraphrases: tuple[str, ...]
    suggested_regex: str | None


# ── 動態 schema 抽取 ──────────────────────────────────────────────────────────


def extract_schemas_from_class(
    cls: type,
    controller_factory: Callable[[], object],
) -> list[SchemaInfo]:
    """實例化 agent class → call declare_intents() → 攤平成 SchemaInfo list。

    刻意：
    - 實例化用 controller_factory()，caller 通常給 lambda: MagicMock()
      → 處理 controller arg；其他特殊 deps（bot/cogs）若仍炸 → catch 並回 []
    - declare_intents 回 [] → 直接回 []（state-checking agent 不可擴）
    """
    try:
        instance = cls(controller_factory())
    except Exception as exc:
        logger.warning(f"[augment] {cls.__name__} 實例化失敗，跳過: {exc}")
        return []

    try:
        schemas = instance.declare_intents()
    except Exception as exc:
        logger.warning(f"[augment] {cls.__name__}.declare_intents() 炸了: {exc}")
        return []

    out: list[SchemaInfo] = []
    agent_name = getattr(instance, "name", cls.__name__)
    for s in schemas:
        out.append(SchemaInfo(
            agent_name=agent_name,
            intent_name=s.name,
            confidence=s.confidence,
            patterns=tuple(s.patterns),
            reason_template=s.reason_template,
        ))
    return out


# ── prompt + response 解析 ────────────────────────────────────────────────────


def make_augment_prompt(schema: SchemaInfo) -> str:
    """生 user prompt 給 cheap LLM；要求 JSON 含 paraphrases + suggested_regex。"""
    existing = "\n".join(f"  - {p}" for p in schema.patterns) or "  - (none)"
    return f"""這是 Discord 語音助理的 intent 定義，請幫忙生成「**台灣口語繁體中文**」paraphrases。

Agent: {schema.agent_name}
Intent: {schema.intent_name}
Reason template: {schema.reason_template}
現有 regex pattern:
{existing}

語言規則（強制）：
- 字形：繁體（影片/品質/網路），禁簡體（视频/质量/网络）— 任何簡體字會直接被丟棄
- 用詞：台灣口語（弄/怎樣/可不可以），禁大陸用語（搞/咋了/行不行）
- STT 永遠輸出繁體，混簡體 pattern 等於白寫

任務：
1. 生成 **10 個** 台灣使用者可能用來觸發這個 intent 的口語表達（含命令式 / 委婉 / 短句）
2. 涵蓋現有 pattern 沒抓到的講法（同義詞、語氣詞、簡寫）
3. 不要重複現有 pattern 已涵蓋的字
4. 不要包含「歌名」「人名」等變數槽位（用佔位 {{slot}} 代替）
5. 提出一條 suggested_regex（OR-pattern，能命中你生成的 paraphrases）

回傳嚴格 JSON：
{{
  "paraphrases": ["...", "...", ...],
  "suggested_regex": "..."
}}"""


def parse_augment_response(raw: str | None) -> AugmentResult | None:
    """容錯：raw=None/空/非 JSON/缺 paraphrases → None；空字串 paraphrase 過濾。"""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None

    paraphrases_raw = parsed.get("paraphrases")
    if not isinstance(paraphrases_raw, list):
        return None
    paraphrases = tuple(
        p.strip() for p in paraphrases_raw
        if isinstance(p, str) and p.strip()
    )
    if not paraphrases:
        return None

    suggested = parsed.get("suggested_regex")
    if suggested is not None and (not isinstance(suggested, str) or not suggested.strip()):
        suggested = None

    return AugmentResult(
        paraphrases=paraphrases,
        suggested_regex=suggested,
    )


# ── markdown report ─────────────────────────────────────────────────────────


def format_report(suggestions: list[AugmentSuggestion]) -> str:
    """產出 markdown：agent header → 每個 intent 一個 subsection。

    結構（reviewer 視角）：

      # Intent Augmentation Suggestions
      ## <agent_name>
      ### <intent_name> (confidence=X)
      **現有 pattern**:
        - `<pattern>`
      **LLM 建議 paraphrases**:
        - <p1>
        - <p2>
      **Suggested regex**:
      ```
      <regex>
      ```
    """
    lines: list[str] = ["# Intent Augmentation Suggestions", ""]

    if not suggestions:
        lines.append("_無 suggestion（LLM 全部失敗或沒可擴的 agent）_")
        return "\n".join(lines)

    # group by agent_name 保序：先見到的 agent 先出
    by_agent: dict[str, list[AugmentSuggestion]] = {}
    for s in suggestions:
        by_agent.setdefault(s.schema.agent_name, []).append(s)

    for agent_name, items in by_agent.items():
        lines.append(f"## {agent_name}")
        lines.append("")
        for sug in items:
            sc = sug.schema
            lines.append(f"### {sc.intent_name} (confidence={sc.confidence})")
            lines.append("")
            lines.append("**現有 pattern**:")
            for p in sc.patterns:
                lines.append(f"  - `{p}`")
            lines.append("")
            lines.append("**LLM 建議 paraphrases**:")
            for p in sug.paraphrases:
                lines.append(f"  - {p}")
            lines.append("")
            if sug.suggested_regex:
                lines.append("**Suggested regex**:")
                lines.append("```")
                lines.append(sug.suggested_regex)
                lines.append("```")
                lines.append("")

    return "\n".join(lines)
