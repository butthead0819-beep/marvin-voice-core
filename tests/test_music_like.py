"""Like 功能：song['likes'] 明確按讚訊號 + autopilot 候選擴散到 liker（次於點播者計分）。

需求（2026-07-08）：控制卡加 Like 按鈕→記誰喜歡；autopilot 候選判定從「iff M in requesters」
擴成「M in requesters ∪ likes」，讓歌曲喜好不只屬點播者。likes 計分次於點播者（點播>按讚>純在場）。
"""
import time

from music_memory import MusicMemory
from music_recommender import build_member_pools


def _info(vid="dQw4w9WgXcQ", title="稻香"):
    return {"title": title, "uploader": "周杰倫", "url": "http://s/x",
            "webpage_url": f"https://youtu.be/{vid}"}


# ── song['likes'] toggle ─────────────────────────────────────────────

def test_toggle_like_adds_then_removes(tmp_path):
    mm = MusicMemory(path=str(tmp_path / "mm.json"))
    info = _info()
    mm.record_play(info, "狗與露")               # 歌要播過(存在)才能讚
    assert mm.toggle_like(info, "大肚") is True    # 按讚
    assert "大肚" in mm._data["songs"][mm._key(info)]["likes"]
    assert mm.toggle_like(info, "大肚") is False   # 再按=取消
    assert "大肚" not in mm._data["songs"][mm._key(info)]["likes"]


def test_toggle_like_unknown_song_returns_none(tmp_path):
    mm = MusicMemory(path=str(tmp_path / "mm.json"))
    assert mm.toggle_like(_info(), "大肚") is None  # 沒播過的歌不能讚


def test_toggle_like_empty_user_none(tmp_path):
    mm = MusicMemory(path=str(tmp_path / "mm.json"))
    info = _info()
    mm.record_play(info, "x")
    assert mm.toggle_like(info, "") is None


# ── build_member_pools 用 likes ──────────────────────────────────────

def test_liked_song_is_candidate_scored_below_requested():
    now = time.time()
    songs = {
        "s1": {"title": "他點過的", "uploader": "a", "requesters": {"大肚": 3},
               "plays": [{"ts": now}], "connections": []},
        "s2": {"title": "他只按讚的", "uploader": "b", "requesters": {}, "likes": {"大肚": now},
               "plays": [{"ts": now}], "connections": []},
    }
    pools = build_member_pools(members=["大肚"], songs=songs, exclude_titles=[], now=now)
    titles = {c.anchor_title for c in pools["大肚"]}
    assert "他只按讚的" in titles                    # 按讚的歌也成候選（喜好擴散）
    score = {c.anchor_title: c.score for c in pools["大肚"]}
    assert score["他點過的"] > score["他只按讚的"]     # 點播 > 按讚


def test_song_without_likes_or_requesters_not_candidate():
    now = time.time()
    songs = {"s": {"title": "沒人點沒人讚", "uploader": "a", "requesters": {},
                   "plays": [{"ts": now}], "connections": []}}
    pools = build_member_pools(members=["大肚"], songs=songs, exclude_titles=[], now=now)
    assert pools["大肚"] == []
