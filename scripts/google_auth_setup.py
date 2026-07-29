#!/usr/bin/env python3
"""scripts/google_auth_setup.py — Google API OAuth 2.0 第一次性授權與 Token 產生腳本。

用於產生/更新 google_tokens.json，供 scripts/run_gmail_calendar_sync.py
純 Python 直連存取 Gmail API 與 Google Calendar API 使用。
"""
from __future__ import annotations

import argparse
import os
import sys

from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
]

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CREDS_PATH = os.path.join(REPO_DIR, "google_credentials.json")
DEFAULT_TOKEN_PATH = os.path.join(REPO_DIR, "google_tokens.json")


def get_valid_credentials(
    creds_path: str = DEFAULT_CREDS_PATH,
    token_path: str = DEFAULT_TOKEN_PATH,
    interactive: bool = True,
) -> Credentials | None:
    """獲取可用的 Google OAuth Credentials（若憑證已過期自動 refresh）。"""
    creds = None
    if os.path.exists(token_path):
        try:
            creds = Credentials.from_authorized_user_file(token_path, SCOPES)
        except Exception as e:
            print(f"[google_auth] 讀取 token 失敗: {e}", file=sys.stderr)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            with open(token_path, "w", encoding="utf-8") as f:
                f.write(creds.to_json())
            print("[google_auth] 成功刷回 Token！")
            return creds
        except Exception as e:
            print(f"[google_auth] Refresh Token 失敗: {e}", file=sys.stderr)

    if not interactive:
        return None

    if not os.path.exists(creds_path):
        print(f"[google_auth] 找不到 API 憑證檔：{creds_path}", file=sys.stderr)
        print("請在 Google Cloud Console 下載 OAuth 2.0 Client ID 的 credentials.json 並存放置 repo 根目錄。", file=sys.stderr)
        return None

    flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
    creds = flow.run_local_server(port=0)

    with open(token_path, "w", encoding="utf-8") as f:
        f.write(creds.to_json())
    print(f"[google_auth] 授權成功！已儲存 Token 至 {token_path}")
    return creds


def main() -> int:
    parser = argparse.ArgumentParser(description="Google API OAuth 授權與 Token 管理")
    parser.add_argument("--credentials", default=DEFAULT_CREDS_PATH, help="Google OAuth Credentials JSON 路徑")
    parser.add_argument("--token", default=DEFAULT_TOKEN_PATH, help="儲存/讀取 Authorized Token JSON 路徑")
    args = parser.parse_args()

    creds = get_valid_credentials(args.credentials, args.token, interactive=True)
    if creds and creds.valid:
        print("✅ Google API 憑證有效。")
        return 0
    else:
        print("❌ 憑證獲取失敗。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
