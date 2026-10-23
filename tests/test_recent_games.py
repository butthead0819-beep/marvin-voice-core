"""recent_games：記住 Discord「正在玩」抓到過的遊戲，當下抓不到時用最近 7 天在場的人玩過的。

2026-10-23：大家在 PS 上玩 Grounded 2，Discord 多半不顯示，10 月 95 次查詢只有 13 次帶到遊戲脈絡。
"""
from __future__ import annotations

import recent_games as rg

DAY = 86400.0


def test_recent_within_window_most_recent_first(tmp_path):
    p = tmp_path / "g.json"
    rg.note_games("showay", ["Grounded 2"], now=1000.0, path=p)
    rg.note_games("狗與露", ["原神"], now=2000.0, path=p)
    assert rg.recent_games(["showay", "狗與露"], now=3000.0, path=p) == ["原神", "Grounded 2"]


def test_older_than_window_excluded(tmp_path):
    p = tmp_path / "g.json"
    rg.note_games("showay", ["Grounded 2"], now=0.0, path=p)
    assert rg.recent_games(["showay"], now=7 * DAY + 1.0, path=p) == []
    assert rg.recent_games(["showay"], now=7 * DAY, path=p) == ["Grounded 2"]


def test_only_present_members(tmp_path):
    p = tmp_path / "g.json"
    rg.note_games("大肚", ["Minecraft"], now=1000.0, path=p)
    assert rg.recent_games(["showay"], now=2000.0, path=p) == []


def test_limit_three(tmp_path):
    p = tmp_path / "g.json"
    for i, g in enumerate(["A", "B", "C", "D"]):
        rg.note_games("showay", [g], now=1000.0 + i, path=p)
    assert rg.recent_games(["showay"], now=2000.0, path=p) == ["D", "C", "B"]


def test_corrupt_or_missing_file_is_empty(tmp_path):
    p = tmp_path / "g.json"
    assert rg.recent_games(["showay"], now=1.0, path=p) == []
    p.write_text("{not json", encoding="utf-8")
    assert rg.recent_games(["showay"], now=1.0, path=p) == []
    rg.note_games("showay", ["Grounded 2"], now=1.0, path=p)   # 壞檔被覆蓋、不拋
    assert rg.recent_games(["showay"], now=2.0, path=p) == ["Grounded 2"]
