"""TDD: undo_play —— 誤點救回（2026-07-01 使用者要求）。

情境：偶爾點錯的歌播出來時，要能當下把它從記憶抹去，避免污染口味指紋
（_human_plays 從 requesters 算）與 autopilot 種子。

undo_play(info) 反向抵銷最近一次 record_play：
  - pop 掉最後一筆 plays，total_plays -1
  - requesters[點播者] -1，歸零則移除該 key
  - 已無真人播放且無 reactions → 整首移除（不再當推薦種子）
  - 找不到這首 → 回 False（no-op）
"""
from __future__ import annotations

import pytest

from music_memory import MusicMemory
from taste_fingerprint import compute_taste_fingerprint


def _info(vid="dQw4w9WgXcQ", title="不小心點到的歌", uploader="某藝人"):
    return {
        "title": title,
        "uploader": uploader,
        "webpage_url": f"https://www.youtube.com/watch?v={vid}",
    }


# ── 基本反向抵銷 ──────────────────────────────────────────────────────────────

def test_undo_play_removes_last_play_and_decrements_counts(tmp_path):
    mm = MusicMemory(path=str(tmp_path / "mm.json"))
    info = _info()
    mm.record_play(info, "阿明")
    mm.record_play(info, "阿明")   # 播了兩次
    assert mm.undo_play(info) is True

    s = mm._data["songs"][mm._key(info)]
    assert s["total_plays"] == 1
    assert len(s["plays"]) == 1
    assert s["requesters"]["阿明"] == 1


def test_undo_play_removes_requester_key_when_hits_zero(tmp_path):
    mm = MusicMemory(path=str(tmp_path / "mm.json"))
    info = _info()
    mm.record_play(info, "阿明")
    mm.undo_play(info)
    s = mm._data["songs"].get(mm._key(info))
    # 只播過一次 → 抹除後整首移除
    assert s is None


def test_undo_play_drops_song_when_no_human_plays_left(tmp_path):
    """唯一一次播放被抹除 → 整筆從 songs 移除，不再當 autopilot 種子。"""
    mm = MusicMemory(path=str(tmp_path / "mm.json"))
    info = _info()
    mm.record_play(info, "阿明")
    mm.undo_play(info)
    assert mm._key(info) not in mm._data["songs"]


def test_undo_play_keeps_song_when_reactions_exist(tmp_path):
    """有情緒共鳴紀錄的歌不整筆刪（保留反應），但真人播放計數仍歸零。"""
    mm = MusicMemory(path=str(tmp_path / "mm.json"))
    info = _info()
    mm.record_play(info, "阿明")
    mm.record_reactions(info, {"阿明": {"feelings": ["感動"], "quotes": [], "lyric_match": ""}})
    mm.undo_play(info)
    s = mm._data["songs"].get(mm._key(info))
    assert s is not None
    assert s.get("reactions")
    assert "阿明" not in s.get("requesters", {})


# ── de-poison 口味指紋 ────────────────────────────────────────────────────────

def test_undo_play_depoisons_taste_fingerprint(tmp_path):
    mm = MusicMemory(path=str(tmp_path / "mm.json"))
    good = _info(vid="aaaaaaaaaaa", title="常聽的歌")
    bad = _info(vid="bbbbbbbbbbb", title="手滑點到的歌")
    mm.record_play(good, "阿明")
    mm.record_play(bad, "阿明")

    mm.undo_play(bad)
    fp = compute_taste_fingerprint(mm._data["songs"])
    assert fp["distinct_songs"] == 1
    assert fp["total_human_requests"] == 1


# ── no-op / 持久化 ────────────────────────────────────────────────────────────

def test_undo_play_unknown_song_is_noop(tmp_path):
    mm = MusicMemory(path=str(tmp_path / "mm.json"))
    assert mm.undo_play(_info()) is False


def test_undo_play_survives_reload(tmp_path):
    p = str(tmp_path / "mm.json")
    info = _info()
    mm = MusicMemory(path=p)
    mm.record_play(info, "阿明")
    mm.record_play(info, "阿明")
    mm.undo_play(info)
    # 重新 load → 抹除結果已持久化
    reloaded = MusicMemory(path=p)
    s = reloaded._data["songs"][reloaded._key(info)]
    assert s["total_plays"] == 1
    assert s["requesters"]["阿明"] == 1
