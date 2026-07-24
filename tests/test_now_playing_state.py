"""
tests/test_now_playing_state.py
TDD：now_playing_state.py — 跨進程橋接檔（真 Discord bot 寫、satellite /now 讀）。

比照 location_state.py 同一套模式：純檔案讀寫，無網路無 Discord。
"""
import json

from now_playing_state import load_now_playing_state, save_now_playing_state


def test_save_then_load_round_trips(tmp_path):
    path = str(tmp_path / "now_playing_state.json")
    save_now_playing_state(
        playing=True, title="夜曲", by="大肚", cover="http://x/y.jpg",
        palette=["#111111", "#222222"], queue=[{"title": "晴天", "by": "小明"}],
        duration=245.0, song_start_time=1700000000.0, comment="這首不錯。", path=path,
    )
    state = load_now_playing_state(path=path)
    assert state == {
        "playing": True, "title": "夜曲", "by": "大肚",
        "cover": "http://x/y.jpg", "palette": ["#111111", "#222222"],
        "queue": [{"title": "晴天", "by": "小明"}],
        "duration": 245.0, "song_start_time": 1700000000.0, "comment": "這首不錯。",
    }


def test_save_not_playing_defaults_empty_fields(tmp_path):
    path = str(tmp_path / "now_playing_state.json")
    save_now_playing_state(playing=False, path=path)
    state = load_now_playing_state(path=path)
    assert state["playing"] is False
    assert state["title"] == ""
    assert state["palette"] == []
    assert state["queue"] == []
    assert state["duration"] is None
    assert state["song_start_time"] is None
    assert state["comment"] is None


def test_load_missing_file_returns_none(tmp_path):
    path = str(tmp_path / "missing.json")
    assert load_now_playing_state(path=path) is None


def test_load_corrupt_json_returns_none(tmp_path):
    path = str(tmp_path / "now_playing_state.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write("{not json")
    assert load_now_playing_state(path=path) is None
