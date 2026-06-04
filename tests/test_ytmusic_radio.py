"""T2 Discovery PoC 測試 — ytmusic_radio parse/filter（純函式，無網路）。"""
from __future__ import annotations

import pytest

from ytmusic_radio import parse_length, parse_radio_tracks, ytmusic_radio


# fixture：仿 get_watch_playlist 真實回應（tracks[0] 是 seed），含一筆無 videoId 的髒資料
SEED = "XsUjqoSb5bo"
_WP = {
    "tracks": [
        {"videoId": SEED, "title": "知影", "artists": [{"name": "莫宰羊"}], "length": "3:20"},
        {"videoId": "ZjU0FRd9c9g", "title": "健康快樂", "artists": [{"name": "莫宰羊"}], "length": "4:05"},
        {"videoId": "f5qijcIf-9w", "title": "Fight With The Demon",
         "artists": [{"name": "Marz23"}, {"name": "Goater"}], "length": "3:12"},
        {"videoId": None, "title": "壞資料無 vid", "artists": []},      # 應丟掉
        {"videoId": "abc12345678", "title": "", "artists": []},           # 空 title 丟掉
    ]
}


class _FakeYT:
    def __init__(self, wp):
        self._wp = wp
        self.called_with = None
    def get_watch_playlist(self, **kw):
        self.called_with = kw
        return self._wp


# ── parse_length ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("s,expect", [
    ("3:45", 225), ("1:02:03", 3723), ("0:30", 30),
    (None, 0), ("", 0), ("亂碼", 0), ("3:xx", 0),
])
def test_parse_length(s, expect):
    assert parse_length(s) == expect


# ── parse_radio_tracks ────────────────────────────────────────────────────────

def test_parse_drops_seed_and_dirty_rows():
    cands = parse_radio_tracks(_WP, seed_video_id=SEED)
    titles = [c["title"] for c in cands]
    assert titles == ["健康快樂", "Fight With The Demon"]   # seed + 無vid + 空title 都丟
    c0 = cands[0]
    assert c0["video_id"] == "ZjU0FRd9c9g"
    assert c0["url"] == "https://www.youtube.com/watch?v=ZjU0FRd9c9g"
    assert c0["duration_s"] == 245
    assert cands[1]["artist"] == "Marz23, Goater"      # 多藝人串接


def test_parse_empty_response():
    assert parse_radio_tracks({}, seed_video_id=SEED) == []
    assert parse_radio_tracks({"tracks": None}) == []


# ── ytmusic_radio（IO shell，client 注入）─────────────────────────────────────

def test_radio_returns_related_new_songs():
    yt = _FakeYT(_WP)
    out = ytmusic_radio(SEED, client=yt)
    assert [c["title"] for c in out] == ["健康快樂", "Fight With The Demon"]
    # 用對 RDAMVM playlistId（避開 KeyError 'endpoint' 那條路）
    assert yt.called_with == {"videoId": SEED, "playlistId": "RDAMVM" + SEED}


def test_radio_excludes_skipped_and_played():
    yt = _FakeYT(_WP)
    out = ytmusic_radio(SEED, exclude_titles=["健康快樂"], client=yt)
    assert [c["title"] for c in out] == ["Fight With The Demon"]   # skipped 那首被擋


def test_radio_respects_limit():
    yt = _FakeYT(_WP)
    out = ytmusic_radio(SEED, limit=1, client=yt)
    assert len(out) == 1


def test_radio_empty_seed_returns_empty():
    assert ytmusic_radio("", client=_FakeYT(_WP)) == []


def test_radio_client_failure_is_graceful():
    class _Boom:
        def get_watch_playlist(self, **kw):
            raise RuntimeError("InnerTube 500")
    assert ytmusic_radio(SEED, client=_Boom()) == []   # 失敗回 []，讓上層退 T3
