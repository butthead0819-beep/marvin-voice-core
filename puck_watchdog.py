"""
puck_watchdog.py — ESP32 車 puck「完全沒聲音沒反應」主動偵測（server 端）。

跟 scripts/pipeline_heartbeat_probe.py 同精神（該有輸出而沒輸出→主動抓，不等使用者
回報），但 in-process、不做外部 cron + 去重狀態機：puck 只在跟車主 iPhone 配對、真的
在車上時才會開機（見 car_presence.is_present），不是 24/7 無人值守場景——偵測到直接
DM 本人一次就夠，人本來就在附近。

兩種「沒反應」訊號，各自獨立判斷（check_puck_stall 是純函式，時間全部注入，好測）：
  1. poll stall：puck 連 /car_commands 心跳輪詢都停了——比②更嚴重，代表斷線/當機/
     沒電，只在 car_presence 判斷駕駛還在車上時才有意義去警報（否則就是正常熄火）。
  2. deck stall：Mac 已經下了 play/queue_next，但超過門檻秒數 puck 都沒真的打
     /puck_deck 來拉音源——輪詢正常、指令也送達，只是沒被消費。2026-08-13 那次
     firmware seq 倒退 bug 就是這個症狀，當時只能人工看 log 才發現。
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

# 車 puck 上車瞬間 puck 還沒連上是正常的，給緩衝別誤報；puck 正常輪詢節奏是 1s 一次，
# 20s＝連 20 輪都沒打，遠超正常網路抖動可以解釋的範圍。
DEFAULT_POLL_STALL_THRESHOLD_S = 20.0
# /puck_deck 是現場 yt-dlp resolve + ffmpeg 轉碼才開始吐音，給一點餘裕（正常應該幾秒
# 內就打得到），15s 還沒打＝真的卡住，不是單純網路慢半拍。
DEFAULT_DECK_STALL_THRESHOLD_S = 15.0


@dataclass(frozen=True)
class PuckStallStatus:
    stalled: bool
    reason: str | None   # "poll" | "deck" | None


def check_puck_stall(
    *,
    is_present: bool,
    last_polled_ts: float,
    stall_seconds: float | None,
    now: float | None = None,
    poll_stall_threshold_s: float = DEFAULT_POLL_STALL_THRESHOLD_S,
    deck_stall_threshold_s: float = DEFAULT_DECK_STALL_THRESHOLD_S,
) -> PuckStallStatus:
    """純函式：車主不在車上就不用判斷（沒人在意熄火後 puck 沒反應）。

    last_polled_ts＝0（這趟上車後從沒輪詢過）視同 poll stall——跟「輪詢過但太久沒
    再輪詢」用同一個門檻判，caller 只該在 is_present 已經持續一段時間後才呼叫這個
    check（不是 present() 那一瞬間就檢查，避免 puck 還在連線路上就被誤報）。
    """
    if not is_present:
        return PuckStallStatus(False, None)
    now = now if now is not None else time.time()
    if last_polled_ts <= 0 or (now - last_polled_ts) > poll_stall_threshold_s:
        return PuckStallStatus(True, "poll")
    if stall_seconds is not None and stall_seconds > deck_stall_threshold_s:
        return PuckStallStatus(True, "deck")
    return PuckStallStatus(False, None)


STALL_REASON_TEXT = {
    "poll": "puck 完全沒回應（連 /car_commands 輪詢都停了，可能斷線/當機/沒電）",
    "deck": "puck 有在輪詢但下的播放指令沒被消費（可能卡住了，沒真的出聲音）",
}


# ── Discord REST DM（跟 pipeline_heartbeat_probe.py::dm_owner 同款寫法——main_satellite.py
# 是 standalone 進程、沒登入 Discord gateway，只能靠 REST API 直打，跟線上 24/7 bot 的
# gateway session 不衝突）。同步函式，呼叫端自己包 asyncio.to_thread 避免卡住 event loop。
def _read_bot_token() -> str:
    tok = os.environ.get("DISCORD_BOT_TOKEN", "")
    if tok:
        return tok
    env_path = Path(__file__).resolve().parent / ".env"
    for line in env_path.read_text().splitlines():
        if line.startswith("DISCORD_BOT_TOKEN="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("DISCORD_BOT_TOKEN not found")


def dm_owner_sync(text: str) -> None:
    token = _read_bot_token()
    owner_id = os.environ.get("MARVIN_OWNER_ID", "876758076831723580")

    def _api(path: str, payload: dict) -> dict:
        req = urllib.request.Request(
            f"https://discord.com/api/v10{path}",
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bot {token}", "Content-Type": "application/json",
                     "User-Agent": "MarvinPuckWatchdog/1.0"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())

    ch = _api("/users/@me/channels", {"recipient_id": str(owner_id)})
    _api(f"/channels/{ch['id']}/messages", {"content": text[:1990]})
