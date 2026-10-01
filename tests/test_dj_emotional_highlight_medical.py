"""TDD — _recent_emotional_highlight 濾掉醫療健康類素材（同 dj_daily_highlight.MEDICAL_KEYWORDS）。

2026-10-01 事故：Marvin 自己的 emotional_highlights 素材「對家人健康狀況感到擔憂」
被 DJ 串場寫成「昨晚你在醫院門口等人…手術室…」。_recent_emotional_highlight 原本
沒有醫療過濾，應跳過該則、往更舊的找。
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest


def _make_cog():
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None

    from cogs.music_cog import MusicCog
    cog = MusicCog(bot)
    return cog


def _highlight(moment, age_s=0.0, valence="warm"):
    return {"moment": moment, "valence": valence, "timestamp": time.time() - age_s}


def test_only_medical_highlight_returns_empty():
    cog = _make_cog()
    cog.bot.router.memory.get_player_memory.return_value = {
        "emotional_highlights": [_highlight("對家人健康狀況感到擔憂", age_s=10)],
    }
    assert cog._recent_emotional_highlight("大肚") == ""


def test_older_normal_highlight_used_when_newest_is_medical():
    cog = _make_cog()
    cog.bot.router.memory.get_player_memory.return_value = {
        "emotional_highlights": [
            _highlight("被大家的笑聲感染", age_s=100),
            _highlight("聽說他住院了", age_s=10),
        ],
    }
    assert cog._recent_emotional_highlight("大肚") == "被大家的笑聲感染"


def test_normal_highlight_returned_as_is():
    cog = _make_cog()
    cog.bot.router.memory.get_player_memory.return_value = {
        "emotional_highlights": [_highlight("被大家的笑聲感染", age_s=10)],
    }
    assert cog._recent_emotional_highlight("大肚") == "被大家的笑聲感染"
