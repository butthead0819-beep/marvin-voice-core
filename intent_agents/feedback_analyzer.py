"""FeedbackAnalyzer — A 類 plugin pattern.

Per `feedback_meta_agent_taxonomy.md`：每個主動推薦 agent 對應一個
FeedbackAnalyzer 實作。離線批 NightlyFeedbackBatch 對每筆 Recommendation
派給 analyzers[rec.agent].analyze(rec, utts_in_window)。

Heuristic 優先（省 LLM call）：rec.speaker 在 window 內沒講話 → 直接判 positive
（silence = 接受，per `feedback_slow_learning_via_recommendations.md` 鐵則）。
有講話才打 LLM 做 sentiment classification。

LLM failure / invalid JSON / unknown sentiment 一律回 neutral with confidence=0.0
讓 caller 知道這筆不該被信任（taste store 寫入要按 confidence threshold filter）。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from intent_agents.recommendation import Recommendation

logger = logging.getLogger(__name__)

Sentiment = Literal["positive", "negative", "neutral", "skipped_immediately"]
_VALID_SENTIMENTS = frozenset({"positive", "negative", "neutral", "skipped_immediately"})


@dataclass(frozen=True)
class Utterance:
    """One transcript record. Mirrors transcript_store.get_recent() row shape."""
    speaker: str
    text: str
    timestamp: float


@dataclass(frozen=True)
class FeedbackResult:
    """Output of analyzer.analyze(). Frozen — never mutated post-creation."""
    sentiment: Sentiment
    confidence: float                # 0.0 = don't trust, 1.0 = certain
    reason: str                      # human-readable explanation
    evidence: tuple[str, ...] = ()   # quoted utterance fragments LLM cited


class FeedbackAnalyzer(Protocol):
    """Contract for per-agent feedback analyzers. agent_type matches Recommendation.agent."""
    agent_type: str

    async def analyze(
        self,
        rec: Recommendation,
        utts_in_window: list[Utterance],
    ) -> FeedbackResult: ...


# ── Prompt ─────────────────────────────────────────────────────────────────

_MUSIC_SYS_PROMPT = (
    "你是 Marvin 系統的 feedback 分析器。"
    "user 收到一個音樂推薦後，在窗口內講了一些話。你的任務：判斷 user 對該推薦的情緒。\n\n"
    "硬規則：\n"
    "1. 必須回 JSON：{\"sentiment\": str, \"confidence\": float, \"reason\": str, \"evidence\": [str]}\n"
    "2. sentiment 只能是 positive / negative / neutral / skipped_immediately 四選一\n"
    "3. confidence 0.0-1.0，反映你對判斷的把握\n"
    "4. evidence 是你判斷時引用的 user 原話片段（最多 3 筆）\n"
    "5. 觀察「換一首/不要/難聽」明確負面；「讚/好聽/再來一首」明確正面；\n"
    "   無明顯情緒或混合 → neutral\n"
    "6. 若 user 在推薦發出後立刻講話請求別的歌 → skipped_immediately\n"
)


def _build_music_user_message(rec: Recommendation, utts: list[Utterance]) -> str:
    lines = [
        f"speaker: {rec.speaker}",
        f"recommended: {rec.selected}",
        f"explanation_uttered: {rec.explanation_uttered}",
        f"trigger: {rec.trigger}",
        "",
        "speaker's utterances in feedback window:",
    ]
    for u in utts:
        delta = u.timestamp - rec.ts
        lines.append(f"  +{delta:.1f}s: {u.text}")
    return "\n".join(lines)


# ── Parsing ────────────────────────────────────────────────────────────────

def _parse_llm_response(content: str) -> FeedbackResult | None:
    """Parse JSON, validate sentiment. Return None on any failure (caller fallbacks)."""
    stripped = (content or "").strip()
    if not stripped.startswith("{"):
        return None
    try:
        data = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return None
    sentiment = data.get("sentiment", "")
    if sentiment not in _VALID_SENTIMENTS:
        return None
    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    reason = str(data.get("reason", "")).strip()[:200]
    evidence_raw = data.get("evidence", []) or []
    evidence = tuple(str(e).strip()[:100] for e in evidence_raw[:3] if e)
    return FeedbackResult(
        sentiment=sentiment,  # type: ignore[arg-type]
        confidence=confidence,
        reason=reason,
        evidence=evidence,
    )


# ── Concrete analyzer ─────────────────────────────────────────────────────

class MusicFeedbackAnalyzer:
    """First concrete plugin. Other agent types implement same shape.

    2026-05-21：LLM 從直連 Groq client（鎖死 llama-3.3-70b、與全 bot 搶同一額度）改吃
    TieredLLMRouter.analyze（Tier 2，70b 池多家分流 + 429 cooldown）。離線批量分析屬
    Tier 2「分析質量」（per project_llm_tier_wrapper）。analyze() 回 content 或 None
    （池全冷卻/失敗，不 raise）→ None 時降級 neutral conf=0。
    """
    agent_type: str = "music"

    def __init__(self, router: Any):
        """router: TieredLLMRouter。缺（None）→ 視同池不可用，analyze 回 neutral conf=0。"""
        self.router = router

    async def analyze(
        self,
        rec: Recommendation,
        utts_in_window: list[Utterance],
    ) -> FeedbackResult:
        # Heuristic 1: rec.speaker 自己沒講話 → silence = positive 但弱訊號
        # confidence=0.4 刻意低於 T1 min_confidence (0.5)：silence 不自動寫進 music_memory，
        # 避免「user 沒抗議」連 3 次被誤升 suki.likes（review 2026-05-20 校正）
        speaker_utts = [u for u in utts_in_window if u.speaker == rec.speaker]
        if not speaker_utts:
            return FeedbackResult(
                sentiment="positive",
                confidence=0.4,
                reason="silence in window (no抗議 = 接受，但訊號弱不寫 store)",
                evidence=(),
            )

        # LLM classify：router 內部做 pool 分流 + cooldown；全冷卻/失敗回 None（不 raise）。
        user_msg = _build_music_user_message(rec, speaker_utts)
        content = None
        if self.router is not None:
            content = await self.router.analyze(
                user_msg, caller="feedback_analyzer", system=_MUSIC_SYS_PROMPT,
                json=True, max_tokens=300, temperature=0.0,
            )
        if content is None:
            logger.warning("⚠️ [FeedbackAnalyzer:music] router 無回應（池全冷卻/無 router）→ neutral 兜底")
            return FeedbackResult(
                sentiment="neutral",
                confidence=0.0,
                reason="llm_unavailable: pool exhausted or no router",
                evidence=(),
            )

        parsed = _parse_llm_response(content)
        if parsed is None:
            # Distinguish invalid JSON vs invalid sentiment for reason field
            reason = "invalid_sentiment_or_json" if content and content.strip().startswith("{") else "invalid_json"
            return FeedbackResult(
                sentiment="neutral",
                confidence=0.0,
                reason=reason,
                evidence=(),
            )
        return parsed
