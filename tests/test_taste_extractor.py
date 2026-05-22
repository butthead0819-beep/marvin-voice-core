"""
taste Phase C：即時明示偏好偵測（確定性，零 LLM）。

設計（Jack 2026-05-22 拍板）：P1 修好同步後，daily 的 LLM 抽取已能進 bot；C 不再重做
即時 LLM 抽取（與 slow-learning 原則衝突，見 feedback_slow_learning_via_recommendations），
改用 regex side-channel（仿 Farewell detector）只抓**明示**偏好句「我喜歡/超愛/討厭 X」，
給小分入「曾提及」（< LIKE_THRESHOLD，需跨場景累積才 confirmed）；隱性興趣仍交 offline daily。

extract_taste_signals(text) → [(item, signed_delta), ...]，正分=喜歡、負分=討厭。
"""
from __future__ import annotations

from taste_extractor import extract_taste_signals, REALTIME_TASTE_DELTA


def _items(text):
    return {item for item, _ in extract_taste_signals(text)}


# ── 喜歡 ─────────────────────────────────────────────────────────────────────

def test_simple_like():
    sig = extract_taste_signals("我喜歡爬山")
    assert sig == [("爬山", REALTIME_TASTE_DELTA)]


def test_like_with_intensifier():
    assert _items("我超愛吃拉麵") == {"吃拉麵"}
    assert _items("我最愛周杰倫") == {"周杰倫"}


def test_like_with_wake_prefix():
    # 我 不一定在句首（馬文，我很喜歡貓）
    assert _items("馬文，我很喜歡貓") == {"貓"}


def test_like_delta_is_below_confirmed_threshold():
    from suki_memory import LIKE_THRESHOLD
    assert 0 < REALTIME_TASTE_DELTA < LIKE_THRESHOLD, (
        "單次明示偏好只該入『曾提及』，不該一次變 confirmed（slow learning）"
    )


# ── 討厭 ─────────────────────────────────────────────────────────────────────

def test_simple_dislike():
    assert extract_taste_signals("我討厭香菜") == [("香菜", -REALTIME_TASTE_DELTA)]


def test_negation_is_dislike_not_like():
    # 「我不喜歡X」必須判成討厭，不能誤抓成喜歡 X
    sig = extract_taste_signals("我不喜歡下雨天")
    assert sig == [("下雨天", -REALTIME_TASTE_DELTA)]


def test_dislike_with_intensifier():
    assert _items("我超討厭塞車") == {"塞車"}


# ── 標點/語尾粒子清理 ────────────────────────────────────────────────────────

def test_stops_at_punctuation():
    assert _items("我喜歡爬山。今天天氣好") == {"爬山"}


def test_strips_trailing_particle():
    assert _items("我喜歡爬山啦") == {"爬山"}
    assert _items("我喜歡貓喔") == {"貓"}


# ── 不該誤觸發 ───────────────────────────────────────────────────────────────

def test_other_subject_not_matched():
    assert extract_taste_signals("你喜歡什麼") == []
    assert extract_taste_signals("他喜歡音樂") == []


def test_no_object_not_matched():
    assert extract_taste_signals("我喜歡") == []
    assert extract_taste_signals("我喜歡。") == []


def test_pronoun_object_skipped():
    # 「我喜歡你」不該記「你」當興趣項目
    assert extract_taste_signals("我喜歡你") == []


def test_empty_and_non_string_safe():
    assert extract_taste_signals("") == []
    assert extract_taste_signals(None) == []  # type: ignore[arg-type]
