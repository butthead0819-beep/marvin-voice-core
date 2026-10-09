"""TDD — scripts/dj_mode_skip_rate.compute_skip_rates：哪種 DJ mode 講完比較會被跳。

純函式層驗證，不碰檔案 IO（main() 的讀檔/列印不測）。
"""
from __future__ import annotations

from scripts.dj_mode_skip_rate import compute_skip_rates


def _play(play_id, mode, ts=0.0):
    return {"type": "play", "play_id": play_id, "mode": mode, "ts": ts}


def _attach(play_id, mode):
    return {"type": "narration_attach", "play_id": play_id, "mode": mode}


def _skip(play_id, elapsed_s):
    return {"play_id": play_id, "elapsed_s": elapsed_s}


def test_baseline_group_for_none_mode():
    plays = [_play("p1", None)]
    result = compute_skip_rates(plays, [])
    assert result == [{"group": "baseline", "plays": 1, "skips": 0, "rate": 0.0, "decidable": False}]


def test_mode_group_from_play_row():
    plays = [_play("p1", "life")]
    result = compute_skip_rates(plays, [])
    assert result[0]["group"] == "life"
    assert result[0]["plays"] == 1


def test_attach_overrides_mode():
    plays = [_play("p1", None), _attach("p1", "short")]
    result = compute_skip_rates(plays, [])
    assert result == [{"group": "short", "plays": 1, "skips": 0, "rate": 0.0, "decidable": False}]


def test_attach_takes_last_when_multiple():
    plays = [_play("p1", None), _attach("p1", "life"), _attach("p1", "short")]
    result = compute_skip_rates(plays, [])
    assert result[0]["group"] == "short"


def test_skip_within_window_counts():
    plays = [_play("p1", "life")]
    skips = [_skip("p1", 10.0)]
    result = compute_skip_rates(plays, skips, window_s=30.0)
    assert result[0]["skips"] == 1
    assert result[0]["rate"] == 1.0


def test_skip_beyond_window_not_counted():
    plays = [_play("p1", "life")]
    skips = [_skip("p1", 45.0)]
    result = compute_skip_rates(plays, skips, window_s=30.0)
    assert result[0]["skips"] == 0


def test_skip_exactly_at_window_boundary_counted():
    plays = [_play("p1", "life")]
    skips = [_skip("p1", 30.0)]
    result = compute_skip_rates(plays, skips, window_s=30.0)
    assert result[0]["skips"] == 1


def test_skip_with_none_elapsed_not_counted():
    plays = [_play("p1", "life")]
    skips = [_skip("p1", None)]
    result = compute_skip_rates(plays, skips)
    assert result[0]["skips"] == 0


def test_duplicate_skip_same_play_id_counted_once():
    plays = [_play("p1", "life")]
    skips = [_skip("p1", 5.0), _skip("p1", 10.0)]
    result = compute_skip_rates(plays, skips)
    assert result[0]["skips"] == 1


def test_skip_with_unknown_play_id_ignored():
    plays = [_play("p1", "life")]
    skips = [_skip("p_unknown", 5.0)]
    result = compute_skip_rates(plays, skips)
    assert result[0]["skips"] == 0


def test_decidable_depends_on_min_n():
    plays = [_play(f"p{i}", "life") for i in range(5)]
    result = compute_skip_rates(plays, [], min_n=20)
    assert result[0]["decidable"] is False
    result2 = compute_skip_rates(plays, [], min_n=5)
    assert result2[0]["decidable"] is True


def test_groups_sorted_by_name():
    plays = [_play("p1", "quick"), _play("p2", "life"), _play("p3", None)]
    result = compute_skip_rates(plays, [])
    assert [r["group"] for r in result] == ["baseline", "life", "quick"]


def test_zero_plays_group_not_output():
    plays = [_play("p1", "life")]
    skips = [_skip("p_unknown", 5.0)]
    result = compute_skip_rates(plays, skips)
    assert all(r["plays"] > 0 for r in result)
