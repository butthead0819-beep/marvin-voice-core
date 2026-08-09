"""wake_detector.WAKE_WORDS_LIST 跟 wake_intent_gate._WAKE_WORDS 必須衍生自
wake_words_data.WAKE_WORD_ENTRIES 這同一份底層資料，不是各自寫死湊巧重疊。"""
from wake_words_data import WAKE_WORD_ENTRIES, words_for
from wake_detector import WAKE_WORDS_LIST
from wake_intent_gate import _WAKE_WORDS


def test_detector_list_matches_entries_tagged_detector():
    expected = [word for word, _cat, consumers in WAKE_WORD_ENTRIES if "detector" in consumers]
    # WAKE_WORDS_LIST 可能被 records/wake_words_override.json 動態增刪，
    # 但底層 entries 標 detector 的詞一定要全部在裡面（衍生關係，非湊巧重疊）
    for word in expected:
        assert word in WAKE_WORDS_LIST


def test_gate_words_match_entries_tagged_gate():
    expected = frozenset(word for word, _cat, consumers in WAKE_WORD_ENTRIES if "gate" in consumers)
    assert _WAKE_WORDS == expected


def test_shared_words_are_literally_the_same_entry():
    """同時被兩邊使用的詞（如「馬文」「marvin」「麻文」）必須是同一筆 entry 標兩個 consumer，
    不是兩邊各自寫一份長得一樣的字串（避免未來改一邊漏改另一邊）。"""
    shared = set(words_for("detector")) & set(words_for("gate"))
    assert shared, "至少要有共用詞才驗證得出衍生關係，不是巧合重疊"
    for word in shared:
        matches = [consumers for w, _cat, consumers in WAKE_WORD_ENTRIES if w == word]
        assert len(matches) == 1, f"「{word}」不該在 WAKE_WORD_ENTRIES 裡出現超過一次"
        assert "detector" in matches[0] and "gate" in matches[0]


def test_no_duplicate_entries():
    words = [word for word, _cat, _consumers in WAKE_WORD_ENTRIES]
    assert len(words) == len(set(words))
