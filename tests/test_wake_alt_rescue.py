"""TDD: 喚醒詞 Alt-Lattice 救援（第五通道 alt_wake）。

真實事故（2026-09-17 18:25）：「馬文，下一首」被 SwiftV2 STT 主文字辨成
「我是下一首」→ 不含任何喚醒詞 → 喚醒層直接 drop。但 STT meta 的 N-best
lattice 裡藏著「馬我」（跟「馬文」拼音 fuzz.ratio=72.7）。

核心安全性質：alt 命中單獨永遠不足以喚醒，必須搭配既有 task/control 通道佐證
（見 wake_detector.multi_channel_decide 的 ALT_WAKE_WEIGHT=0.30 < MULTI_THRESHOLD）。
"""
from __future__ import annotations

from wake_detector import WakeDetector, score_alt_wake


# ── score_alt_wake 單元測試 ───────────────────────────────────────────────────

def test_score_alt_wake_none_segments():
    assert score_alt_wake(None, "我是下一首") == 0.0


def test_score_alt_wake_empty_segments():
    assert score_alt_wake([], "我是下一首") == 0.0


def test_score_alt_wake_raw_text_already_has_wake_word():
    # 主文字已含「馬文」→ 已有證據，不重複計分
    alt_segments = [["我", "馬我", "那我"], ["是下", "下"], ["一首"]]
    assert score_alt_wake(alt_segments, "馬文下一首") == 0.0


def test_score_alt_wake_hits_on_near_miss_pinyin():
    # 真實事故重現：「馬我」拼音跟「馬文」ratio=72.7 >= 72 閾值
    alt_segments = [["我", "馬我", "那我"], ["是下", "下"], ["一首"]]
    assert score_alt_wake(alt_segments, "我是下一首") == 1.0


def test_score_alt_wake_only_scans_first_two_segments():
    # 喚醒詞候選只出現在第 4 個 segment → 不掃、回 0.0
    alt_segments = [["我"], ["是下"], ["一首"], ["馬我"]]
    assert score_alt_wake(alt_segments, "我是下一首") == 0.0


def test_score_alt_wake_no_hit_when_no_near_miss():
    alt_segments = [["這", "那"], ["部電影"]]
    assert score_alt_wake(alt_segments, "這部電影") == 0.0


# ── multi_channel_decide 整合測試 ─────────────────────────────────────────────

def test_incident_repro_wakes_with_alt_and_control():
    """真實事故重現：alt 命中 + 「下一首」control 命中 → should_wake=True。"""
    wd = WakeDetector()
    alt_segments = [["我", "馬我", "那我"], ["是下", "下"], ["一首"]]
    alt = score_alt_wake(alt_segments, "我是下一首")
    assert alt == 1.0
    should_wake, confidence, scores = wd.multi_channel_decide(
        action="drop",
        wake_intent=None,
        text="我是下一首",
        speaker="TestUser",
        context_active=False,
        alt_wake=alt,
    )
    assert scores["control"] > 0.0, "「下一首」control regex 沒命中，設計前提不成立"
    assert should_wake is True


def test_alt_hit_alone_without_task_control_does_not_wake():
    """安全性質：alt 命中但無 task/control 佐證 → should_wake=False（即使拼音也命中「馬克」）。"""
    wd = WakeDetector()
    # 「馬克」拼音對「馬文」ratio=72.7，會命中 alt，但整句無 task/control 訊號
    alt_segments = [["馬克", "馬個"], ["你好"]]
    alt = score_alt_wake(alt_segments, "馬克你好")
    should_wake, confidence, scores = wd.multi_channel_decide(
        action="drop",
        wake_intent=None,
        text="馬克你好",
        speaker="TestUser",
        context_active=False,
        alt_wake=alt,
    )
    assert scores["task"] == 0.0
    assert scores["control"] == 0.0
    assert should_wake is False


def test_alt_segments_none_regression_unchanged():
    """回歸：alt_wake=0.0（預設/None 場景）時，既有行為完全不變。"""
    wd = WakeDetector()
    should_wake_with_default, confidence_default, scores_default = wd.multi_channel_decide(
        action="fast_intervene",
        wake_intent=1.0,
        text="馬文幫我查天氣",
        speaker="TestUser",
        context_active=False,
    )
    should_wake_explicit, confidence_explicit, scores_explicit = wd.multi_channel_decide(
        action="fast_intervene",
        wake_intent=1.0,
        text="馬文幫我查天氣",
        speaker="TestUser",
        context_active=False,
        alt_wake=0.0,
    )
    assert should_wake_with_default is True
    assert should_wake_with_default == should_wake_explicit
    assert confidence_default == confidence_explicit
    assert scores_default["total"] == scores_explicit["total"]

    should_wake_drop, confidence_drop, scores_drop = wd.multi_channel_decide(
        action="drop",
        wake_intent=None,
        text="今天天氣不錯",
        speaker="TestUser",
        context_active=False,
    )
    assert should_wake_drop is False
    assert confidence_drop < 0.35


def test_alt_wake_zeroed_when_voice_already_has_evidence():
    """voice != 0 時（主文字已有喚醒證據）alt_wake 不計入 total。"""
    wd = WakeDetector()
    _, _, scores = wd.multi_channel_decide(
        action="fast_intervene",
        wake_intent=1.0,
        text="馬文",
        speaker="TestUser",
        context_active=False,
        alt_wake=1.0,
    )
    assert scores["alt_wake"] == 0.0


def test_alt_wake_zeroed_by_track_b_veto():
    """Track B LLM 明確否決（wake_intent < 0.65）時，alt_wake 也一併清零。"""
    wd = WakeDetector()
    _, _, scores = wd.multi_channel_decide(
        action="llm_verify",
        wake_intent=0.2,
        text="我是下一首",
        speaker="TestUser",
        context_active=False,
        track="B",
        alt_wake=1.0,
    )
    assert scores["alt_wake"] == 0.0
    assert scores["control"] == 0.0
