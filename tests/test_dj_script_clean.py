"""dj_script_clean.py 測試：清掉 LLM DJ 口白雜訊（舞台指示/markdown/怪引號/外層「」/換行）。

9/30 使用者定：清雜訊後多長都完整播出，不截斷、不因超長退墊底。
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from dj_script_clean import clean_dj_script


def test_strips_leading_stage_direction_with_newlines():
    text = "（歌聲剛落，調整音量，語氣親近又帶點小調侃）\n\n「水瓶座的你，手機充電了嗎？」"
    assert clean_dj_script(text) == "水瓶座的你，手機充電了嗎？"


def test_strips_asterisk_wrapped_leading_stage_direction():
    text = "*（歌聲剛落，馬上開口）*\n\n「陳進文，床墊到了嗎？」"
    assert clean_dj_script(text) == "陳進文，床墊到了嗎？"


def test_keeps_mid_sentence_song_name_parens():
    text = "這首《體面 (Live)》送給大肚。"
    assert clean_dj_script(text) == text


def test_keeps_leading_parens_when_containing_song_name_mark():
    text = "（《夜曲》前奏）接下來這首"
    assert clean_dj_script(text) == text


def test_strips_asterisks_and_curly_low_quote_only():
    text = "這首聽起來像在說：*„為什麼愛情這麼難？*“"
    assert clean_dj_script(text) == "這首聽起來像在說：為什麼愛情這麼難？“"


def test_keeps_outer_quotes_when_inner_quotes_present():
    text = "「他說『好』」然後「走了」"
    assert clean_dj_script(text) == text


def test_removes_whole_line_paren_only_lines():
    text = "第一句。\n（停頓）\n第二句。"
    assert clean_dj_script(text) == "第一句。第二句。"


def test_empty_and_none():
    assert clean_dj_script("") == ""
    assert clean_dj_script(None) == ""


# ── _fetch_dj_interjection_raw：清雜訊後不截斷、不因超長退墊底 ─────────────

@pytest.mark.asyncio
async def test_fetch_dj_interjection_keeps_long_clean_script_in_full(monkeypatch):
    from tests.test_dj_story_context import _exclude, _info, _make_cog

    _exclude(monkeypatch, "quick")
    cog = _make_cog()
    long_text = (
        "今天早上的風有點涼，大家出門記得多帶一件外套，別跟我一樣只穿短袖就衝出去。"
        "說到這種涼涼的天氣，就很適合來一首慢慢的歌，讓心情跟著節奏放鬆一下。"
        "接下來這首周杰倫的夜曲，送給點歌的大肚，也送給還在路上的你，希望你今天也順順利利，"
        "回家路上小心慢慢開，晚點見。"
    )
    assert len(long_text) > 120
    cog.bot.router.generate_dynamic_system_msg = AsyncMock(return_value=long_text)

    result = await cog._fetch_dj_interjection_raw(_info())

    assert result["text"] == long_text


@pytest.mark.asyncio
async def test_fetch_dj_interjection_strips_stage_direction_noise(monkeypatch):
    from tests.test_dj_story_context import _exclude, _info, _make_cog

    _exclude(monkeypatch, "quick")
    cog = _make_cog()
    raw = "（歌聲剛落，調整音量，語氣親近又帶點小調侃）\n\n「接下來這首夜曲，陪還在路上的你。」"
    cog.bot.router.generate_dynamic_system_msg = AsyncMock(return_value=raw)

    result = await cog._fetch_dj_interjection_raw(_info())

    assert "歌聲剛落" not in result["text"]
    assert result["text"] == "接下來這首夜曲，陪還在路上的你。"
