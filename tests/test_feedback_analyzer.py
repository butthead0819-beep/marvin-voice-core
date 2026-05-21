"""TDD: FeedbackAnalyzer Protocol + MusicFeedbackAnalyzer (A 類 plugin 第一個實作).

5/21 slice 延伸：把一筆 Recommendation + 窗口內的 transcript utts 解析成
FeedbackResult（sentiment / confidence / reason / evidence）。

Per `feedback_meta_agent_taxonomy.md`：這是 A 類 plugin，不是 IntentBus agent。
2026-05-21：LLM 從直連 Groq client 改吃 TieredLLMRouter.analyze（Tier 2，70b 池
多家分流 + 429 cooldown）。analyze() 直接回 content 字串或 None（池全冷卻/失敗），
不 raise——所以測試 mock 的是 router.analyze 的回傳字串，而非 client response 物件。
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from intent_agents.feedback_analyzer import (
    FeedbackResult,
    MusicFeedbackAnalyzer,
    Utterance,
)
from intent_agents.recommendation import Recommendation


# ── Fixtures ───────────────────────────────────────────────────────────────

def _make_rec(speaker: str = "大肚", selected: str = "周杰倫 夜曲") -> Recommendation:
    return Recommendation(
        ts=1000.0,
        agent="music",
        speaker=speaker,
        trigger="queue_empty",
        selected=selected,
        reason_internal="late_night+age_35",
        explanation_uttered="猜你想聽周杰倫",
        feedback_window_s=300,
        channel_state={},
    )


def _utt(speaker: str, text: str, t: float) -> Utterance:
    return Utterance(speaker=speaker, text=text, timestamp=t)


def _router_returning(sentiment: str, confidence: float = 0.9,
                      reason: str = "", evidence: list | None = None):
    """假 router：analyze() 回一段合法 sentiment JSON 字串。"""
    payload = {
        "sentiment": sentiment,
        "confidence": confidence,
        "reason": reason or f"LLM judged as {sentiment}",
        "evidence": evidence or [],
    }
    router = MagicMock()
    router.analyze = AsyncMock(return_value=json.dumps(payload, ensure_ascii=False))
    return router


def _router_returning_raw(content):
    """假 router：analyze() 回任意 content（含 None＝池全冷卻、或非 JSON）。"""
    router = MagicMock()
    router.analyze = AsyncMock(return_value=content)
    return router


# ── 1. Silence = positive (heuristic, no LLM) ──────────────────────────────

@pytest.mark.asyncio
async def test_no_speaker_utterances_returns_positive_silence():
    """沉默 = 正面（沒抗議 = 接受）—— heuristic 不打 LLM。"""
    router = _router_returning("negative")  # 即使 router 是 mock 也不該被打
    analyzer = MusicFeedbackAnalyzer(router=router)

    result = await analyzer.analyze(_make_rec(speaker="大肚"), utts_in_window=[])

    assert result.sentiment == "positive"
    assert "silence" in result.reason.lower() or "沉默" in result.reason
    assert router.analyze.await_count == 0, "silence path 不該打 LLM"


@pytest.mark.asyncio
async def test_only_other_speakers_utts_treated_as_silence():
    """窗口內只有別人說話，rec.speaker 自己沒講 → 同 silence。"""
    router = _router_returning("positive")
    analyzer = MusicFeedbackAnalyzer(router=router)

    utts = [
        _utt("露", "這歌不錯", 1010.0),
        _utt("馬文", "已加入佇列", 1015.0),
    ]
    result = await analyzer.analyze(_make_rec(speaker="大肚"), utts_in_window=utts)

    assert result.sentiment == "positive"
    assert router.analyze.await_count == 0


# ── 2. LLM classifies utterances ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_speaker_utt_triggers_llm_classification():
    """rec.speaker 自己在窗口內講話 → 打 LLM 分類。"""
    router = _router_returning("positive", confidence=0.85,
                               reason="user 說『讚 我超愛這首』",
                               evidence=["讚 我超愛這首"])
    analyzer = MusicFeedbackAnalyzer(router=router)

    utts = [_utt("大肚", "讚 我超愛這首", 1020.0)]
    result = await analyzer.analyze(_make_rec(speaker="大肚"), utts_in_window=utts)

    assert result.sentiment == "positive"
    assert result.confidence == 0.85
    assert "讚" in result.reason
    assert result.evidence == ("讚 我超愛這首",)
    assert router.analyze.await_count == 1


@pytest.mark.asyncio
async def test_llm_prompt_contains_rec_and_utts():
    """LLM 必須收到 rec 內容 + speaker 的 utts，才能做 attribution。"""
    router = _router_returning("negative")
    analyzer = MusicFeedbackAnalyzer(router=router)

    rec = _make_rec(speaker="大肚", selected="周杰倫 夜曲")
    utts = [_utt("大肚", "啊不要這首啦 換一首", 1050.0)]
    await analyzer.analyze(rec, utts)

    user_msg = router.analyze.await_args.args[0]

    assert "周杰倫 夜曲" in user_msg, "rec.selected 必須注入 prompt"
    assert "啊不要這首啦 換一首" in user_msg, "speaker utt 必須注入 prompt"
    assert "大肚" in user_msg, "speaker 名稱必須注入 prompt"


@pytest.mark.asyncio
async def test_llm_called_with_json_system_caller():
    """analyze 必帶 json/system(prompt)/caller=feedback_analyzer，否則歸屬與 parse 不可靠。"""
    router = _router_returning("neutral")
    analyzer = MusicFeedbackAnalyzer(router=router)

    await analyzer.analyze(_make_rec(), [_utt("大肚", "嗯", 1010.0)])

    kw = router.analyze.await_args.kwargs
    assert kw["json"] is True
    assert kw["caller"] == "feedback_analyzer"
    assert kw["system"]  # 帶 system prompt（非空）


# ── 3. Failure modes ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pool_exhausted_returns_neutral_with_zero_confidence():
    """池全冷卻/失敗 → router.analyze 回 None → 安全降級 neutral conf=0；caller 看 reason 知道。"""
    router = _router_returning_raw(None)
    analyzer = MusicFeedbackAnalyzer(router=router)

    result = await analyzer.analyze(_make_rec(), [_utt("大肚", "嗯", 1010.0)])

    assert result.sentiment == "neutral"
    assert result.confidence == 0.0  # 標明這筆不該被信任
    assert result.reason  # 有 reason 說明為何不可信


@pytest.mark.asyncio
async def test_llm_returns_invalid_json_falls_to_neutral():
    """router 回非 JSON → 同 pool failure 處理。"""
    router = _router_returning_raw("this is not json")
    analyzer = MusicFeedbackAnalyzer(router=router)

    result = await analyzer.analyze(_make_rec(), [_utt("大肚", "ok", 1010.0)])

    assert result.sentiment == "neutral"
    assert result.confidence == 0.0


@pytest.mark.asyncio
async def test_llm_returns_unknown_sentiment_falls_to_neutral():
    """LLM 回個沒見過的 sentiment 字串 → fallback neutral，不亂寫進 store。"""
    router = _router_returning("ecstatic_with_existential_dread")  # 非合法 sentiment
    analyzer = MusicFeedbackAnalyzer(router=router)

    result = await analyzer.analyze(_make_rec(), [_utt("大肚", "ok", 1010.0)])

    assert result.sentiment == "neutral"
    assert "invalid_sentiment" in result.reason.lower() or "未知" in result.reason


# ── 4. Schema ─────────────────────────────────────────────────────────────

def test_agent_type_is_music():
    """plugin 註冊靠 agent_type；MusicFeedbackAnalyzer.agent_type 必須是 'music'。"""
    analyzer = MusicFeedbackAnalyzer(router=MagicMock())
    assert analyzer.agent_type == "music"


def test_feedback_result_is_frozen():
    r = FeedbackResult(sentiment="positive", confidence=0.9,
                       reason="x", evidence=("e1",))
    with pytest.raises((AttributeError, TypeError, Exception)):
        r.sentiment = "negative"  # type: ignore[misc]
