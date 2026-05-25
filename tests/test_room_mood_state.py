"""RoomMoodState — Week 1 基建測試。

跨 agent 共用的房間狀態 store（記憶體 + 5min JSON dump）。

設計合約見 docs/social_catalyst_plan.md。

涵蓋：
  1. 預設值（無資料時 group_mood = 放鬆 / temperature = 0.0 / hot_chat = False）
  2. 個體 mood 寫入 / 讀取
  3. 群體 mood + temperature 寫入
  4. hot_chat 配對寫入
  5. dump / load roundtrip
  6. 過期 channel 不會炸（隔離性）
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from room_mood_state import RoomMoodStateStore


@pytest.fixture
def store(tmp_path) -> RoomMoodStateStore:
    return RoomMoodStateStore(dump_path=str(tmp_path / "room_mood_state.json"))


def test_unknown_channel_returns_default(store):
    state = store.get(channel_id=999)
    assert state.channel_id == 999
    assert state.group_mood == "放鬆"
    assert state.group_temperature == 0.0
    assert state.hot_chat is False
    assert state.hot_chat_pair is None
    assert state.individual_mood == {}


def test_set_individual_mood(store):
    store.set_individual_mood(channel_id=100, speaker="alice", mood="低落")
    state = store.get(100)
    assert state.individual_mood["alice"] == "低落"


def test_set_group_mood_and_temperature(store):
    store.set_group(channel_id=100, mood="興奮", temperature=0.75)
    state = store.get(100)
    assert state.group_mood == "興奮"
    assert state.group_temperature == 0.75


def test_set_hot_chat_pair(store):
    store.set_hot_chat(channel_id=100, hot=True, pair=("alice", "bob"))
    state = store.get(100)
    assert state.hot_chat is True
    assert state.hot_chat_pair == ("alice", "bob")

    store.set_hot_chat(channel_id=100, hot=False)
    state = store.get(100)
    assert state.hot_chat is False
    assert state.hot_chat_pair is None


def test_channels_are_isolated(store):
    store.set_group(100, mood="興奮", temperature=0.8)
    store.set_group(200, mood="低落", temperature=0.2)
    assert store.get(100).group_mood == "興奮"
    assert store.get(200).group_mood == "低落"


def test_dump_and_reload(tmp_path):
    p = str(tmp_path / "state.json")
    s1 = RoomMoodStateStore(dump_path=p)
    s1.set_individual_mood(100, "alice", "低落")
    s1.set_group(100, mood="興奮", temperature=0.6)
    s1.set_hot_chat(100, hot=True, pair=("alice", "bob"))
    s1.dump()

    s2 = RoomMoodStateStore(dump_path=p)
    s2.load()
    state = s2.get(100)
    assert state.individual_mood["alice"] == "低落"
    assert state.group_mood == "興奮"
    assert state.group_temperature == 0.6
    assert state.hot_chat is True
    assert state.hot_chat_pair == ("alice", "bob")


def test_load_missing_file_is_noop(tmp_path):
    """檔案不存在時 load() 不該炸。"""
    p = str(tmp_path / "does_not_exist.json")
    s = RoomMoodStateStore(dump_path=p)
    s.load()  # should not raise
    state = s.get(100)
    assert state.group_mood == "放鬆"  # default


def test_load_corrupted_file_is_noop(tmp_path):
    """壞掉的 JSON 不該炸——保 fallback 路徑。"""
    p = tmp_path / "corrupt.json"
    p.write_text("not valid json {{{")
    s = RoomMoodStateStore(dump_path=str(p))
    s.load()
    state = s.get(100)
    assert state.group_mood == "放鬆"


def test_updated_at_advances_on_writes(store):
    t_before = time.time()
    store.set_group(100, mood="興奮", temperature=0.5)
    state = store.get(100)
    assert state.updated_at >= t_before
