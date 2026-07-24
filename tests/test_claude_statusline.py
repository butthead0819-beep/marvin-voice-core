"""
tests/test_claude_statusline.py
TDD：scripts/claude_statusline.py — Claude Code statusLine hook。

順便把 stdin JSON 帶的 rate_limits（5hr/週用量）寫進 claude_sessions_state.py
橋接檔，給 HUD 用；同時要吐一行基本文字當狀態列本體（因為改成自訂 statusLine
會取代內建預設顯示，不能只寫檔不顯示東西）。
"""
from claude_sessions_state import load_claude_sessions_state
from scripts.claude_statusline import render_and_record


def test_writes_rate_limits_to_state_file_when_present(tmp_path):
    path = str(tmp_path / "claude_sessions_state.json")
    payload = {
        "cwd": "/Users/jackhuang/Code/Discord-voice-bot",
        "model": {"id": "claude-sonnet-5", "display_name": "Sonnet"},
        "rate_limits": {
            "five_hour": {"used_percentage": 23.5, "resets_at": 1738425600},
            "seven_day": {"used_percentage": 41.2, "resets_at": 1738857600},
        },
    }
    render_and_record(payload, state_path=path)
    state = load_claude_sessions_state(path=path)
    assert state["rate_limits"]["five_hour"]["used_percentage"] == 23.5
    assert state["rate_limits"]["seven_day"]["used_percentage"] == 41.2


def test_text_line_includes_cwd_basename_model_and_percentage(tmp_path):
    path = str(tmp_path / "claude_sessions_state.json")
    payload = {
        "cwd": "/Users/jackhuang/Code/Discord-voice-bot",
        "model": {"id": "claude-sonnet-5", "display_name": "Sonnet"},
        "rate_limits": {
            "five_hour": {"used_percentage": 23.5, "resets_at": 1738425600},
            "seven_day": {"used_percentage": 41.2, "resets_at": 1738857600},
        },
    }
    text = render_and_record(payload, state_path=path)
    assert "Discord-voice-bot" in text
    assert "Sonnet" in text
    assert "%" in text


def test_missing_rate_limits_does_not_write_or_crash(tmp_path):
    path = str(tmp_path / "claude_sessions_state.json")
    payload = {"cwd": "/x/y", "model": {"id": "m", "display_name": "M"}}
    text = render_and_record(payload, state_path=path)
    assert load_claude_sessions_state(path=path) is None
    assert "y" in text


def test_missing_model_field_does_not_crash(tmp_path):
    path = str(tmp_path / "claude_sessions_state.json")
    payload = {"cwd": "/x/y"}
    text = render_and_record(payload, state_path=path)
    assert isinstance(text, str) and text
