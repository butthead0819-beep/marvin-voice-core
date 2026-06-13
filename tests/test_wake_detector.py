"""Tests for WakeDetector (merged wake-word logic)."""
from wake_detector import (
    WakeDetector,
    WAKE_WORDS_LIST,
    FAST_ONLY_WAKE_WORDS,
    WAKE_PATTERN,
    pre_filter_speech,
    check_cleaned_text_for_wake,
)
from utils import (
    WAKE_WORDS_LIST as utils_WAKE_WORDS_LIST,
    WAKE_PATTERN as utils_WAKE_PATTERN,
    pre_filter_speech as utils_pre_filter,
    check_cleaned_text_for_wake as utils_check_cleaned,
)


# ── Module-level constants ────────────────────────────────────────────────────

def test_wake_words_list_non_empty():
    assert len(WAKE_WORDS_LIST) > 5
    assert "馬文" in WAKE_WORDS_LIST


def test_wake_pattern_contains_all_words():
    for word in WAKE_WORDS_LIST[:5]:
        assert word in WAKE_PATTERN


def test_fast_only_words_not_in_main_list():
    for w in FAST_ONLY_WAKE_WORDS:
        assert w not in WAKE_WORDS_LIST


# ── utils.py re-export backward compat ───────────────────────────────────────

def test_utils_reexports_same_list():
    assert WAKE_WORDS_LIST is utils_WAKE_WORDS_LIST


def test_utils_reexports_same_pattern():
    assert WAKE_PATTERN == utils_WAKE_PATTERN


def test_utils_reexports_same_functions():
    assert utils_pre_filter is pre_filter_speech
    assert utils_check_cleaned is check_cleaned_text_for_wake


# ── pre_filter_speech ─────────────────────────────────────────────────────────

def test_pre_filter_returns_fast_intervene_on_sentence_start_wake():
    result = pre_filter_speech("嗨馬文你好")
    assert result["action"] == "fast_intervene"
    assert result["text"] == "嗨馬文你好"


def test_pre_filter_returns_fast_intervene_english():
    result = pre_filter_speech("marvin help me")
    assert result["action"] == "fast_intervene"


def test_pre_filter_catches_maowen_variant():
    """2026-06-13 SwiftV2 實測：「馬文這首誰唱的」被辨識成「毛文這首誰唱的」
    → 喚醒漏接。毛文與既有 馬聞/馬溫/馬問 同為 STT 聲學混淆變體。"""
    result = pre_filter_speech("毛文這首誰唱的")
    assert result["action"] != "drop"


def test_pre_filter_returns_drop_on_empty():
    assert pre_filter_speech("")["action"] == "drop"
    assert pre_filter_speech("   ")["action"] == "drop"


def test_pre_filter_returns_drop_on_irrelevant():
    assert pre_filter_speech("嗯")["action"] == "drop"


def test_pre_filter_returns_llm_verify_mid_sentence_wake():
    # Wake word not at sentence start
    result = pre_filter_speech("我剛才叫了馬文一聲")
    assert result["action"] == "llm_verify"


# ── check_cleaned_text_for_wake ───────────────────────────────────────────────

def test_check_cleaned_detects_wake_word():
    assert check_cleaned_text_for_wake("馬文，幫我查一下天氣") is True


def test_check_cleaned_rejects_no_wake_word():
    assert check_cleaned_text_for_wake("天氣今天怎麼樣") is False


# ── WakeDetector class ────────────────────────────────────────────────────────

def test_wake_detector_instantiates():
    wd = WakeDetector()
    assert wd is not None


def test_wake_detector_static_aliases():
    assert WakeDetector.pre_filter is pre_filter_speech
    assert WakeDetector.check_cleaned is check_cleaned_text_for_wake


def test_multi_channel_decide_wakes_on_fast_intervene_with_task():
    wd = WakeDetector()
    # fast_intervene voice + hard task intent = well above threshold
    should_wake, confidence, scores = wd.multi_channel_decide(
        action="fast_intervene",
        wake_intent=1.0,
        text="馬文幫我查天氣",
        speaker="TestUser",
        context_active=False,
    )
    assert should_wake is True
    assert confidence > 0.35
    assert "voice" in scores and "task" in scores


def test_multi_channel_decide_drops_no_signal():
    wd = WakeDetector()
    # No wake action, no task/control intent
    should_wake, confidence, scores = wd.multi_channel_decide(
        action="drop",
        wake_intent=None,
        text="今天天氣不錯",
        speaker="TestUser",
        context_active=False,
    )
    assert should_wake is False
    assert confidence < 0.35


