"""
TDD tests for affirmative.is_affirmative()

測試範圍：
- 肯定詞（中文、英文）→ True
- 否定前綴（不、no）→ False
- 空字串、純空白 → False
- 完全無關的文字 → False
- STT 可能有額外字的 contains 情境 → True
"""

import pytest
from affirmative import is_affirmative


# ---- 肯定詞：應回傳 True ----

def test_yao_returns_true():
    assert is_affirmative("要") is True


def test_hao_returns_true():
    assert is_affirmative("好") is True


def test_keyi_returns_true():
    assert is_affirmative("可以") is True


def test_xing_returns_true():
    assert is_affirmative("行") is True


def test_en_returns_true():
    assert is_affirmative("嗯") is True


def test_haoah_returns_true():
    assert is_affirmative("好啊") is True


def test_haoya_returns_true():
    assert is_affirmative("好呀") is True


def test_keyiah_returns_true():
    assert is_affirmative("可以啊") is True


def test_xingah_returns_true():
    assert is_affirmative("行啊") is True


def test_yaoah_returns_true():
    assert is_affirmative("要啊") is True


def test_yeah_lowercase_returns_true():
    assert is_affirmative("yeah") is True


def test_ok_lowercase_returns_true():
    assert is_affirmative("ok") is True


def test_ok_mixed_returns_true():
    assert is_affirmative("OK") is True


def test_yes_lowercase_returns_true():
    assert is_affirmative("yes") is True


def test_yes_capitalized_returns_true():
    assert is_affirmative("Yes") is True


def test_yes_uppercase_returns_true():
    assert is_affirmative("YES") is True


# ---- 否定詞：應回傳 False ----

def test_buyao_returns_false():
    """「不要」含有「要」，必須先排除否定前綴"""
    assert is_affirmative("不要") is False


def test_buxing_returns_false():
    assert is_affirmative("不行") is False


def test_buhao_returns_false():
    assert is_affirmative("不好") is False


def test_suanle_returns_false():
    assert is_affirmative("算了") is False


def test_meiguanxi_returns_false():
    assert is_affirmative("沒關係") is False


def test_no_lowercase_returns_false():
    assert is_affirmative("no") is False


def test_no_capitalized_returns_false():
    assert is_affirmative("No") is False


def test_bu_returns_false():
    """單字「不」也是否定"""
    assert is_affirmative("不") is False


def test_buyongle_returns_false():
    assert is_affirmative("不用了") is False


# ---- 空白與無關文字：應回傳 False ----

def test_empty_string_returns_false():
    assert is_affirmative("") is False


def test_whitespace_only_returns_false():
    assert is_affirmative("   ") is False


def test_unrelated_gaming_returns_false():
    assert is_affirmative("繼續打遊戲") is False


def test_unrelated_wait_returns_false():
    assert is_affirmative("等一下") is False


# ---- STT 額外字的 contains 情境：應回傳 True ----

def test_affirmative_with_extra_stt_text_returns_true():
    """STT 可能轉錄出「好啊 我想要」這種有額外字的情況"""
    assert is_affirmative("好啊 我想要") is True


def test_ok_in_sentence_returns_true():
    """英文 OK 出現在句子中"""
    assert is_affirmative("ok sure") is True


def test_yao_with_trailing_text_returns_true():
    """「要 謝謝」"""
    assert is_affirmative("要 謝謝") is True


def test_en_with_trailing_text_returns_true():
    """「嗯嗯」也應視為肯定"""
    assert is_affirmative("嗯嗯") is True


# ---- 否定前綴在句子中：確保不誤判 ----

def test_buyao_in_sentence_returns_false():
    """「不要了 謝謝」仍是否定"""
    assert is_affirmative("不要了 謝謝") is False


def test_no_in_sentence_returns_false():
    """「no thanks」仍是否定"""
    assert is_affirmative("no thanks") is False
