#!/usr/bin/env python3
"""scripts/run_gmail_calendar_sync.py — 本機排程跑的純 Python 查 Gmail/Calendar。

改掉原本用重型 Agent (claude -p) 造成的龐大 Token 浪費，採用 Option A1 架構：
- 使用 Google API SDK (google-api-python-client) 查 Gmail 與 Calendar (0 Token)。
- 在 Python 中進行 5 大類別域名規則分類 (0 Token)。
- 搜尋範圍：當天的未讀郵件 (is:unread newer_than:1d -in:draft)。
- 用量極省：僅針對當天全新未讀信件 (最多 3 封) 呼叫 llm_pool router 產生簡短繁中摘要 (Single-turn, < 500 tokens)。
- 寫進跨進程橋接檔 (gmail_calendar_state.py)。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import sys
import time
from typing import Any

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_DIR)

from dotenv import load_dotenv  # noqa: E402
load_dotenv()

from google.auth.transport.requests import Request  # noqa: E402
from google.oauth2.credentials import Credentials  # noqa: E402
from googleapiclient.discovery import build  # noqa: E402

from gmail_calendar_state import load_gmail_calendar_state, save_gmail_calendar_state  # noqa: E402
from llm_pool import build_tiered_router  # noqa: E402

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
]
TOKEN_PATH = os.path.join(REPO_DIR, "google_tokens.json")

BANK_DOMAINS = ["ctbcbank", "megabank", "taipeifubon", "feib", "hncb", "cathaylife", "cathaybk", "entrust", "fubon", "sinopac", "esun"]
INVOICE_DOMAINS = ["uber", "yoxi", "foodpanda", "711", "family", "shopee", "momo", "pchome", "books.com", "dks", "nitori", "buyee"]
WORK_DOMAINS = ["linkedin", "github"]
IMPORTANT_DOMAINS = ["apple", "microsoft", "fedex", "google", "binance", "cloudflare", "playstation", "osaka-marathon", "coinbase"]


def classify_sender(sender: str) -> str:
    s = sender.lower()
    for d in BANK_DOMAINS:
        if d in s:
            return "銀行通知"
    for d in INVOICE_DOMAINS:
        if d in s:
            return "發票郵件"
    for d in WORK_DOMAINS:
        if d in s:
            return "工作郵件"
    for d in IMPORTANT_DOMAINS:
        if d in s:
            return "重要通知"
    return "關注的信件"


def load_google_credentials() -> Credentials | None:
    if not os.path.exists(TOKEN_PATH):
        return None
    try:
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(TOKEN_PATH, "w", encoding="utf-8") as f:
                f.write(creds.to_json())
        return creds if creds and creds.valid else None
    except Exception as e:
        print(f"[gmail_calendar_sync] 載入 Credentials 失敗: {e}", file=sys.stderr)
        return None


def fetch_gmail_unread_today(creds: Credentials) -> tuple[int, dict[str, int], list[dict[str, Any]]]:
    """使用 Gmail API 查詢當天的未讀信件 (is:unread in:inbox newer_than:1d -in:draft)。"""
    service = build("gmail", "v1", credentials=creds)
    query = "is:unread in:inbox newer_than:1d -in:draft"

    
    threads_res = service.users().threads().list(userId="me", q=query, maxResults=50).execute()
    threads = threads_res.get("threads", [])
    
    categories = {
        "關注的信件": 0,
        "重要通知": 0,
        "工作郵件": 0,
        "發票郵件": 0,
        "銀行通知": 0,
    }
    
    raw_unread_items = []
    
    for t in threads:
        t_id = t["id"]
        t_data = service.users().threads().get(userId="me", id=t_id, format="full").execute()
        messages = t_data.get("messages", [])
        if not messages:
            continue
            
        first_msg = messages[0]
        headers = {h["name"].lower(): h["value"] for h in first_msg.get("payload", {}).get("headers", [])}
        
        subject = headers.get("subject", "(無標題)")
        sender = headers.get("from", "(未知寄件者)")
        date_str = headers.get("date", "")
        snippet = first_msg.get("snippet", "")
        
        cat = classify_sender(sender)
        categories[cat] = categories.get(cat, 0) + 1
        
        raw_unread_items.append({
            "id": t_id,
            "subject": subject,
            "sender": sender,
            "date": date_str,
            "snippet": snippet,
            "category": cat,
        })
        
    categories = {k: v for k, v in categories.items() if v > 0}
    return len(threads), categories, raw_unread_items


def fetch_calendar_today_count(creds: Credentials) -> int:
    """使用 Google Calendar API 查詢當天事件數。"""
    service = build("calendar", "v3", credentials=creds)
    today = dt.date.today().isoformat()
    start_time = f"{today}T00:00:00+08:00"
    end_time = f"{today}T23:59:59+08:00"
    
    events_res = service.events().list(
        calendarId="primary",
        timeMin=start_time,
        timeMax=end_time,
        singleEvents=True,
        timeZone="Asia/Taipei",
    ).execute()
    
    items = events_res.get("items", [])
    return len(items)


async def summarize_important_emails(
    raw_items: list[dict[str, Any]],
    cached_emails: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """針對新信件呼叫 llm_pool 進行輕量摘要 (Single-turn LLM)。"""
    cached_map = {item["id"]: item for item in cached_emails if "id" in item}
    
    result_emails = []
    items_to_summarize = []
    
    for item in raw_items[:10]:
        t_id = item["id"]
        if t_id in cached_map:
            result_emails.append(cached_map[t_id])
        else:
            items_to_summarize.append(item)
            
    if not items_to_summarize:
        return result_emails[:3]
        
    targets = items_to_summarize[:3]
    prompt_input = json.dumps([{
        "id": x["id"],
        "subject": x["subject"],
        "sender": x["sender"],
        "date": x["date"],
        "snippet": x["snippet"],
    } for x in targets], ensure_ascii=False)
    
    prompt = f"""你是一個精準的繁體中文郵件助理。請分析以下未讀信件：
{prompt_input}

