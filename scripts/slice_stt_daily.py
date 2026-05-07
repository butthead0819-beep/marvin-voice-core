#!/usr/bin/env python3
"""
每天 08:00 執行：從 stt_history.log 切出「昨日中午 12:00 ~ 今日中午 12:00」的區塊
輸出到 records/daily/stt_YYYY-MM-DD.log（以起始日期命名）
"""
import os
import re
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_PATH  = os.path.join(BASE_DIR, "stt_history.log")
OUT_DIR   = os.path.join(BASE_DIR, "records", "daily")

# 2026-04-26 01:05:19,715 - ...
RE_MAIN = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+")
# --- ... | Tue Mar 31 00:15:41 2026 ---
RE_SEP  = re.compile(r"(\w{3} \w{3} +\d{1,2} \d{2}:\d{2}:\d{2} \d{4})")


def parse_ts(line: str) -> datetime | None:
    m = RE_MAIN.match(line)
    if m:
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
    m = RE_SEP.search(line)
    if m:
        try:
            return datetime.strptime(m.group(1), "%a %b %d %H:%M:%S %Y")
        except ValueError:
            return None
    return None


def main():
    now = datetime.now()
    end   = now.replace(hour=12, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=1)

    date_str    = start.strftime("%Y-%m-%d")
    output_file = os.path.join(OUT_DIR, f"stt_{date_str}.log")

    os.makedirs(OUT_DIR, exist_ok=True)

    matched = []
    with open(LOG_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            ts = parse_ts(line)
            if ts is not None and start <= ts < end:
                matched.append(line)

    with open(output_file, "w", encoding="utf-8") as f:
        f.write("\n".join(matched))
        if matched:
            f.write("\n")

    print(f"[slice_stt_daily] {start} ~ {end}")
    print(f"[slice_stt_daily] {len(matched)} lines -> {output_file}")


if __name__ == "__main__":
    main()
