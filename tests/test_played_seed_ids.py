"""MusicMemory.get_played_seed_ids — 用點播史(真人點的)當 T2 radio 多 seed 來源。

比 get_liked_video_ids 更廣（liked 稀疏）；關鍵守則：排除「Marvin推薦」自薦
（避免回音室，[[skip_signal_attribution]] 同精神：只用真人正向訊號），按點播次數加權。
"""
from __future__ import annotations

from music_memory import MusicMemory


def _mm(tmp_path):
    return MusicMemory(path=str(tmp_path / "mm.json"))


def _play(mm, vid, title, by, times=1):
    info = {"title": title, "webpage_url": f"https://www.youtube.com/watch?v={vid}"}
    for _ in range(times):
        mm.record_play(info, by)


# ── 1. 排除 Marvin 自薦 + 按次數加權 ──
def test_excludes_marvin_recs_and_weights_by_count(tmp_path):
    mm = _mm(tmp_path)
    _play(mm, "aaaaaaaaaaa", "歌A", "狗與露", times=3)
    _play(mm, "bbbbbbbbbbb", "歌B", "showay", times=1)
    _play(mm, "ccccccccccc", "歌C", "Marvin推薦（為狗與露）", times=5)  # 只有 Marvin → 排除
    seeds = mm.get_played_seed_ids(["狗與露", "showay"], limit=10)
    assert "ccccccccccc" not in seeds
    assert seeds[0] == "aaaaaaaaaaa"     # 3x 權重最高排前
    assert "bbbbbbbbbbb" in seeds


# ── 2. 只算在場成員 ──
def test_filters_to_present_members(tmp_path):
    mm = _mm(tmp_path)
    _play(mm, "aaaaaaaaaaa", "歌A", "狗與露")
    _play(mm, "bbbbbbbbbbb", "歌B", "不在場的人")
    assert mm.get_played_seed_ids(["狗與露"], limit=10) == ["aaaaaaaaaaa"]


# ── 3. 同首歌真人+Marvin 混點 → 算真人那份，保留 ──
def test_mixed_requester_song_kept_via_human(tmp_path):
    mm = _mm(tmp_path)
    info = {"title": "歌A", "webpage_url": "https://www.youtube.com/watch?v=aaaaaaaaaaa"}
    mm.record_play(info, "狗與露")
    mm.record_play(info, "Marvin推薦（為狗與露）")
    assert "aaaaaaaaaaa" in mm.get_played_seed_ids(["狗與露"], limit=10)


# ── 4. limit 截斷，高次數優先 ──
def test_limit_keeps_top_weighted(tmp_path):
    mm = _mm(tmp_path)
    for i in range(5):
        _play(mm, f"vid{i:08d}", f"歌{i}", "狗與露", times=5 - i)  # 遞減次數
    seeds = mm.get_played_seed_ids(["狗與露"], limit=3)
    assert len(seeds) == 3
    assert seeds[0] == "vid00000000"     # 次數最高


# ── 5. 沒在場成員點播史 → 空 ──
def test_empty_when_no_member_history(tmp_path):
    mm = _mm(tmp_path)
    _play(mm, "aaaaaaaaaaa", "歌A", "別人")
    assert mm.get_played_seed_ids(["狗與露"], limit=10) == []
