"""TDD: Threads 風格生活小搞笑與日常觀察庫測試。

驗證：
1. 搞笑短文庫每條字數符合 30-55 字標準（適合 9 秒 crossfade）。
2. 風格無空泛假文青詞（「時光流動」「歲月靜好」），語句通順。
3. 取得隨機台詞或帶歌名台詞。
4. 新聞快報模板格式正確且在字數內。
"""
from __future__ import annotations

import pytest
from dj_comedy_fallback import (
    COMEDY_FALLBACK_SCRIPTS,
    get_comedy_fallback,
    build_news_interjection_template,
)

FORBIDDEN_WORDS = ["時光流動", "歲月靜好", "撫平心靈", "流淌的旋律", "身為AI", "大家好我是"]


def test_comedy_fallback_scripts_length_and_quality():
    """所有預設搞笑段子字數應在 30-55 字之間，且不得包含假文青禁詞。"""
    assert len(COMEDY_FALLBACK_SCRIPTS) >= 20
    for script in COMEDY_FALLBACK_SCRIPTS:
        # 去除空白後計算長度
        clean = script.replace(" ", "").replace("\n", "")
        assert 30 <= len(clean) <= 55, f"長度不合規 ({len(clean)}字): {script}"
        for fw in FORBIDDEN_WORDS:
            assert fw not in script, f"包含禁詞 {fw}: {script}"


def test_get_comedy_fallback_returns_valid_string():
    """測試抽取搞笑台詞。"""
    s = get_comedy_fallback()
    assert isinstance(s, str)
    assert len(s) >= 30


def test_build_news_interjection_template():
    """測試新聞快報接歌模板。"""
    headline = "氣象署發布週末好天氣特報"
    text = build_news_interjection_template(headline, song_title="晴天")
    assert "氣象署發布週末好天氣特報" in text
    assert len(text) <= 55
    assert len(text) >= 20
