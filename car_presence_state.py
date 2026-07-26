"""car_presence_state.py — 跨進程橋接：車載 puck 現在在不在場 → music_cog 讀取。

main_satellite.py 的 CarPresence 是純記憶體狀態機（car_presence.py 特意無 I/O、好測），
但「現正播放要不要寫進 HUD 橋接檔」這個決定要在 main_discord.py 那個獨立進程裡的
music_cog.py 做——兩邊互相看不到對方的記憶體，只能靠這種檔案橋接（比照
now_playing_state.py／claude_sessions_state.py 同一套模式）。

寫入者：main_satellite.py 背景迴圈，每次 car_presence.is_present 有值就定期寫一拍
（不只在 arrive/depart 那瞬間寫）——這樣就算 main_satellite.py 進程本身掛了，
`updated_at` 也會停止更新，讀的一方可以靠新鮮度自己判斷「這份資料還可信嗎」，
不會永遠卡在最後一次寫的 present=True。
"""
from __future__ import annotations

import json
import os

DEFAULT_PATH = os.path.join(os.path.dirname(__file__), "car_presence_state.json")

# 跟 car_presence.py 的 CarPresence 預設 ttl_s 一致：心跳斷了多久算「其實已經不在用」。
DEFAULT_STALE_AFTER_S = 90.0


def save_car_presence_state(*, present: bool, updated_at: float, path: str = DEFAULT_PATH) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"present": present, "updated_at": updated_at}, f)


def is_car_actively_in_use(*, now: float, path: str = DEFAULT_PATH,
                            stale_after_s: float = DEFAULT_STALE_AFTER_S) -> bool:
    """車載 puck 現在是不是真的在用（現正播放要不要照樣寫進 HUD 橋接檔的依據）。

    檔案不存在／壞掉／太久沒更新（main_satellite.py 沒在跑，或 puck 心跳已經停了一段
    時間）一律當作「沒在用」→ 呼叫端應該 fallback 寫回 Discord 的播放狀態給家用 HUD。
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return False
    if not state.get("present"):
        return False
    updated_at = state.get("updated_at")
    if updated_at is None or (now - updated_at) > stale_after_s:
        return False
    return True
