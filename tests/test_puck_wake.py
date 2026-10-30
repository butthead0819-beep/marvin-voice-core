"""puck_wake：無 prompt 路徑用拼音比對偵測喚醒詞（取代字面 prompt echo 比對）。"""
from __future__ import annotations

from puck_wake import pinyin_wake_match, strip_through_wake


def test_hit_hei_ma_wei():
    assert pinyin_wake_match("黑馬威下一首") is not None


def test_hit_ma_wei_alone():
    assert pinyin_wake_match("馬尾") is not None


def test_hit_hei_ma_wen():
    assert pinyin_wake_match("嘿，馬文，下一首") is not None


def test_hit_tie_ma_wen():
    assert pinyin_wake_match("貼碼溫") is not None


def test_hit_english_marvin():
    assert pinyin_wake_match("Hey Marvin next") is not None


def test_hit_ma_wan_with_space():
    assert pinyin_wake_match("馬 萬") is not None


def test_no_hit_plain_greeting():
    assert pinyin_wake_match("大家好") is None


def test_no_hit_ma_shang():
    assert pinyin_wake_match("馬上") is None


def test_no_hit_reversed_wen_ma():
    assert pinyin_wake_match("文馬") is None


def test_no_hit_ascii_letter_between():
    assert pinyin_wake_match("馬X文") is None


def test_no_hit_ma_lu_wen_hua():
    assert pinyin_wake_match("馬路文化") is None


def test_end_index_hei_ma_wen():
    assert pinyin_wake_match("嘿，馬文，下一首") == 4


def test_strip_through_wake_with_punct():
    assert strip_through_wake("嘿，馬文，下一首") == "下一首"


def test_strip_through_wake_no_punct():
    assert strip_through_wake("黑馬威下一首") == "下一首"


def test_strip_through_wake_english():
    assert strip_through_wake("Hey Marvin, play") == "play"


def test_strip_through_wake_no_match_strips_only_whitespace():
    assert strip_through_wake(" 大家好 ") == "大家好"
