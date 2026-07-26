#!/usr/bin/env python3
"""scripts/run_gmail_calendar_sync.py — 本機排程跑的 headless Claude Code，查
Gmail/Calendar 兩個數字寫進跨進程橋接檔（gmail_calendar_state.py）。

為什麼是這支腳本、不是雲端排程 agent：/schedule 建的雲端 agent 跑在 Anthropic
雲端沙盒，碰不到這台 Mac 的檔案系統——寫了也是寫進拋棄式沙盒，HUD 永遠讀不到。
這支改用本機 `claude -p`（headless），MCP Gmail/Calendar connector 沿用這台機器
已登入的 claude.ai 帳號，親測 headless 模式下可用（2026-07-26 手動驗證：
`echo prompt | claude -p --allowedTools "mcp__claude_ai_Gmail__search_threads"`
真的能拿到 resultCountEstimate，不用另外互動授權，只要用 --allowedTools 過權限
提示）。

只查數字，不讀信件內容——count-only 是刻意的設計（見
[[project_hud_actionable_open_loops]] 的 DAKboard 參考）。
"""
from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLAUDE_BIN = os.environ.get("MARVIN_CLAUDE_BIN", "claude")

PROMPT_TEMPLATE = """1. 呼叫 Gmail MCP 工具 search_threads，query 是 "is:unread newer_than:7d -in:draft"，
   view 用 THREAD_VIEW_METADATA_ONLY，pageSize 用 50。不要用 resultCountEstimate（不準）。
   分頁拿完所有 threads（一頁一頁帶 nextPageToken 直到沒有），把每封信的 sender 欄位
   依下列規則分類，累計每類數量：

   分類規則（每封信只落在一類，優先序由上往下）：
   - 銀行通知：sender domain 含 ctbcbank / megabank / taipeifubon / feib / hncb /
     cathaylife / cathaybk / entrust / fubon / sinopac / esun
   - 發票郵件：sender domain 含 uber / yoxi / foodpanda / 711 / family / shopee /
     momo / pchome / books.com / dks / nitori / buyee（購物/叫車收據類）
   - 工作郵件：sender domain 含 linkedin / github
   - 重要通知：sender domain 含 apple / microsoft / fedex / google / binance /
     cloudflare / playstation / osaka-marathon / coinbase
   - 關注的信件：其餘所有信件（電子報、個人信、投資報告等）

   計算出 5 個類別的各自數量（若某類為 0 可省略），格式：
   {{"關注的信件": N1, "重要通知": N2, "工作郵件": N3, "發票郵件": N4, "銀行通知": N5}}
   總數 N = 所有類別加總。不要讀信件內容，只看 sender。
2. 呼叫 Google Calendar MCP 工具 list_events，calendarId 用 "primary"，
   startTime="{today}T00:00:00+08:00"，endTime="{today}T23:59:59+08:00"，
   timeZone="Asia/Taipei"。算回傳的事件數量（沒有 items 欄位就是 0）。不要描述
   事件內容。
3. 用 Bash 執行（工作目錄是 {repo_dir}）：
   python3 scripts/sync_gmail_calendar_state.py --unread <N> --calendar-today <M> \
     --categories '<CATEGORIES_JSON>'
   N=總數、M=步驟 2 的事件數、CATEGORIES_JSON=步驟 1 算出的 JSON（單引號包住）。
只做這三步，不要輸出其他文字、不要問確認。
"""


def main() -> int:
    today = dt.date.today().isoformat()
    prompt = PROMPT_TEMPLATE.format(today=today, repo_dir=REPO_DIR)
    try:
        result = subprocess.run(
            [CLAUDE_BIN, "-p", "--allowedTools",
             "mcp__claude_ai_Gmail__search_threads",
             "mcp__claude_ai_Google_Calendar__list_events",
             "Bash(python3 scripts/sync_gmail_calendar_state.py*)"],
            input=prompt, text=True, cwd=REPO_DIR, timeout=180,
            capture_output=True, check=True,
        )
    except subprocess.CalledProcessError as e:
        print(f"claude -p 失敗: {e}\nstdout={e.stdout}\nstderr={e.stderr}", file=sys.stderr)
        return 1
    except subprocess.TimeoutExpired:
        print("claude -p 逾時（180s）", file=sys.stderr)
        return 1
    print(result.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