請回傳一個 JSON 陣列（格式如下），整合最多 3 封重點信件的摘要：
[
  {{
    "id": "信件ID",
    "subject": "主旨",
    "sender": "寄件者",
    "date": "日期/時間",
    "summary": "1-2 句話精簡繁體中文摘要",
    "action_item": "建議動作（如：需回覆/繳費；無則寫 '無須動作，僅通知'）",
    "priority": "high/medium/low"
  }}
]
只回傳 JSON 陣列，不要加入任何額外說明。"""

    try:
        router = build_tiered_router()
        res_str = await router.quick(prompt, caller="gmail_calendar_sync", json=True)
        if res_str:
            new_summaries = json.loads(res_str)
            if isinstance(new_summaries, list):
                result_emails.extend(new_summaries)
    except Exception as e:
        print(f"[gmail_calendar_sync] LLM 摘要失敗，降級使用原文: {e}", file=sys.stderr)
        for x in targets:
            result_emails.append({
                "id": x["id"],
                "subject": x["subject"],
                "sender": x["sender"],
                "date": x["date"],
                "summary": x["snippet"][:50] + ("..." if len(x["snippet"]) > 50 else ""),
                "action_item": "無須動作，僅通知",
                "priority": "medium",
            })
            
    return result_emails[:3]


async def async_main() -> int:
    creds = load_google_credentials()
    if not creds:
        print(
            "❌ [gmail_calendar_sync] 未能載入 Google Credentials！\n"
            "請先執行: python3 scripts/google_auth_setup.py 進行一次性 Google 帳號授權。",
            file=sys.stderr,
        )
        return 1
        
    try:
        unread_count, categories, raw_items = fetch_gmail_unread_today(creds)
    except Exception as e:
        print(f"❌ [gmail_calendar_sync] 讀取 Gmail 失敗: {e}", file=sys.stderr)
        return 1
        
    try:
        calendar_count = fetch_calendar_today_count(creds)
    except Exception as e:
        print(f"⚠️ [gmail_calendar_sync] 讀取 Calendar 失敗: {e}", file=sys.stderr)
        calendar_count = 0

    existing_state = load_gmail_calendar_state() or {}
    cached_emails = existing_state.get("important_emails", [])

    important_emails = await summarize_important_emails(raw_items, cached_emails)

    save_gmail_calendar_state(
        gmail_unread=unread_count,
        calendar_today_count=calendar_count,
        gmail_categories=categories,
        important_emails=important_emails,
        updated_at=time.time(),
    )

    print(
        f"✅ [gmail_calendar_sync] 成功寫入狀態：unread={unread_count} (當天) "
        f"categories={categories} important={len(important_emails)} calendar={calendar_count}"
    )
    return 0


def main() -> int:
    return asyncio.run(async_main())


if __name__ == "__main__":
    raise SystemExit(main())
