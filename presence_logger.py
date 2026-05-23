"""
presence_logger.py — Forward-looking voice channel presence logger.

寫 voice channel join/leave/move events 到 JSONL，作為 P7「presence as vote」
的 ground truth 基準。

Phase 1 啟動前部署，採集 7+ 天 baseline，Phase 2 evaluation 時對照。

對應 design doc:
  /Users/jackhuang/.gstack/projects/Discord-voice-bot/jackhuang-main-design-20260523-131453.md
  Success Criteria → Phase 0 Baseline 量測 → 在線時長 / 回流頻率

JSONL Schema:
  {
    "ts": float (unix),
    "iso_ts": str (UTC ISO 8601),
    "guild_id": str,
    "user_id": str,
    "user_name": str (display_name),
    "channel_id": str,
    "channel_name": str,
    "event": "join" | "leave" | "move",
    "is_bot": bool
  }

Usage (in main_discord.py listener):
  from presence_logger import log_voice_state_change
  ...
  async def _on_voice_state_update(...):
      log_voice_state_change(member, before, after)
      # ...其他邏輯

Analytics（之後寫對應 script）：
  - per-user 每日總在線分鐘 = sum(leave_ts - prior_join_ts)
  - 回流頻率 = 7 天內出現 join event 的天數
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_LOG_PATH = Path("data/voice_presence.jsonl")
_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)


def log_voice_state_change(member, before, after) -> None:
    """
    處理 discord.py 的 on_voice_state_update。寫 JSONL 一行（如果有 channel 變化）。

    Mute / deafen / self-mute 等 state 變化不寫（before.channel == after.channel）。
    只寫真正的 channel transition：join / leave / move。

    永不 raise——log writing 失敗不該影響 bot 主流程。
    """
    try:
        if before.channel == after.channel:
            return  # mute / deaf 等、不是 presence 變化

        if before.channel is None and after.channel is not None:
            event = "join"
            channel = after.channel
        elif before.channel is not None and after.channel is None:
            event = "leave"
            channel = before.channel
        else:
            event = "move"
            channel = after.channel

        record = {
            "ts": time.time(),
            "iso_ts": datetime.now(timezone.utc).isoformat(),
            "guild_id": str(member.guild.id),
            "user_id": str(member.id),
            "user_name": getattr(member, "display_name", str(member)),
            "channel_id": str(channel.id),
            "channel_name": getattr(channel, "name", ""),
            "event": event,
            "is_bot": bool(getattr(member, "bot", False)),
        }
        with _LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        logger.exception("[presence_logger] log write failed")
