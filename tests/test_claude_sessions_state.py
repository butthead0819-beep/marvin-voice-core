"""
tests/test_claude_sessions_state.py
TDD：claude_sessions_state.py 跨進程橋接（Claude Code session 狀態 → HUD 卡片讀取）。

比照 now_playing_state.py / location_state.py 同一套模式：一邊寫、一邊讀，
壞掉互不影響。schema 見 claude_sessions_state.py docstring。
"""
from claude_sessions_state import (
    load_claude_sessions_state,
    save_claude_sessions_state,
    save_claude_rate_limits,
)


def test_load_returns_none_when_file_missing(tmp_path):
    path = str(tmp_path / "claude_sessions_state.json")
    assert load_claude_sessions_state(path=path) is None


def test_save_then_load_roundtrips_sessions(tmp_path):
    path = str(tmp_path / "claude_sessions_state.json")
    sessions = [
        {"session_id": "abc", "project": "Discord-voice-bot", "cwd": "/x",
         "waiting": True, "last_text": "要不要重跑 CI？", "updated_at": 123.0},
    ]
    save_claude_sessions_state(sessions=sessions, path=path)
    state = load_claude_sessions_state(path=path)
    assert state["sessions"] == sessions


def test_save_rate_limits_merges_without_clobbering_sessions(tmp_path):
    """statusline 腳本跟 scanner 腳本各自獨立寫入，不能互相蓋掉對方的欄位。"""
    path = str(tmp_path / "claude_sessions_state.json")
    save_claude_sessions_state(sessions=[{"session_id": "abc"}], path=path)
    save_claude_rate_limits(
        five_hour={"used_percentage": 42, "resets_at": 1234567890},
        seven_day={"used_percentage": 10, "resets_at": 1234599999},
        path=path,
    )
    state = load_claude_sessions_state(path=path)
    assert state["sessions"] == [{"session_id": "abc"}]
    assert state["rate_limits"]["five_hour"]["used_percentage"] == 42
    assert state["rate_limits"]["seven_day"]["used_percentage"] == 10


def test_save_sessions_merges_without_clobbering_rate_limits(tmp_path):
    path = str(tmp_path / "claude_sessions_state.json")
    save_claude_rate_limits(
        five_hour={"used_percentage": 42, "resets_at": 1}, seven_day={}, path=path)
    save_claude_sessions_state(sessions=[{"session_id": "new"}], path=path)
    state = load_claude_sessions_state(path=path)
    assert state["rate_limits"]["five_hour"]["used_percentage"] == 42
    assert state["sessions"] == [{"session_id": "new"}]
