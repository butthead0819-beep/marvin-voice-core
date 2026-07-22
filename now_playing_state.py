"""now_playing_state.py — 跨進程橋接：main_discord.py 真實播放狀態 → satellite /now 讀取。

main_satellite.py 開的 bot 不登入 Discord（避免同 token 衝突），自己的 MusicCog
永遠是空的；真正在 Discord 播歌的是 main_discord.py 另一個進程。兩邊靠這個檔案橋
接現正播放資料，比照 location_state.py 同一套模式：一邊寫、一邊讀，各自獨立、
壞掉互不影響。
"""
from __future__ import annotations

import json
import os

DEFAULT_PATH = os.path.join(os.path.dirname(__file__), "now_playing_state.json")


def load_now_playing_state(path: str = DEFAULT_PATH) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_now_playing_state(*, playing: bool, title: str = "", by: str = "",
                            cover: str = "", palette: list | None = None,
                            queue: list | None = None,
                            path: str = DEFAULT_PATH) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "playing": playing,
            "title": title,
            "by": by,
            "cover": cover,
            "palette": palette or [],
            "queue": queue or [],
        }, f)
