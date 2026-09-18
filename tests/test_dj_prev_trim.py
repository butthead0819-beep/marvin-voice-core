"""dj_prev_trim.py 測試：DJ 口白超長時先拿掉「上一首」再截斷。"""
from __future__ import annotations

from dj_prev_trim import gate_dj_intro, name_keys, strip_prev_song_mention
from tts_length_policy import LIMITS, truncate_for_tts

TASK = "dj_story"


def est(s: str) -> float:
    return len(s) * 0.25


PREV_TITLE = "周杰倫 Jay Chou【聽媽媽的話 Listen to Mom】-Official Music Video"
NEXT_TITLE = "周杰倫 Jay Chou【印地安老斑鳩 Ancient Indian Turtledove】-Official Music Video"
PREV_TITLE_2 = NEXT_TITLE
NEXT_TITLE_2 = "五月天 Mayday【軋車 Motor Rock】Official Music Video"


def test_name_keys_extracts_cjk_and_english():
    keys = name_keys(PREV_TITLE)
    assert "聽媽媽" in keys
    assert "周杰倫" in keys
    assert "chou" in keys
    assert "official" not in keys
    assert "music" not in keys
    assert "video" not in keys


def test_strip_prev_song_mention_drops_prev_clause():
    text = (
        "聽完聽媽媽的話，接著這首印地安老斑鳩。"
        "六週前我們一起聽過，showay 應該還記得這節奏吧？"
        "別管那些煩人的想法了，跟著節奏放空比較實在。"
    )
    result = strip_prev_song_mention(text, PREV_TITLE, NEXT_TITLE)
    assert result.startswith("接著這首印地安老斑鳩。")
    assert "聽媽媽" not in result


def test_strip_prev_song_mention_second_example():
    text = "剛聽完老斑鳩的節奏，你們聊得正起勁，那這首五月天的《軋車》絕對適合現在的氣氛。"
    result = strip_prev_song_mention(text, PREV_TITLE_2, NEXT_TITLE_2)
    assert "老斑鳩" not in result
    assert "軋車" in result


def test_name_keys_drops_stopwords_that_survive_title_cleaning():
    """Live / Version / Topic 不會被 clean_title_regex 剝掉，要靠停用字擋，否則任何提到 live 的子句都被當成上一首。"""
    keys = name_keys("告白氣球 Live Version") | name_keys("稻香 Topic")
    assert not keys & {"live", "version", "topic"}


def test_strip_prev_song_mention_keeps_clause_mentioning_both():
    # 剩餘部分要 ≥10 字，否則「太短回原文」的 fallback 會蓋掉這條規則
    text = "從聽媽媽的話接到印地安老斑鳩，節奏剛好，大家跟著一起搖擺放空吧。"
    result = strip_prev_song_mention(text, PREV_TITLE, NEXT_TITLE)
    assert result == text


def test_strip_prev_song_mention_falls_back_when_too_short():
    text = "聽完聽媽媽的話。好。"
    result = strip_prev_song_mention(text, PREV_TITLE, NEXT_TITLE)
    assert result == text


def test_strip_prev_song_mention_no_mention_returns_original():
    text = "接著這首印地安老斑鳩，跟著節奏放空吧。"
    result = strip_prev_song_mention(text, PREV_TITLE, NEXT_TITLE)
    assert result == text


def test_strip_prev_song_mention_empty_prev_returns_original():
    text = "聽完聽媽媽的話，接著這首印地安老斑鳩。"
    result = strip_prev_song_mention(text, "", NEXT_TITLE)
    assert result == text


def test_gate_dj_intro_not_cut_leaves_text_untouched():
    text = "聽完聽媽媽的話，接著這首印地安老斑鳩，跟著節奏放空吧。"
    assert est(text) <= LIMITS[TASK]
    final_text, was_cut, prev_dropped = gate_dj_intro(text, PREV_TITLE, NEXT_TITLE, TASK, est)
    assert (final_text, was_cut, prev_dropped) == (text, False, False)


def test_gate_dj_intro_too_long_with_prev_mention_drops_prev():
    filler = "六週前我們一起聽過這節奏，跟著節奏放空比較實在，" * 3
    text = f"聽完聽媽媽的話，接著這首印地安老斑鳩。{filler}"
    assert est(text) > LIMITS[TASK]
    final_text, was_cut, prev_dropped = gate_dj_intro(text, PREV_TITLE, NEXT_TITLE, TASK, est)
    assert prev_dropped is True
    for key in name_keys(PREV_TITLE):
        assert key not in final_text.lower()
    assert "印地安老斑鳩" in final_text


def test_gate_dj_intro_too_long_without_prev_mention_matches_plain_truncate():
    filler = "跟著節奏放空比較實在，別管那些煩人的想法了，" * 3
    text = f"接著這首印地安老斑鳩。{filler}"
    assert est(text) > LIMITS[TASK]
    expected = truncate_for_tts(text, TASK, est)
    final_text, was_cut, prev_dropped = gate_dj_intro(text, PREV_TITLE, NEXT_TITLE, TASK, est)
    assert prev_dropped is False
    assert (final_text, was_cut) == expected


def test_gate_dj_intro_too_long_empty_prev_title():
    filler = "跟著節奏放空比較實在，別管那些煩人的想法了，" * 3
    text = f"接著這首印地安老斑鳩。{filler}"
    assert est(text) > LIMITS[TASK]
    final_text, was_cut, prev_dropped = gate_dj_intro(text, "", NEXT_TITLE, TASK, est)
    assert prev_dropped is False
