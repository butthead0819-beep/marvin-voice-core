"""TDD — T4：推薦解釋接上 autopilot（cogs/music_cog.py::_compute_recommend_explanation）。

2026-08-20 實測發現並修正的 bug：解釋計算必須在 record_play() 之前完成，且必須用
anchor_title 正規化比對找歷史紀錄——不能用 mm._key(info)（resolve 後的 webpage_url）
直接查，因為同一首歌重新 yt-dlp 搜尋常常命中不同影片（同名不同上傳），照 key 查
會找到一個全新的空白 entry，把「現在正要播的這次」誤當成「你上次聽過」的證據。

換歌不等解釋：這裡改成同步純函式（Evidence 抽取本身不涉及網路/LLM I/O），在
_auto_recommend 候選迴圈裡直接算好存進 info['_explanation']，播放時直接讀，
不需要非同步 timeout 機制——播放本身完全不受影響。
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from music_recommender import Candidate


def _make_cog(songs=None):
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.music_memory = MagicMock()
    bot.music_memory.all_songs = MagicMock(return_value=songs or {})

    from cogs.music_cog import MusicCog
    return MusicCog(bot)


def _candidate(**overrides):
    defaults = dict(
        anchor_title="晴天", anchor_artist="周杰倫", lane="long_tail",
        mode="direct", target_member="jack", score=50.0,
    )
    defaults.update(overrides)
    return Candidate(**defaults)


class TestComputeRecommendExplanation:
    def test_no_lane_returns_none_immediately(self):
        cog = _make_cog()
        assert cog._compute_recommend_explanation(cog.bot.music_memory, _candidate(lane="")) is None

    def test_grounded_evidence_renders_explanation(self):
        songs = {
            "https://yt/a": {
                "title": "晴天", "total_plays": 3,
                "plays": [{"by": "jack", "ts": time.time() - 21 * 86400.0}],
                "requesters": {"jack": 3},
            },
        }
        cog = _make_cog(songs)
        text = cog._compute_recommend_explanation(cog.bot.music_memory, _candidate())
        assert isinstance(text, str) and text

    def test_title_lookup_survives_different_video_id_than_history(self):
        """核心 bug 修復：候選重新搜尋命中另一個 video-id（同名不同上傳），
        history 存在別的 key 底下，仍要能靠 normalize_title 找到。"""
        songs = {
            "https://yt/old-upload": {
                "title": "晴天", "total_plays": 5,
                "plays": [{"by": "jack", "ts": time.time() - 30 * 86400.0}],
                "requesters": {"jack": 5},
            },
            "https://yt/freshly-searched-different-upload": {
                "title": "晴天", "total_plays": 0, "plays": [], "requesters": {},
            },
        }
        cog = _make_cog(songs)
        text = cog._compute_recommend_explanation(cog.bot.music_memory, _candidate())
        assert text is not None  # 靠 total_plays 最多的那筆歷史，不是空白 entry

    def test_no_history_falls_back_to_discover_new_or_none(self):
        cog = _make_cog({})  # 完全沒有這首歌的紀錄
        text = cog._compute_recommend_explanation(cog.bot.music_memory, _candidate(lane="discovery"))
        assert text is None  # 沒 adjacent_artists 快取資料，fail-open 回 None，不拋例外

    def test_malformed_song_data_fails_open_returns_none(self):
        songs = {"https://yt/a": {"title": "晴天", "plays": "not-a-list", "requesters": {"jack": 1}}}
        cog = _make_cog(songs)
        assert cog._compute_recommend_explanation(cog.bot.music_memory, _candidate()) is None

    def test_no_music_memory_returns_none(self):
        bot = MagicMock()
        bot.guilds = []
        bot.voice_clients = []
        bot.cogs.get.return_value = None
        del bot.music_memory
        from cogs.music_cog import MusicCog
        cog = MusicCog(bot)
        assert cog._compute_recommend_explanation(None, _candidate()) is None


class TestFetchSongMetaNoLongerHandlesExplanation:
    """換歌不等解釋現在走同步路徑（見上）；_fetch_song_meta 不再參與解釋生成，
    確認 meta dict 沒有殘留 'explanation' 鍵、也不會因為解釋計算拖慢/中斷 meta fetch。"""

    @pytest.mark.asyncio
    async def test_meta_dict_has_no_explanation_key(self):
        from unittest.mock import AsyncMock
        bot = MagicMock()
        bot.guilds = []
        bot.voice_clients = []
        bot.cogs.get.return_value = None
        bot.music_memory = MagicMock()
        bot.music_memory.all_songs = MagicMock(return_value={})

        from cogs.music_cog import MusicCog
        cog = MusicCog(bot)
        cog._fetch_lyrics_raw = AsyncMock(return_value="L")
        cog._fetch_comment_raw = AsyncMock(return_value="C")
        cog._fetch_dj_interjection_raw = AsyncMock(return_value={"text": "D", "audio_path": None})
        cog._fetch_lyrics_synced = AsyncMock(return_value=None)

        meta = await cog._fetch_song_meta({"title": "晴天", "uploader": "周杰倫", "url": "x"})

        assert "explanation" not in meta
        assert meta["lyrics"] == "L"
        assert meta["comment"] == "C"
