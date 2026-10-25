"""TDD：喊「馬文閉嘴」緊急停止——喊停語判斷 + 狀態持久化（emergency_stop.py）。"""
from __future__ import annotations

import json

import pytest

from emergency_stop import EmergencyStop, is_estop_phrase


@pytest.mark.parametrize("text", [
    "馬文閉嘴",
    "馬文安靜",
    "馬文安靜一點",
    "馬文你閉嘴啦",
    "馬文給我安靜",
    "馬文緊急停止",
    "嗯馬文停下來",
    "馬文不要再講了",
    "馬文閉嘴馬文閉嘴",
    "瑪文，閉嘴！",
])
def test_is_estop_phrase_true(text):
    assert is_estop_phrase(text) is True


@pytest.mark.parametrize("text", [
    "馬文停止播放",
    "你用馬文跟他聊天",
    "馬文安靜的時候比較可愛",
    "叫馬文閉嘴",
    "麻煩安靜一點",
    "閉嘴",
    "馬文下一首",
    "",
])
def test_is_estop_phrase_false(text):
    assert is_estop_phrase(text) is False


def test_engage_persists_across_instances(tmp_path):
    path = str(tmp_path / "estop.json")
    st = EmergencyStop(path)
    assert st.engage("狗與露", 100.0) is True

    st2 = EmergencyStop(path)
    assert st2.active is True
    assert st2.by == "狗與露"


def test_engage_twice_second_call_returns_false(tmp_path):
    path = str(tmp_path / "estop.json")
    st = EmergencyStop(path)
    assert st.engage("A", 1.0) is True
    assert st.engage("B", 2.0) is False
    assert st.by == "A"
    assert st.since == 1.0


def test_release_persists_and_noop_when_not_active(tmp_path):
    path = str(tmp_path / "estop.json")
    st = EmergencyStop(path)
    assert st.release("A", 1.0) is False  # 未停止時解除 → False

    st.engage("A", 1.0)
    assert st.release("B", 2.0) is True

    st2 = EmergencyStop(path)
    assert st2.active is False


def test_corrupt_state_file_does_not_raise(tmp_path):
    path = tmp_path / "estop.json"
    path.write_text("{not valid json", encoding="utf-8")
    st = EmergencyStop(str(path))
    assert st.active is False


def test_missing_parent_directory_does_not_raise_on_engage(tmp_path):
    path = str(tmp_path / "no" / "x.json")
    st = EmergencyStop(path)
    assert st.engage("A", 1.0) is True
    assert st.active is True
