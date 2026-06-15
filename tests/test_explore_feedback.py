"""TDD: Step 3 反應回饋閘（2026-06-15）——讓驚喜自我校準。

retreat（負）：某藝人累計 ≥2 首不同歌被 skip → explore 避開該方向（藝人級）。
promotion（正）：有明顯反應（feelings）且沒被 skip 的歌 → 升級成 T2 seed
                （把「有中的驚喜」拉進未來探索種子）。
"""
from __future__ import annotations

from music_memory import MusicMemory


# ── retreat：藝人級 explore 避開 ─────────────────────────────────────────────

def test_artist_skip_avoid_after_two_distinct(tmp_path):
    mm = MusicMemory(path=str(tmp_path / "mm.json"))
    mm.record_artist_skip("某歌手", "https://youtu.be/aaaaaaaaaaa")
    assert mm.get_explore_avoid_artists() == []           # 1 首還不避
    mm.record_artist_skip("某歌手", "https://youtu.be/bbbbbbbbbbb")
    assert "某歌手" in mm.get_explore_avoid_artists()       # 2 首不同 → 避


def test_artist_skip_dedups_same_song(tmp_path):
    mm = MusicMemory(path=str(tmp_path / "mm.json"))
    mm.record_artist_skip("某歌手", "https://youtu.be/aaaaaaaaaaa")
    mm.record_artist_skip("某歌手", "https://www.youtube.com/watch?v=aaaaaaaaaaa")
    assert mm.get_explore_avoid_artists() == []           # 同一首 skip 兩次 ≠ 2 首


def test_artist_skip_survives_reload(tmp_path):
    p = str(tmp_path / "mm.json")
    m1 = MusicMemory(path=p)
    m1.record_artist_skip("某歌手", "https://youtu.be/aaaaaaaaaaa")
    m1.record_artist_skip("某歌手", "https://youtu.be/bbbbbbbbbbb")
    assert "某歌手" in MusicMemory(path=p).get_explore_avoid_artists()


def test_artist_skip_ignores_empty(tmp_path):
    mm = MusicMemory(path=str(tmp_path / "mm.json"))
    mm.record_artist_skip("", "https://youtu.be/aaaaaaaaaaa")     # 無藝人
    mm.record_artist_skip("某歌手", "https://example.com/x")        # 無 vid
    assert mm.get_explore_avoid_artists() == []


# ── promotion：有反應的歌升級成 seed ─────────────────────────────────────────

def _seed_reacted(mm, vid, feelings, requester="Marvin推薦（為x）"):
    url = f"https://www.youtube.com/watch?v={vid}"
    mm._data.setdefault("songs", {})[url] = {
        "title": vid, "webpage_url": url,
        "requesters": {requester: 1},
        "reactions": {"狗與露": {"feelings": feelings}},
    }


def test_reacted_seed_includes_songs_with_feelings(tmp_path):
    mm = MusicMemory(path=str(tmp_path / "mm.json"))
    _seed_reacted(mm, "aaaaaaaaaaa", ["感動", "懷念"])
    assert "aaaaaaaaaaa" in mm.get_reacted_seed_ids(["狗與露"])


def test_reacted_seed_excludes_no_feeling(tmp_path):
    mm = MusicMemory(path=str(tmp_path / "mm.json"))
    _seed_reacted(mm, "bbbbbbbbbbb", [])          # 無感受
    assert mm.get_reacted_seed_ids(["狗與露"]) == []


def test_reacted_seed_excludes_skipped(tmp_path):
    """有反應但被 skip → 不升級（負訊號覆蓋）。"""
    mm = MusicMemory(path=str(tmp_path / "mm.json"))
    _seed_reacted(mm, "ccccccccccc", ["喜歡"])
    mm.record_skipped_video_id("https://youtu.be/ccccccccccc")
    assert "ccccccccccc" not in mm.get_reacted_seed_ids(["狗與露"])


def test_reacted_seed_only_present_members(tmp_path):
    mm = MusicMemory(path=str(tmp_path / "mm.json"))
    _seed_reacted(mm, "ddddddddddd", ["喜歡"])   # 反應來自 狗與露
    assert mm.get_reacted_seed_ids(["showay"]) == []   # 狗與露不在場
