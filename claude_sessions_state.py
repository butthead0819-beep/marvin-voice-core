"""claude_sessions_state.py — 跨進程橋接：Claude Code session 狀態 → HUD /claude_status 讀取。

三個獨立寫入者、各自互不影響（比照 now_playing_state.py 同一套模式）：
- scripts/scan_claude_sessions.py（定期背景任務）掃 ~/.claude/sessions + 各專案
  transcript，寫 `sessions` 欄位（有沒有等待你回應、最後一則 assistant 訊息）。
- scripts/claude_statusline.py（Claude Code 的 statusLine hook，每次刷新觸發）寫
  `rate_limits` 欄位（5hr / weekly 用量）。
- main_satellite.py 的 /claude_hook（Claude Code 的 Notification hook，permission_prompt
  跳出來那瞬間觸發）寫 `notification` 欄位——比 scan loop 的 20s 輪詢快，用來讓 HUD
  在下一次背景掃描補上之前就能立刻反應；不是唯一真相來源，是加速訊號。
兩邊寫入前都先讀現有檔案再合併，不會互相蓋掉對方剛寫的欄位。
"""
from __future__ import annotations

import json
import os

DEFAULT_PATH = os.path.join(os.path.dirname(__file__), "claude_sessions_state.json")


def load_claude_sessions_state(path: str = DEFAULT_PATH) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _read_existing(path: str) -> dict:
    return load_claude_sessions_state(path=path) or {}


def save_claude_sessions_state(*, sessions: list, path: str = DEFAULT_PATH) -> None:
    state = _read_existing(path)
    state["sessions"] = sessions
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f)


def save_claude_rate_limits(*, five_hour: dict, seven_day: dict,
                             path: str = DEFAULT_PATH) -> None:
    state = _read_existing(path)
    state["rate_limits"] = {"five_hour": five_hour, "seven_day": seven_day}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f)


def save_claude_notification(*, session_id: str, cwd: str, message: str,
                              notification_type: str, received_at: float,
                              path: str = DEFAULT_PATH) -> None:
    state = _read_existing(path)
    state["notification"] = {
        "session_id": session_id,
        "cwd": cwd,
        "message": message,
        "notification_type": notification_type,
        "received_at": received_at,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f)
