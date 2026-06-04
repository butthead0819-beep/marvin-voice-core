"""T2 seed 選擇：get_liked_video_ids（2026-06-04）。

在場者 liked 過的歌 → match 到 songs → 取 watch URL 的 videoId，當 ytmusic radio seed。
只用 liked（正向），skipped 不算 seed。
"""
from __future__ import annotations

from music_memory import MusicMemory


def _mm(tmp_path, data):
    mm = MusicMemory(path=str(tmp_path / "mm.json"))
    mm._data = data
    return mm


def test_liked_matches_songs_skipped_excluded(tmp_path):
    mm = _mm(tmp_path, {
        "songs": {
            "https://www.youtube.com/watch?v=AAAAAAAAAAA": {"title": "晴天"},
            "https://www.youtube.com/watch?v=BBBBBBBBBBB": {"title": "稻香"},
            "https://www.youtube.com/watch?v=CCCCCCCCCCC": {"title": "被skip的歌"},
        },
        "recommendations": {
            "alice": {"feedback": [
                {"title": "晴天 (Live)", "result": "liked"},   # 變體後綴 normalize 後仍 match
                {"title": "被skip的歌", "result": "skipped"},   # skipped 不當 seed
            ]},
        },
    })
    # 只有 liked 的晴天；skip 的不算；稻香無 feedback 不算
    assert mm.get_liked_video_ids(["alice"]) == ["AAAAAAAAAAA"]


def test_liked_dedup_and_multi_member(tmp_path):
    mm = _mm(tmp_path, {
        "songs": {
            "https://www.youtube.com/watch?v=AAAAAAAAAAA": {"title": "晴天"},
            "https://www.youtube.com/watch?v=BBBBBBBBBBB": {"title": "稻香"},
        },
        "recommendations": {
            "alice": {"feedback": [{"title": "晴天", "result": "liked"}]},
            "bob":   {"feedback": [{"title": "稻香", "result": "liked"},
                                   {"title": "晴天", "result": "liked"}]},   # 重複 → 去重
        },
    })
    vids = mm.get_liked_video_ids(["alice", "bob"])
    assert set(vids) == {"AAAAAAAAAAA", "BBBBBBBBBBB"}
    assert len(vids) == 2   # 去重


def test_liked_empty_when_none(tmp_path):
    mm = _mm(tmp_path, {"songs": {}, "recommendations": {}})
    assert mm.get_liked_video_ids(["alice"]) == []
