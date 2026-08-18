#!/usr/bin/env python3
"""檢查 YouTube cookies.txt 新鮮度（見 incident_youtube_403_ip_throttle_2026-08-17
記憶：cogs/music_cog.py::_resolve_yt_query 靠這份 cookies 繞開 YouTube IP 節流）。

Netscape cookies.txt 每行第 5 欄是到期時間戳（unix epoch，0＝session cookie，
瀏覽器關掉才失效，這份檔案本身沒有「關閉」的概念，不算過期風險）。抓非
session cookie 裡最早到期的那個當警戒線——這只是個粗略代理指標，實際失效
也可能提早發生（帳號在別處登出、YouTube 判定異常等），不是精確保證，但
沒有更好的訊號來源前，這是最低成本的「該不該提醒使用者重新匯出」判斷依據。

用法：python scripts/check_yt_cookies_freshness.py
exit code：0＝新鮮（>7天）、1＝快過期（≤7天）、2＝已過期或找不到檔案。
"""
from __future__ import annotations

import datetime
import os
import sys

WARN_DAYS = 7
COOKIES_FILE = os.path.expanduser(
    os.getenv("MARVIN_YT_COOKIES_FILE", "~/.config/marvin/youtube_cookies.txt"))


def earliest_expiry(path: str) -> datetime.datetime | None:
    """回傳檔案裡最早到期的非 session cookie 的到期時間；沒有任何有到期時間
    的 cookie（全是 session cookie）就回傳 None。"""
    earliest = None
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.startswith("#") or not line.strip():
                continue
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 7:
                continue
            try:
                expiry_ts = int(parts[4])
            except ValueError:
                continue
            if expiry_ts <= 0:
                continue
            dt = datetime.datetime.fromtimestamp(expiry_ts)
            if earliest is None or dt < earliest:
                earliest = dt
    return earliest


def main() -> int:
    if not os.path.exists(COOKIES_FILE):
        print(f"❌ 找不到 cookies 檔案：{COOKIES_FILE}")
        print("   還沒匯出過，或路徑跟 MARVIN_YT_COOKIES_FILE 設定不一致。")
        return 2

    earliest = earliest_expiry(COOKIES_FILE)
    if earliest is None:
        print(f"⚠️ {COOKIES_FILE} 裡沒有任何非 session cookie（可能格式不對或空檔）")
        return 2

    now = datetime.datetime.now()
    days_left = (earliest - now).days

    if earliest <= now:
        print(f"❌ Cookies 已過期（最早到期的一個在 {earliest:%Y-%m-%d} 就過了），請重新匯出")
        return 2
    if days_left <= WARN_DAYS:
        print(f"⚠️ Cookies 快過期了：最早 {earliest:%Y-%m-%d}（剩 {days_left} 天），建議近期重新匯出")
        return 1

    print(f"✅ Cookies 新鮮：最早到期 {earliest:%Y-%m-%d}（還有 {days_left} 天）")
    print("   （這只是粗略指標——帳號異常/在別處登出可能提早失效，"
          "真的又見大量 403 時優先懷疑這個並重新匯出）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
