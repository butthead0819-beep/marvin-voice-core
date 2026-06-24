"""TDD: 自動點播改用穩定 video-id 排除（2026-06-14 使用者回報「點過/skip 過
的歌一直重複」）。

根因：舊排除靠歌名字串（yt-dlp 同一支影片每次解析歌名會變 → 對不上）+
recently 只取本場 15 首（重啟清空）。

決策（使用者選）：
  - skip 過的歌 → 用 video-id **永久**排除（survives restart、不被 latest-wins 覆蓋）
  - 播過的歌 → **拉長視窗**排除（非永久，防候選枯竭；T3 回收層仍可放寬）
"""
from __future__ import annotations

import time

import pytest

from music_memory import MusicMemory, extract_video_id


# ── extract_video_id ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("url,expected", [
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=xx", "dQw4w9WgXcQ"),
    ("https://music.youtube.com/watch?v=abc12345678", "abc12345678"),
])
def test_extract_video_id_handles_common_forms(url, expected):
    assert extract_video_id(url) == expected


@pytest.mark.parametrize("url", ["", None, "https://example.com/song", "not a url"])
def test_extract_video_id_non_youtube_returns_none(url):
    assert extract_video_id(url) is None


# ── skip 永久排除 ────────────────────────────────────────────────────────────

def test_record_skip_persists_video_id(tmp_path):
    mm = MusicMemory(path=str(tmp_path / "mm.json"))
    mm.record_skipped_video_id("https://youtu.be/dQw4w9WgXcQ")
    assert "dQw4w9WgXcQ" in mm.get_skipped_video_ids()


def test_record_skip_survives_reload(tmp_path):
    """重啟（重新 load）後 skip 集仍在 → 永久排除。"""
    p = str(tmp_path / "mm.json")
    MusicMemory(path=p).record_skipped_video_id("https://youtu.be/dQw4w9WgXcQ")
    assert "dQw4w9WgXcQ" in MusicMemory(path=p).get_skipped_video_ids()


def test_record_skip_dedups(tmp_path):
    mm = MusicMemory(path=str(tmp_path / "mm.json"))
    mm.record_skipped_video_id("https://youtu.be/dQw4w9WgXcQ")
    mm.record_skipped_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert list(mm.get_skipped_video_ids()) == ["dQw4w9WgXcQ"]


def test_record_skip_ignores_non_youtube(tmp_path):
    mm = MusicMemory(path=str(tmp_path / "mm.json"))
    mm.record_skipped_video_id("https://example.com/x")
    mm.record_skipped_video_id("")
    assert mm.get_skipped_video_ids() == set()


# ── 播過拉長視窗（衍生自 songs 的 plays 時戳） ───────────────────────────────

def _seed_song(mm, vid, last_play_ts):
    url = f"https://www.youtube.com/watch?v={vid}"
    mm._data.setdefault("songs", {})[url] = {
        "title": vid, "webpage_url": url,
        "plays": [{"by": "u", "ts": last_play_ts}],
    }


def test_recently_played_includes_within_window(tmp_path):
    mm = MusicMemory(path=str(tmp_path / "mm.json"))
    _seed_song(mm, "aaaaaaaaaaa", time.time() - 3600)       # 1 小時前
    assert "aaaaaaaaaaa" in mm.get_recently_played_video_ids(ttl_s=7 * 24 * 3600)


def test_recently_played_excludes_outside_window(tmp_path):
    mm = MusicMemory(path=str(tmp_path / "mm.json"))
    _seed_song(mm, "bbbbbbbbbbb", time.time() - 30 * 24 * 3600)  # 30 天前
    assert "bbbbbbbbbbb" not in mm.get_recently_played_video_ids(ttl_s=7 * 24 * 3600)


def test_recently_played_empty_when_no_songs(tmp_path):
    mm = MusicMemory(path=str(tmp_path / "mm.json"))
    assert mm.get_recently_played_video_ids(ttl_s=7 * 24 * 3600) == set()


def test_t3_window_excludes_today_but_recycles_older(tmp_path):
    """T3 回收層用 24h 窗：擋今晚剛播的（防同場重播收斂），但回收 1-7 天前舊歌。
    2026-06-24 回報「鼓聲若響」同場 2hr 播 11 次——根因＝T3 把已播排除砍光只剩 skipped。"""
    from cogs.music_cog import MusicCog
    mm = MusicMemory(path=str(tmp_path / "mm.json"))
    _seed_song(mm, "tonightaaaa", time.time() - 2 * 3600)        # 今晚 2 小時前
    _seed_song(mm, "threedayss1", time.time() - 3 * 24 * 3600)   # 3 天前
    t3 = mm.get_recently_played_video_ids(MusicCog._T3_PLAYED_EXCLUDE_TTL_S)
    week = mm.get_recently_played_video_ids(MusicCog._PLAYED_EXCLUDE_TTL_S)
    assert "tonightaaaa" in t3        # 今晚剛播 → T3 也擋，不同場重播
    assert "threedayss1" not in t3    # 3 天前 → T3 放行回收
    assert "threedayss1" in week      # 但 T1/T2 七天窗仍排除
    assert MusicCog._T3_PLAYED_EXCLUDE_TTL_S < MusicCog._PLAYED_EXCLUDE_TTL_S


def test_recently_played_titles_aligns_with_video_ids(tmp_path):
    """T3 候選池要能用歌名排除剛播的，跟迴圈的 video-id 排除對齊；
    否則池子挑出剛播歌、迴圈又擋掉 → enqueue=0 → T3 無 fallback → 停播（2026-06-24 回報）。"""
    mm = MusicMemory(path=str(tmp_path / "mm.json"))
    mm._data.setdefault("songs", {})["https://www.youtube.com/watch?v=ccccccccccc"] = {
        "title": "今晚剛播的歌", "webpage_url": "https://www.youtube.com/watch?v=ccccccccccc",
        "plays": [{"by": "u", "ts": time.time() - 2 * 3600}],
    }
    titles = mm.get_recently_played_titles(24 * 3600)
    assert "今晚剛播的歌" in titles
    assert mm.get_recently_played_titles(3600) == []   # 1h 窗內無 → 空
