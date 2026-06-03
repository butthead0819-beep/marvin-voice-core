"""TDD — MusicMemory 自動推薦 novelty 支援：recent_recommendations ring + skipped 讀取。

ring 是 group-level、持久化（活過重啟），讓自動推薦的 exclude 不會因重啟而失憶。
"""
from __future__ import annotations

from music_memory import MusicMemory


def _mm(tmp_path):
    return MusicMemory(path=str(tmp_path / "mm.json"))


def test_recent_recommendation_ring_roundtrip(tmp_path):
    mm = _mm(tmp_path)
    mm.add_recent_recommendation("晴天")
    mm.add_recent_recommendation("七里香")
    assert mm.get_recent_recommendation_titles() == ["晴天", "七里香"]


def test_recent_recommendation_persists_across_reload(tmp_path):
    mm = _mm(tmp_path)
    mm.add_recent_recommendation("稻香")
    # 重新載入（模擬重啟）
    mm2 = MusicMemory(path=str(tmp_path / "mm.json"))
    assert "稻香" in mm2.get_recent_recommendation_titles()


def test_recent_recommendation_ring_capped(tmp_path):
    mm = _mm(tmp_path)
    for i in range(50):
        mm.add_recent_recommendation(f"歌{i}")
    titles = mm.get_recent_recommendation_titles()
    assert len(titles) == 40
    assert titles[-1] == "歌49"
    assert "歌0" not in titles


def test_recent_recommendation_ttl_filters_old_entries(tmp_path):
    """超過 TTL 的舊推薦不再算 exclude — 避免 ring 變永久黑名單把 recommender 餓死。

    每筆 entry 本就帶 ts；getter 須用 ts 做時間衰減，舊推薦過 TTL 後重新可選。
    """
    import time
    mm = _mm(tmp_path)
    now = time.time()
    mm._data["recent_recommendations"] = [
        {"title": "很久以前推過的", "ts": now - 25 * 3600},  # 25h 前 → 過 24h TTL
        {"title": "剛剛推過的", "ts": now - 1 * 3600},        # 1h 前 → 仍在窗內
    ]
    titles = mm.get_recent_recommendation_titles()
    assert "剛剛推過的" in titles
    assert "很久以前推過的" not in titles


def test_add_recent_recommendation_ignores_empty(tmp_path):
    mm = _mm(tmp_path)
    mm.add_recent_recommendation("")
    assert mm.get_recent_recommendation_titles() == []


def test_get_skipped_titles_collects_across_members(tmp_path):
    mm = _mm(tmp_path)
    mm.add_recommendation_feedback("Alice", "無聊歌A", "skipped")
    mm.add_recommendation_feedback("Alice", "喜歡歌", "liked")
    mm.add_recommendation_feedback("Bob", "無聊歌B", "skipped")
    skipped = mm.get_skipped_titles(["Alice", "Bob"])
    assert set(skipped) == {"無聊歌A", "無聊歌B"}
    assert "喜歡歌" not in skipped
