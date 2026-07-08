"""T4 fresh discovery — ytmusic_search_songs parse/filter（純函式，無網路）。"""
from __future__ import annotations

from ytmusic_radio import parse_search_songs, ytmusic_search_songs


# 仿 ytmusicapi search(filter="songs") 真實回應，含髒資料
_RESULTS = [
    {"resultType": "song", "title": "稻香", "artists": [{"name": "周杰倫"}],
     "videoId": "aaa11111111", "duration": "3:43", "duration_seconds": 223},
    {"resultType": "song", "title": "七里香", "artists": [{"name": "周杰倫"}],
     "videoId": "bbb22222222", "duration": "4:59"},
    {"resultType": "song", "title": "無 vid", "artists": [{"name": "周杰倫"}], "videoId": None},
    {"resultType": "song", "title": "", "artists": [], "videoId": "ccc33333333"},
]


class _FakeYT:
    def __init__(self, results):
        self._r = results
        self.called_with = None

    def search(self, query, **kw):
        self.called_with = (query, kw)
        return self._r


def test_parse_drops_dirty_and_keeps_fields():
    out = parse_search_songs(_RESULTS)
    assert len(out) == 2                          # 丟無 vid + 空 title
    assert out[0]["title"] == "稻香"
    assert out[0]["artist"] == "周杰倫"
    assert out[0]["video_id"] == "aaa11111111"
    assert out[0]["url"].endswith("aaa11111111")
    assert out[0]["duration_s"] == 223            # 用 duration_seconds
    assert out[1]["duration_s"] == 299            # fallback 解析 "4:59"


def test_search_uses_songs_filter_and_excludes():
    yt = _FakeYT(_RESULTS)
    out = ytmusic_search_songs("周杰倫", exclude_titles=["稻香"], client=yt)
    titles = {c["title"] for c in out}
    assert "稻香" not in titles                    # 已播排除
    assert "七里香" in titles
    assert yt.called_with[1].get("filter") == "songs"


def test_search_empty_query_returns_empty():
    assert ytmusic_search_songs("", client=_FakeYT(_RESULTS)) == []


def test_search_client_failure_returns_empty():
    class _BoomYT:
        def search(self, *a, **k):
            raise RuntimeError("network down")
    assert ytmusic_search_songs("周杰倫", client=_BoomYT()) == []
