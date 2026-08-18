"""一次性腳本：把 stt_history.log 裡所有「喚醒詞後的文字」DM 給 owner。

來源：`[✅Query通過] [speaker] gate_ok | query='...'` 這行是 debounce 後、進 intent
routing 前的原始 query 文字（stt_history.log 現存範圍 2026-07-16 ~ 今日）。使用者要
自己人工過一遍抓開放意圖，所以這裡故意不做任何篩選/分類，整批原文丟出去。

用法：python scripts/dm_wake_queries.py
獨立 discord.Client，login → 找到 owner (LOCAL_USER_ID) → 分批 DM（每則 <=1900 字，
按行邊界切）→ close。跟主進程用同一 bot token 開新 gateway 連線，傳完就斷，不影響
主進程運作。
"""
from __future__ import annotations

import asyncio
import os
import re
import sys

import discord
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
OWNER_ID_STR = os.getenv("LOCAL_USER_ID", "0")
LOG_PATH = "stt_history.log"
CHUNK_LIMIT = 1900

_LINE_RE = re.compile(r"^(\S+ \S+) - \[✅Query通過\] \[(.+?)\] gate_ok \| query='(.*)'$")


def extract_wake_queries(path: str) -> list[str]:
    lines = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = _LINE_RE.match(line.rstrip("\n"))
            if m:
                ts, speaker, query = m.groups()
                lines.append(f"{ts} [{speaker}] {query}")
    return lines


def chunk_lines(lines: list[str], limit: int) -> list[str]:
    chunks, buf = [], ""
    for line in lines:
        candidate = f"{buf}\n{line}" if buf else line
        if len(candidate) > limit:
            if buf:
                chunks.append(buf)
            buf = line
        else:
            buf = candidate
    if buf:
        chunks.append(buf)
    return chunks


async def main() -> None:
    if not TOKEN:
        print("❌ 找不到 DISCORD_BOT_TOKEN", file=sys.stderr)
        sys.exit(1)
    try:
        owner_id = int(OWNER_ID_STR)
    except ValueError:
        owner_id = 0
    if not owner_id:
        print("❌ LOCAL_USER_ID 未設", file=sys.stderr)
        sys.exit(1)

    lines = extract_wake_queries(LOG_PATH)
    if not lines:
        print("⚠️ stt_history.log 沒抓到任何 [✅Query通過] 記錄")
        return
    chunks = chunk_lines(lines, CHUNK_LIMIT)
    print(f"共 {len(lines)} 筆喚醒後文字，切成 {len(chunks)} 則訊息，準備 DM owner={owner_id}")

    intents = discord.Intents.default()
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        try:
            owner = client.get_user(owner_id) or await client.fetch_user(owner_id)
            if owner is None:
                print(f"❌ 找不到 user_id={owner_id}", file=sys.stderr)
            else:
                await owner.send(
                    f"📋 喚醒詞後的原始文字，共 {len(lines)} 筆"
                    f"（來源 stt_history.log，2026-07-16 起現存範圍）："
                )
                for i, chunk in enumerate(chunks, 1):
                    await owner.send(f"```\n{chunk}\n```")
                    print(f"  已送出 {i}/{len(chunks)}")
                print("✅ 全部送出")
        finally:
            await client.close()

    await client.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
