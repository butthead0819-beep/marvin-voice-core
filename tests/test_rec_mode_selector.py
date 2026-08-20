"""TDD — 推薦引擎情境切換選擇器：music_recommender.select_rec_mode()。

仿 dj_topic_selector.select_mode 的優先序 cascade pattern：本地決定 rediscover
（挖舊）vs discover_new（新領域）vs default（fallback 現有排序），不用每次問 LLM。
只是排序優先度層，不動 long_tail/adjacent_artists 既有分數公式。
"""
from __future__ import annotations

from music_recommender import RecModeStore, select_rec_mode


def _store(tmp_path, last_mode=None):
    store = RecModeStore(path=str(tmp_path / "rec_mode_state.json"))
    if last_mode is not None:
        store.set_last_mode(last_mode)
    return store


class TestSelectRecMode:
    def test_only_rediscover_available_picks_rediscover(self, tmp_path):
        store = _store(tmp_path)
        mode = select_rec_mode(has_rediscover=True, has_discover_new=False, store=store)
        assert mode == "rediscover"

    def test_only_discover_new_available_picks_discover_new(self, tmp_path):
        store = _store(tmp_path)
        mode = select_rec_mode(has_rediscover=False, has_discover_new=True, store=store)
        assert mode == "discover_new"

    def test_both_empty_falls_back_to_default(self, tmp_path):
        store = _store(tmp_path)
        mode = select_rec_mode(has_rediscover=False, has_discover_new=False, store=store)
        assert mode == "default"

    def test_both_available_rotates_away_from_last_mode(self, tmp_path):
        store = _store(tmp_path, last_mode="rediscover")
        mode = select_rec_mode(has_rediscover=True, has_discover_new=True, store=store)
        assert mode == "discover_new"

    def test_both_available_no_last_mode_defaults_to_priority_order(self, tmp_path):
        store = _store(tmp_path)
        mode = select_rec_mode(has_rediscover=True, has_discover_new=True, store=store)
        assert mode == "rediscover"

    def test_rotation_persists_across_store_instances(self, tmp_path):
        path = str(tmp_path / "rec_mode_state.json")
        store1 = RecModeStore(path=path)
        first = select_rec_mode(has_rediscover=True, has_discover_new=True, store=store1)

        store2 = RecModeStore(path=path)
        second = select_rec_mode(has_rediscover=True, has_discover_new=True, store=store2)

        assert first != second

    def test_missing_state_file_fails_open_to_priority_order(self, tmp_path):
        store = RecModeStore(path=str(tmp_path / "nonexistent" / "rec_mode_state.json"))
        mode = select_rec_mode(has_rediscover=True, has_discover_new=True, store=store)
        assert mode == "rediscover"
