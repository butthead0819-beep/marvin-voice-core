"""TDD: location_state.py — 車載 ESP32 puck 最新 GPS 訊號存取。"""
from __future__ import annotations

from location_state import load_location_state, save_location_state


def test_load_missing_file_returns_none(tmp_path):
    path = tmp_path / "location_state.json"
    assert load_location_state(path=str(path)) is None


def test_save_then_load_roundtrip(tmp_path):
    path = tmp_path / "location_state.json"
    save_location_state(lat=25.06, lon=121.58, ts=1000.0, path=str(path))
    state = load_location_state(path=str(path))
    assert state == {"lat": 25.06, "lon": 121.58, "ts": 1000.0}


def test_saving_again_overwrites_previous_entry(tmp_path):
    path = tmp_path / "location_state.json"
    save_location_state(lat=25.06, lon=121.58, ts=1000.0, path=str(path))
    save_location_state(lat=25.09, lon=121.52, ts=2000.0, path=str(path))
    state = load_location_state(path=str(path))
    assert state == {"lat": 25.09, "lon": 121.52, "ts": 2000.0}


def test_corrupted_file_treated_as_empty(tmp_path):
    path = tmp_path / "location_state.json"
    path.write_text("not json", encoding="utf-8")
    assert load_location_state(path=str(path)) is None
    save_location_state(lat=1.0, lon=2.0, ts=3.0, path=str(path))
    assert load_location_state(path=str(path)) == {"lat": 1.0, "lon": 2.0, "ts": 3.0}
