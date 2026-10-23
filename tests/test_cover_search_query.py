"""TDD — 翻唱推薦改直接搜尋，不打 LLM（省免費層額度）。

`_llm_coverify`（原本問 LLM 推薦 cover 版本）改名 `_cover_search_query`，
本體只回「<歌名> 翻唱」當 YouTube 搜尋字串。
"""
from __future__ import annotations

from unittest.mock import MagicMock

from cogs.music_cog_autopilot import MusicAutopilotMixin
from music_recommender import Candidate


class _FakeCog(MusicAutopilotMixin):
    def __init__(self):
        self.bot = MagicMock()
        self.bot.router._call_llm = MagicMock()


def _cand(anchor_title: str) -> Candidate:
    return Candidate(
        anchor_title=anchor_title, anchor_artist="周杰倫", lane="spotlight",
        mode="cover", target_member=None, score=0.0,
    )


def test_cover_search_query_appends_cover_suffix():
    cog = _FakeCog()
    assert cog._cover_search_query(_cand("晴天")) == "晴天 翻唱"


def test_cover_search_query_empty_anchor_title_returns_empty():
    cog = _FakeCog()
    assert cog._cover_search_query(_cand("")) == ""


def test_cover_search_query_never_calls_llm():
    cog = _FakeCog()
    cog._cover_search_query(_cand("晴天"))
    cog.bot.router._call_llm.assert_not_called()
