"""TDD: GPS 訊號 → DJ prompt 用的區級地點描述（純函式，無 I/O）。

唯一訊號源＝隨身的 ESP32 車載 puck，15 分鐘門檻；過期或沒訊號 → 回預設城市（台中）。
"""
from __future__ import annotations

from gps_context import city_label, nearest_district


def test_nearest_district_picks_closest_centroid():
    # 內湖區中心點附近
    assert nearest_district(25.0693, 121.5885) == "內湖區"


def test_no_state_returns_default():
    assert city_label(None, now=1000.0) == "台中"


def test_fresh_signal_returns_its_district():
    state = {"lat": 25.0693, "lon": 121.5885, "ts": 1000.0}
    assert city_label(state, now=1000.0 + 60) == "內湖區"


def test_stale_signal_falls_back_to_default():
    state = {"lat": 25.0693, "lon": 121.5885, "ts": 0.0}
    assert city_label(state, now=16 * 60) == "台中"  # 超過 15 分鐘門檻


def test_signal_exactly_at_threshold_still_fresh():
    state = {"lat": 25.0693, "lon": 121.5885, "ts": 0.0}
    assert city_label(state, now=15 * 60) == "內湖區"
