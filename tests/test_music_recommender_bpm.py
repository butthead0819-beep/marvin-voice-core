"""TDD：DJ 選歌加入 BPM 鄰近 soft re-rank（見 bpm_estimate.py 取樣落地）。

跟 vibe_filter 同一種設計：純函式注入 bpm_filter={"current_bpm":.., "store":..}，
候選歌 BPM 越接近目前播放歌 → boost 越高；任一沒 BPM 記錄（新歌/沒取樣過）→ 0，
不影響原排序（新資料只加分不扣分，漸進式覆蓋）。
"""
from __future__ import annotations

from music_recommender import _bpm_boost, build_member_pools

NOW = 1_700_000_000.0
DAY = 86400.0


def _song(title, *, requesters=None, webpage_url="", last_play_age_days=0.0):
    ts = NOW - last_play_age_days * DAY
    return {
        "title": title,
        "uploader": "orig",
        "webpage_url": webpage_url,
        "requesters": dict(requesters or {}),
        "plays": [{"by": b, "ts": ts} for b in (requesters or {"a": 1})],
        "connections": [],
    }


VID_CLOSE = "https://www.youtube.com/watch?v=aaaaaaaaaaa"
VID_FAR = "https://www.youtube.com/watch?v=bbbbbbbbbbb"
VID_UNKNOWN = "https://www.youtube.com/watch?v=ccccccccccc"

STORE = {
    "aaaaaaaaaaa": {"bpm": 120.0},
    "bbbbbbbbbbb": {"bpm": 80.0},
}


class TestBpmBoost:
    def test_no_filter_returns_zero(self):
        song = _song("A", webpage_url=VID_CLOSE)
        assert _bpm_boost(song, None) == 0.0

    def test_filter_without_current_bpm_returns_zero(self):
        song = _song("A", webpage_url=VID_CLOSE)
        assert _bpm_boost(song, {"store": STORE}) == 0.0

    def test_song_without_bpm_entry_returns_zero(self):
        song = _song("A", webpage_url=VID_UNKNOWN)
        assert _bpm_boost(song, {"current_bpm": 120.0, "store": STORE}) == 0.0

    def test_closer_bpm_scores_higher_than_farther_bpm(self):
        close = _song("close", webpage_url=VID_CLOSE)
        far = _song("far", webpage_url=VID_FAR)
        filt = {"current_bpm": 122.0, "store": STORE}
        assert _bpm_boost(close, filt) > _bpm_boost(far, filt)

    def test_beyond_tolerance_returns_zero(self):
        song = _song("far", webpage_url=VID_FAR)
        filt = {"current_bpm": 200.0, "store": STORE}  # 差 120，遠超容忍帶
        assert _bpm_boost(song, filt) == 0.0

    def test_exact_match_scores_positive(self):
        song = _song("close", webpage_url=VID_CLOSE)
        filt = {"current_bpm": 120.0, "store": STORE}
        assert _bpm_boost(song, filt) > 0.0


def test_build_member_pools_reranks_long_tail_by_bpm_proximity():
    songs = {
        "close": _song("close", requesters={"a": 3}, webpage_url=VID_CLOSE, last_play_age_days=10),
        "far": _song("far", requesters={"a": 3}, webpage_url=VID_FAR, last_play_age_days=10),
    }
    # 沒有 bpm_filter：兩首 long_tail base 分相同（同 requester 次數/age），排序由 dict 序決定
    no_filter_pool = build_member_pools(members=["a"], songs=songs, exclude_titles=[], now=NOW)["a"]
    assert {c.anchor_title for c in no_filter_pool} == {"close", "far"}

    bpm_filter = {"current_bpm": 122.0, "store": STORE}
    pool = build_member_pools(
        members=["a"], songs=songs, exclude_titles=[], now=NOW, bpm_filter=bpm_filter,
    )["a"]
    assert pool[0].anchor_title == "close"


def test_build_member_pools_bpm_filter_none_does_not_change_scores():
    songs = {
        "close": _song("close", requesters={"a": 3}, webpage_url=VID_CLOSE, last_play_age_days=10),
    }
    pool_no_filter = build_member_pools(members=["a"], songs=songs, exclude_titles=[], now=NOW)["a"]
    pool_explicit_none = build_member_pools(
        members=["a"], songs=songs, exclude_titles=[], now=NOW, bpm_filter=None,
    )["a"]
    assert pool_no_filter[0].score == pool_explicit_none[0].score
