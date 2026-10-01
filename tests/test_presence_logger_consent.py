"""測試語音進出紀錄只記 Marvin 所在頻道與已同意者。

涵蓋：
- presence_event: 純函式行為
- log_voice_state_change: 寫檔行為與 consent 整合
"""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import presence_logger
from presence_logger import log_voice_state_change, presence_event


def _make_channel(ch_id: int, name: str):
    return SimpleNamespace(id=ch_id, name=name)


def test_presence_event_unconsented():
    ch_marvin = _make_channel(1, "voice-marvin")
    assert presence_event(
        is_bot=False,
        before_ch=None,
        after_ch=ch_marvin,
        marvin_ch=ch_marvin,
        consented=False,
    ) is None


def test_presence_event_consented_join_leave():
    ch_marvin = _make_channel(1, "voice-marvin")
    # join
    assert presence_event(
        is_bot=False,
        before_ch=None,
        after_ch=ch_marvin,
        marvin_ch=ch_marvin,
        consented=True,
    ) == ("join", ch_marvin)
    # leave
    assert presence_event(
        is_bot=False,
        before_ch=ch_marvin,
        after_ch=None,
        marvin_ch=ch_marvin,
        consented=True,
    ) == ("leave", ch_marvin)


def test_presence_event_between_non_marvin_channels():
    ch_marvin = _make_channel(1, "voice-marvin")
    ch_a = _make_channel(2, "voice-a")
    ch_b = _make_channel(3, "voice-b")
    assert presence_event(
        is_bot=False,
        before_ch=ch_a,
        after_ch=ch_b,
        marvin_ch=ch_marvin,
        consented=True,
    ) is None


def test_presence_event_move_into_and_out_of_marvin_channel():
    ch_marvin = _make_channel(1, "voice-marvin")
    ch_other = _make_channel(2, "voice-other")

    # 從其他頻道移進 Marvin 頻道 → ("join", marvin_ch)
    assert presence_event(
        is_bot=False,
        before_ch=ch_other,
        after_ch=ch_marvin,
        marvin_ch=ch_marvin,
        consented=True,
    ) == ("join", ch_marvin)

    # 從 Marvin 頻道移到其他頻道 → ("leave", marvin_ch)，channel 是 marvin_ch 不是另一個頻道
    assert presence_event(
        is_bot=False,
        before_ch=ch_marvin,
        after_ch=ch_other,
        marvin_ch=ch_marvin,
        consented=True,
    ) == ("leave", ch_marvin)


def test_presence_event_marvin_not_in_voice():
    ch_a = _make_channel(2, "voice-a")
    assert presence_event(
        is_bot=False,
        before_ch=None,
        after_ch=ch_a,
        marvin_ch=None,
        consented=True,
    ) is None


def test_presence_event_bot_ignored():
    ch_marvin = _make_channel(1, "voice-marvin")
    assert presence_event(
        is_bot=True,
        before_ch=None,
        after_ch=ch_marvin,
        marvin_ch=ch_marvin,
        consented=True,
    ) is None


def test_log_voice_state_change_file_integration(tmp_path, monkeypatch):
    log_file = tmp_path / "presence.jsonl"
    monkeypatch.setattr(presence_logger, "_LOG_PATH", log_file)

    ch_marvin = _make_channel(100, "general")
    guild = SimpleNamespace(id=500)

    member_unconsented = SimpleNamespace(id=201, guild=guild, display_name="Bob", bot=False)
    before_state = SimpleNamespace(channel=None)
    after_state = SimpleNamespace(channel=ch_marvin)

    # 未同意者事件不寫檔
    log_voice_state_change(
        member_unconsented,
        before_state,
        after_state,
        marvin_ch=ch_marvin,
        consented=False,
    )
    assert not log_file.exists() or log_file.read_text(encoding="utf-8") == ""

    # 已同意者 join 寫一行，channel_id 等於 marvin_ch.id
    member_consented = SimpleNamespace(id=202, guild=guild, display_name="Alice", bot=False)
    log_voice_state_change(
        member_consented,
        before_state,
        after_state,
        marvin_ch=ch_marvin,
        consented=True,
    )

    assert log_file.exists()
    lines = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 1
    assert lines[0]["event"] == "join"
    assert lines[0]["channel_id"] == "100"
    assert lines[0]["user_id"] == "202"
    assert lines[0]["is_bot"] is False
