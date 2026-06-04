"""skip-override：手動點回的歌（played_again）覆蓋舊 skip（2026-06-04）。

get_skipped_titles 改 latest-wins——較新的 played_again/liked 蓋過舊 skipped，讓
skip 過後手動點回的歌不再被 auto-recommend 永久排除。
"""
from __future__ import annotations

from music_memory import MusicMemory


def _mm(tmp_path, data):
    mm = MusicMemory(path=str(tmp_path / "mm.json"))
    mm._data = data
    return mm


def test_played_again_after_skip_overrides(tmp_path):
    mm = _mm(tmp_path, {"recommendations": {"alice": {"feedback": [
        {"title": "晴天", "result": "skipped", "ts": 100},
        {"title": "晴天", "result": "played_again", "ts": 200},   # 較新 → 蓋過 skip
    ]}}})
    assert mm.get_skipped_titles(["alice"]) == []   # 不再算 skipped


def test_skip_after_played_again_still_skipped(tmp_path):
    # 反向：最新是 skipped → 仍算 skipped
    mm = _mm(tmp_path, {"recommendations": {"alice": {"feedback": [
        {"title": "晴天", "result": "played_again", "ts": 100},
        {"title": "晴天", "result": "skipped", "ts": 200},
    ]}}})
    assert mm.get_skipped_titles(["alice"]) == ["晴天"]


def test_liked_overrides_old_skip(tmp_path):
    mm = _mm(tmp_path, {"recommendations": {"alice": {"feedback": [
        {"title": "稻香", "result": "skipped", "ts": 100},
        {"title": "稻香", "result": "liked", "ts": 200},
    ]}}})
    assert mm.get_skipped_titles(["alice"]) == []


def test_plain_skip_still_excluded(tmp_path):
    mm = _mm(tmp_path, {"recommendations": {"alice": {"feedback": [
        {"title": "孤獨", "result": "skipped", "ts": 100},
    ]}}})
    assert mm.get_skipped_titles(["alice"]) == ["孤獨"]
