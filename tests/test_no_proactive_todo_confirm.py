"""主動待辦確認（「剛才說的『X』，要記成待辦嗎？」）已拔除。

2026-10-23：9/27 起開口 7 次、0 次答「好」；靜默檢查看的 _last_speech_time 只在接受喚醒時更新，
等於沒檢查 → 10/22 15:39 在狗與露打給廠商的電話中途開口，問號又開了追問窗，
把電話內容當成回答丟給主 LLM。使用者定：改成上班時提醒昨天沒做完的事，這條拔掉。
"""
from __future__ import annotations

import ast
import pathlib

from cogs.voice_controller import VoiceController

_SRC = pathlib.Path(__file__).resolve().parent.parent / "cogs" / "voice_controller.py"


def test_confirmation_machinery_removed():
    for name in ("_confirmation_checker_loop", "_on_commitment_detected", "_handle_confirmation_response"):
        assert not hasattr(VoiceController, name), f"{name} 應已拔除"


def test_session_summarizer_not_wired_to_commitment_callback():
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "SessionSummarizer"]
    assert calls, "找不到 SessionSummarizer(...) 建構呼叫"
    for c in calls:
        assert "on_commitment_detected" not in {k.arg for k in c.keywords}


def test_todo_confirm_prompt_text_gone():
    assert "要記成待辦嗎" not in _SRC.read_text(encoding="utf-8")
