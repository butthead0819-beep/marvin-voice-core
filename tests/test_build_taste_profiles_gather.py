"""scripts/build_taste_profiles._gather — 個人歌單取材要依權重排序，不是 JSON 插入順序。

優化 #1（[[project_taste_review_explore_loop]] 口味優化順序第一項）：原本 `_gather` 對
music_memory 的歌一視同仁地照字典順序取前 25 首，聽過 1 次跟愛播 10 次的歌權重相同，
可能讓 LLM 看到的樣本不代表真正的口味。改成依該使用者的點播次數（`requesters` count）
降冪排序，song_history（無次數，只有標題）當補充訊號、依最近性（reversed）接在後面。
"""
from __future__ import annotations

from scripts.build_taste_profiles import _gather, _skipped_titles


def _song(title, requesters):
    return {"title": title, "requesters": requesters}


def test_gather_sorts_by_request_count_descending():
    mm = {
        "songs": {
            "u1": _song("冷門歌", {"showay": 1}),
            "u2": _song("最愛歌", {"showay": 9}),
            "u3": _song("普通歌", {"showay": 3}),
        }
    }
    sk = {"players": {}}
    titles, _likes = _gather("showay", mm, sk)
    assert titles == ["最愛歌", "普通歌", "冷門歌"]


def test_gather_excludes_marvin_recommended():
    mm = {
        "songs": {
            "u1": _song("真人點的", {"showay": 2}),
            "u2": _song("馬文推薦的", {"Marvin推薦（為showay）": 5}),
        }
    }
    sk = {"players": {}}
    titles, _likes = _gather("showay", mm, sk)
    assert titles == ["真人點的"]


def test_gather_song_history_appended_after_weighted_and_recency_reversed():
    mm = {"songs": {}}
    sk = {"players": {"showay": {"song_history": ["舊歌", "中間歌", "最近歌"]}}}
    titles, _likes = _gather("showay", mm, sk)
    # add_song_history 只 append 新的到尾巴 → 最近的在最後；取材該優先給最近的
    assert titles == ["最近歌", "中間歌", "舊歌"]


def test_gather_dedup_prefers_weighted_source_position():
    mm = {"songs": {"u1": _song("重複歌", {"showay": 5})}}
    sk = {"players": {"showay": {"song_history": ["重複歌", "其他歌"]}}}
    titles, _likes = _gather("showay", mm, sk)
    assert titles == ["重複歌", "其他歌"]
    assert titles.count("重複歌") == 1


# ── 優化 #2：skip 訊號 ──
def test_skipped_titles_latest_wins():
    mm = {
        "recommendations": {
            "showay": {
                "feedback": [
                    {"title": "歌A", "result": "skipped"},
                    {"title": "歌B", "result": "skipped"},
                    {"title": "歌B", "result": "liked"},   # 後來點回 → 覆蓋成非 skipped
                ]
            }
        }
    }
    assert _skipped_titles("showay", mm) == ["歌A"]


def test_skipped_titles_no_recommendations_empty():
    assert _skipped_titles("showay", {}) == []


def test_skipped_titles_other_user_not_leaked():
    mm = {"recommendations": {"大肚": {"feedback": [{"title": "歌A", "result": "skipped"}]}}}
    assert _skipped_titles("showay", mm) == []