def test_multi_channel_decide_independent_per_speaker():
    wd = WakeDetector()
    # Alice gets a strong signal; Bob does not
    wake_alice, _, _ = wd.multi_channel_decide(
        action="fast_intervene", wake_intent=1.0,
        text="馬文幫我", speaker="Alice", context_active=False,
    )
    wake_bob, _, _ = wd.multi_channel_decide(
        action="drop", wake_intent=None,
        text="嗯嗯", speaker="Bob", context_active=False,
    )
    assert wake_alice is True
    assert wake_bob is False


def test_wake_echo_re_catches_multi_wake_word_hallucination():
    """_WAKE_ECHO_RE finds 2+ wake words in a single STT output (echo loop pattern)."""
    import re
    from utils import WAKE_PATTERN
    wake_echo_re = re.compile(rf'({WAKE_PATTERN})', re.IGNORECASE)

    # 嗨馬文×3 + garbage — classic Swift echo loop
    assert len(wake_echo_re.findall("嗨馬文,apa 嗨馬文,incu 嗨馬文 譚語")) >= 2
    # 馬文 + 艾瑪文×2 — another echo pattern from today's log
    assert len(wake_echo_re.findall("馬文, 艾瑪文, 艾瑪文, 謝謝 謝謝 謝謝")) >= 2
    # 狗與露 log case
    assert len(wake_echo_re.findall("嗨馬文,啞馬文,佳馬文,啞馬文")) >= 2

    # Single wake word → should NOT be caught (not an echo loop)
    assert len(wake_echo_re.findall("嗨馬文幫我查天氣")) == 1
    assert len(wake_echo_re.findall("馬文播放戀曲1990")) == 1


def test_multi_channel_decide_returns_threshold_in_scores():
    wd = WakeDetector()
    _, _, scores = wd.multi_channel_decide(
        action="fast_intervene", wake_intent=1.0,
        text="馬文", speaker="X", context_active=False,
    )
    assert "threshold" in scores
    assert "total" in scores


# ── Echo guard window：Marvin 剛說完 0-2s 內提高 threshold ─────────────────────

def test_echo_window_raises_threshold(monkeypatch, tmp_path):
    """0-2s echo window：raise threshold by ECHO_PENALTY，擋掉 TTS 尾音/麥克回授。"""
    monkeypatch.setattr("wake_detector._STATS_FILE", str(tmp_path / "stats.json"))
    wd = WakeDetector()
    base = wd.get_threshold("Alice", context_active=False)
    echo = wd.get_threshold("Alice", context_active=False, marvin_in_echo_window=True)
    assert echo > base, f"echo window should raise (base={base}, echo={echo})"
    assert echo == round(base + wd.ECHO_PENALTY, 2)


def test_echo_window_overrides_just_spoke(monkeypatch, tmp_path):
    """echo window 與 just_spoke 同時為 True（不該發生，但容錯）→ echo 優先（raise）。"""
    monkeypatch.setattr("wake_detector._STATS_FILE", str(tmp_path / "stats.json"))
    wd = WakeDetector()
    th = wd.get_threshold("Alice", context_active=False,
                          marvin_just_spoke=True, marvin_in_echo_window=True)
    base = wd.BASE_THRESHOLD
    assert th == round(base + wd.ECHO_PENALTY, 2), \
        "echo window 必須蓋過 just_spoke bonus（防 echo 比 follow-up 重要）"


def test_just_spoke_still_lowers_threshold_outside_echo(monkeypatch, tmp_path):
    """2-15s 視窗（marvin_just_spoke=True, marvin_in_echo_window=False）行為不變：lower。"""
    monkeypatch.setattr("wake_detector._STATS_FILE", str(tmp_path / "stats.json"))
    wd = WakeDetector()
    base = wd.get_threshold("Alice", context_active=False)
    follow_up = wd.get_threshold("Alice", context_active=False, marvin_just_spoke=True)
    assert follow_up < base, f"follow-up window should still lower (base={base}, fu={follow_up})"


def test_decide_uses_echo_window(monkeypatch, tmp_path):
    """decide() 也要吃 marvin_in_echo_window。"""
    monkeypatch.setattr("wake_detector._STATS_FILE", str(tmp_path / "stats.json"))
    wd = WakeDetector()
    # base threshold 0.70；echo window 後變 0.80；intent=0.75 跨 base 不跨 echo
    wake_no_echo, _ = wd.decide(0.75, "Bob", context_active=False)
    wake_echo, th_echo = wd.decide(0.75, "Bob", context_active=False, marvin_in_echo_window=True)
    assert wake_no_echo is True
    assert wake_echo is False
    assert th_echo == 0.80
