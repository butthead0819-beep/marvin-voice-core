"""T4 冒險發現改 per-member：抽在場「輪到的人」的 top 藝人（+排行榜輪替）。

需求（2026-07-08 使用者）：挖新歌時輪流以在場每個人的風格/歌手/排行榜。這裡測純抽藝人邏輯。
"""
from cogs.music_cog import MusicCog

_top = MusicCog._extract_top_artists


def test_dedupes_and_limits_preserving_order():
    songs = [
        {"title": "周杰倫 - 稻香 (官方MV)"},
        {"title": "周杰倫 - 七里香"},        # 同藝人 → 去重
        {"title": "陶喆 David Tao - 普通朋友"},
        {"title": "五月天 Mayday - 溫柔"},
    ]
    assert _top(songs, n=2) == ["周杰倫", "陶喆"]   # 去重+取前2+保序


def test_skips_empty_and_untitled():
    songs = [{"title": ""}, {"title": "陶喆 - 沙灘"}, {"other": "x"}]
    assert _top(songs, n=5) == ["陶喆"]


def test_empty_input_returns_empty():
    assert _top([], n=4) == []
