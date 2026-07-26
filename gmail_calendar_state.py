"""gmail_calendar_state.py — 跨進程橋接：排程 agent 查 Gmail/Calendar → HUD 讀取。

main_satellite.py 是 24/7 背景進程，碰不到 MCP Gmail/Calendar connector（那些只在
互動 session 活著）。真正的資料來源是排程跑的 Claude Code agent（見
scripts/sync_gmail_calendar_state.py），定期查完寫這個檔案，HUD 純讀檔——比照
now_playing_state.py／location_state.py 同一套模式。

[[project_hud_actionable_open_loops]] 已經定案：這只是 count-only 的環境感知卡
（DAKboard 那種「顯示未讀數，不顯示內容」），不是逐封信分類/追蹤——那個是下一步
「明講關注」機制要做的事，這邊先不做。
"""
from __future__ import annotations

import json
import os

DEFAULT_PATH = os.path.join(os.path.dirname(__file__), "gmail_calendar_state.json")

# 排程多久沒跑，資料就當作太舊不可信（HUD 那邊用這個門檻決定要不要顯示這張卡）。
DEFAULT_STALE_AFTER_S = 3600.0 * 3


def load_gmail_calendar_state(path: str = DEFAULT_PATH) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                data.setdefault("important_emails", [])
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_gmail_calendar_state(*, gmail_unread: int, calendar_today_count: int,
                               gmail_categories: dict | None = None,
                               important_emails: list[dict] | None = None,
                               updated_at: float, path: str = DEFAULT_PATH) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "gmail_unread": gmail_unread,
            "gmail_categories": gmail_categories or {},
            "important_emails": important_emails or [],
            "calendar_today_count": calendar_today_count,
            "updated_at": updated_at,
        }, f)

