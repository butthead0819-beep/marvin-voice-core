"""
test_presence_logger.py — presence_logger 狀態機 + JSONL 寫入測試

測試範圍：
  - join / leave / move 三種 transition 都寫 JSONL
  - mute / deaf 等同 channel 變化不寫
  - 例外不外拋（exception swallowing）
  - JSONL schema 與 design doc 對齊
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import presence_logger as pl


@pytest.fixture
def temp_log(tmp_path, monkeypatch):
    log_path = tmp_path / "voice_presence.jsonl"
    monkeypatch.setattr(pl, "_LOG_PATH", log_path)
    yield log_path


def _mk_member(user_id="42", display_name="狗與露", guild_id="999", is_bot=False):
    m = MagicMock()
    m.id = user_id
    m.display_name = display_name
    m.guild = MagicMock()
    m.guild.id = guild_id
    m.bot = is_bot
    return m


def _mk_voice_state(channel_id=None, channel_name=None):
    s = MagicMock()
    if channel_id is None:
        s.channel = None
    else:
        s.channel = MagicMock()
        s.channel.id = channel_id
        s.channel.name = channel_name
    return s


def _read_records(log_path: Path) -> list[dict]:
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]


def test_join_writes_record(temp_log):
    member = _mk_member()
    before = _mk_voice_state(channel_id=None)
    after = _mk_voice_state(channel_id="ch1", channel_name="general")

    pl.log_voice_state_change(member, before, after)

    records = _read_records(temp_log)
    assert len(records) == 1
    r = records[0]
    assert r["event"] == "join"
    assert r["channel_id"] == "ch1"
    assert r["channel_name"] == "general"
    assert r["user_id"] == "42"
    assert r["user_name"] == "狗與露"
    assert r["guild_id"] == "999"
    assert r["is_bot"] is False
    assert isinstance(r["ts"], float)
    assert "iso_ts" in r and "T" in r["iso_ts"]


def test_leave_writes_record_with_before_channel(temp_log):
    member = _mk_member()
    before = _mk_voice_state(channel_id="ch1", channel_name="general")
    after = _mk_voice_state(channel_id=None)

    pl.log_voice_state_change(member, before, after)

    records = _read_records(temp_log)
    assert len(records) == 1
    assert records[0]["event"] == "leave"
    assert records[0]["channel_id"] == "ch1"
    assert records[0]["channel_name"] == "general"


def test_move_writes_record_with_after_channel(temp_log):
    member = _mk_member()
    before = _mk_voice_state(channel_id="ch1", channel_name="general")
    after = _mk_voice_state(channel_id="ch2", channel_name="music")

    pl.log_voice_state_change(member, before, after)

    records = _read_records(temp_log)
    assert len(records) == 1
    assert records[0]["event"] == "move"
    assert records[0]["channel_id"] == "ch2"
    assert records[0]["channel_name"] == "music"


def test_same_channel_does_not_write(temp_log):
    """Mute / deafen / self-mute 等 state 變化 — before.channel == after.channel — 不寫。"""
    ch = MagicMock()
    ch.id = "ch1"
    ch.name = "general"
    member = _mk_member()
    before = MagicMock()
    before.channel = ch
    after = MagicMock()
    after.channel = ch  # 同一個 channel 物件

    pl.log_voice_state_change(member, before, after)

    assert _read_records(temp_log) == []


def test_bot_member_is_logged_with_is_bot_true(temp_log):
    member = _mk_member(user_id="bot1", display_name="Marvin", is_bot=True)
    before = _mk_voice_state(channel_id=None)
    after = _mk_voice_state(channel_id="ch1", channel_name="general")

    pl.log_voice_state_change(member, before, after)

    records = _read_records(temp_log)
    assert len(records) == 1
    assert records[0]["is_bot"] is True


def test_exception_does_not_propagate(temp_log, monkeypatch):
    """log writing 失敗不該影響 caller（discord.py listener）。"""
    member = _mk_member()
    before = _mk_voice_state(channel_id=None)
    after = _mk_voice_state(channel_id="ch1", channel_name="general")

    # 強制讓 file open 拋 exception
    def _broken_open(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(Path, "open", _broken_open)

    # 不該拋
    pl.log_voice_state_change(member, before, after)


def test_multiple_events_append_to_same_file(temp_log):
    member1 = _mk_member(user_id="1", display_name="A")
    member2 = _mk_member(user_id="2", display_name="B")
    join_before = _mk_voice_state(channel_id=None)
    ch1_after = _mk_voice_state(channel_id="ch1", channel_name="general")

    pl.log_voice_state_change(member1, join_before, ch1_after)
    pl.log_voice_state_change(member2, join_before, ch1_after)

    records = _read_records(temp_log)
    assert len(records) == 2
    assert records[0]["user_id"] == "1"
    assert records[1]["user_id"] == "2"
