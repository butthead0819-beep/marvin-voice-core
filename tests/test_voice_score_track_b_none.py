"""TDD: Track=B 但 wake_intent=None → 不該回 1.0（regex 高分）。

2026-05-20 prod 17:06–17:07 真實 false wake：STT 把「馬文」黏在「李宗盛」前
→ cleaner LLM JSON parse 失敗回 wake_intent=None → _voice_score fall-through
到「if action == fast_intervene: return 1.0」→ IBA voice=1.0 → wake 觸發。

修法：track="B" + wake_intent=None 視為「LLM 試了但無 verdict」，回低分
（0.30 < MULTI_THRESHOLD 0.35）避免單靠 regex match 觸發 wake。
"""
from __future__ import annotations

from wake_detector import WakeDetector


def test_track_b_with_none_intent_returns_low_score():
    """關鍵 case：track=B + wake_intent=None → 低分（防 STT 幻覺）。"""
    score = WakeDetector._voice_score(
        action="fast_intervene",   # regex 命中（STT 在 raw 黏了「馬文」）
        wake_intent=None,           # cleaner LLM 失敗無 verdict
        track="B",                  # cleaner ran
    )
    assert score < 0.35, \
        f"Track=B + wake_intent=None 該回低分（<MULTI_THRESHOLD 0.35），實際={score}"


def test_track_b_with_high_intent_returns_intent():
    """Track=B + 高 intent → 直接用 cleaner 的判定（既有行為）。"""
    score = WakeDetector._voice_score(
        action="fast_intervene",
        wake_intent=0.9,
        track="B",
    )
    assert score == 0.9


def test_track_b_with_mid_intent_returns_intent():
    """Track=B + mid intent → cleaner 判定（不被 fast_intervene 1.0 蓋）。"""
    score = WakeDetector._voice_score(
        action="fast_intervene",
        wake_intent=0.5,
        track="B",
    )
    assert score == 0.5


def test_track_a_fast_intervene_unaffected():
    """純 Track A regex（track=None）→ fast_intervene=1.0（既有行為）。"""
    score = WakeDetector._voice_score(
        action="fast_intervene",
        wake_intent=None,
        track=None,
    )
    assert score == 1.0


def test_track_a_force_intervene_unaffected():
    score = WakeDetector._voice_score(
        action="force_intervene",
        wake_intent=None,
        track=None,
    )
    assert score == 0.95


def test_llm_verify_with_intent_returns_intent():
    """llm_verify 路徑 + 有 intent → 用 intent（既有行為）。"""
    score = WakeDetector._voice_score(
        action="llm_verify",
        wake_intent=0.7,
        track=None,
    )
    assert score == 0.7


def test_drop_action_returns_zero():
    score = WakeDetector._voice_score(
        action="drop",
        wake_intent=None,
        track=None,
    )
    assert score == 0.0


def test_track_b_none_below_default_threshold():
    """新 score 在預設 MULTI_THRESHOLD 0.35 下，total = voice * 0.5 仍 < threshold。"""
    voice = WakeDetector._voice_score("fast_intervene", None, "B")
    # IBA total = voice * VOICE_WEIGHT(0.5) — task/info/control 全 0 時
    total = voice * 0.5
    assert total < WakeDetector.MULTI_THRESHOLD, \
        f"voice={voice} × 0.5 = {total} 該 < {WakeDetector.MULTI_THRESHOLD}（無 wake）"
