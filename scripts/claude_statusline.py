"""scripts/claude_statusline.py — Claude Code statusLine hook 指令。

Claude Code 每次刷新狀態列會把 JSON（cwd/model/rate_limits...）餵給 stdin，
要求 stdout 印一行文字當狀態列內容。這個腳本除了印基本文字，順便把
rate_limits（5hr/週用量，只有 Pro/Max 帳號、且要打過至少一次 API 才會出現這
欄位）寫進 claude_sessions_state.py 橋接檔，給 HUD /claude_status 讀。

裝法：~/.claude/settings.json 加
  "statusLine": {"type": "command", "command": "python3 <此檔絕對路徑>"}
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from claude_sessions_state import DEFAULT_PATH, save_claude_rate_limits  # noqa: E402


def render_and_record(payload: dict, state_path: str = DEFAULT_PATH) -> str:
    cwd = payload.get("cwd") or (payload.get("workspace") or {}).get("current_dir") or ""
    model_name = (payload.get("model") or {}).get("display_name") or ""

    rate_limits = payload.get("rate_limits")
    pct_suffix = ""
    if isinstance(rate_limits, dict):
        five_hour = rate_limits.get("five_hour") or {}
        seven_day = rate_limits.get("seven_day") or {}
        if five_hour or seven_day:
            save_claude_rate_limits(five_hour=five_hour, seven_day=seven_day, path=state_path)
        pct = five_hour.get("used_percentage")
        if pct is not None:
            pct_suffix = f" · 5h {pct:.0f}%"

    parts = [p for p in (os.path.basename(cwd.rstrip("/")) or cwd, model_name) if p]
    return " · ".join(parts) + pct_suffix


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        payload = {}
    print(render_and_record(payload))


if __name__ == "__main__":
    main()
