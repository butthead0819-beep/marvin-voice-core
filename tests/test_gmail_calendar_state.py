"""
tests/test_gmail_calendar_state.py
TDD：gmail_calendar_state.py — 跨進程橋接檔（排程 agent 寫、HUD /gmail_calendar_status 讀）。

比照 now_playing_state.py 同一套模式：純檔案讀寫，無網路。
"""
from gmail_calendar_state import load_gmail_calendar_state, save_gmail_calendar_state


def test_save_then_load_round_trips(tmp_path):
    path = str(tmp_path / "gmail_calendar_state.json")
    save_gmail_calendar_state(gmail_unread=12, calendar_today_count=2, updated_at=1700000000.0, path=path)
    state = load_gmail_calendar_state(path=path)
    assert state == {"gmail_unread": 12, "calendar_today_count": 2, "updated_at": 1700000000.0}


def test_load_missing_file_returns_none(tmp_path):
    path = str(tmp_path / "does_not_exist.json")
    assert load_gmail_calendar_state(path=path) is None
