#!/usr/bin/env python3
"""scripts/sync_gmail_calendar_state.py — 排程 agent 專用的寫檔小工具。

排程的 Claude Code agent 本身就能呼叫 MCP Gmail/Calendar connector 拿到真實數字
（main_satellite.py 這個 24/7 進程碰不到那些 MCP 工具），算完兩個數字後呼叫這支
script 寫進跨進程橋接檔，HUD 純讀檔（見 gmail_calendar_state.py 開頭說明）。

用法：python3 scripts/sync_gmail_calendar_state.py --unread 12 --calendar-today 2
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gmail_calendar_state import save_gmail_calendar_state  # noqa: E402


def main() -> None:
    import json as _json
    parser = argparse.ArgumentParser()
    parser.add_argument("--unread", type=int, required=True)
    parser.add_argument("--calendar-today", type=int, required=True)
    parser.add_argument("--categories", type=str, default=None,
                        help='JSON，如 \'{"關注的信件":3,"重要通知":5,"工作郵件":2,"發票郵件":1,"銀行通知":8}\'')
    parser.add_argument("--important-emails", type=str, default=None,
                        help='JSON 陣列，含 [{"id":..., "subject":..., "sender":..., "summary":..., "action_item":..., "priority":...}]')
    args = parser.parse_args()
    cats = _json.loads(args.categories) if args.categories else None
    important_emails = _json.loads(args.important_emails) if args.important_emails else None
    save_gmail_calendar_state(
        gmail_unread=args.unread,
        gmail_categories=cats,
        important_emails=important_emails,
        calendar_today_count=args.calendar_today,
        updated_at=time.time(),
    )
    print(f"寫入完成：gmail_unread={args.unread} categories={cats} important_emails={len(important_emails) if important_emails else 0} 封 calendar_today_count={args.calendar_today}")



if __name__ == "__main__":
    main()
