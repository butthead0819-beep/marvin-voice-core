"""
presence_logger.py — Forward-looking voice channel presence logger.

寫 voice channel join/leave events 到 JSONL，作為 P7「presence as vote」
的 ground truth 基準。

Phase 1 隱私規範（STATUS 🔴6）：
  - 只記錄相對於 Marvin 所在頻道的進出（join / leave）
  - 只記錄已同意者（consent.is_consented == True）
  - 不再記錄 bot（bot → None）
  - 不再產生 "move"（非 Marvin 頻道之間移動一律忽略）
  - Marvin 不在語音頻道時不記錄

JSONL Schema:
  {
    "ts": float (unix),
    "iso_ts": str (UTC ISO 8601),
    "guild_id": str,
    "user_id": str,
    "user_name": str (display_name),
    "channel_id": str,
    "channel_name": str,
    "event": "join" | "leave",
    "is_bot": bool (永遠為 False)
  }

Usage (in main_discord.py listener):
  from presence_logger import log_voice_state_change
  ...
  async def _on_voice_state_update_for_temp(_member, before, after) -> None:
      _vc = _member.guild.voice_client
      _marvin_ch = _vc.channel if _vc else None
      _vcog = self.get_cog("VoiceController")
      _consented = bool(_vcog and _vcog.consent.is_consented(_member.display_name))
      log_voice_state_change(_member, before, after, marvin_ch=_marvin_ch, consented=_consented)
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


def presence_event(*, is_bot: bool, before_ch, after_ch, marvin_ch, consented: bool):
    """回 (event, channel) 或 None（不記）。只看相對於 Marvin 所在頻道的進出：
    進入 marvin_ch → ("join", marvin_ch)；離開 marvin_ch → ("leave", marvin_ch)。
    其他頻道之間移動、Marvin 不在語音、bot、未同意者 → None。不再產生 "move"。"""
    if is_bot:
        return None
    if marvin_ch is None:
        return None
    if not consented:
        return None
    if before_ch == after_ch:
        return None
    if after_ch == marvin_ch:
        return ("join", marvin_ch)
    if before_ch == marvin_ch:
        return ("leave", marvin_ch)
    return None


def log_voice_state_change(member, before, after, *, marvin_ch, consented: bool) -> None:
    """處理 discord.py 的 on_voice_state_update。寫 JSONL 一行（若為 Marvin 所在頻道且已同意者）。

    只記相對於 Marvin 所在頻道的進出（join / leave），不再產生 move。
    bot、未同意者、Marvin 不在頻道、非 Marvin 頻道間移動皆不記錄。

    永不 raise——log writing 失敗不該影響 bot 主流程。
    """
    try:
        before_ch = before.channel if before else None
        after_ch = after.channel if after else None
        is_bot = bool(getattr(member, "bot", False))

        ev_info = presence_event(
            is_bot=is_bot,
            before_ch=before_ch,
            after_ch=after_ch,
            marvin_ch=marvin_ch,
            consented=consented,
        )
        if ev_info is None:
            return

        event, channel = ev_info

        record = {
            "ts": time.time(),
            "iso_ts": datetime.now(timezone.utc).isoformat(),
            "guild_id": str(member.guild.id),
            "user_id": str(member.id),
            "user_name": getattr(member, "display_name", str(member)),
            "channel_id": str(channel.id),
            "channel_name": getattr(channel, "name", ""),
            "event": event,
            "is_bot": False,
        }
        with _LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        logger.exception("[presence_logger] log write failed")
