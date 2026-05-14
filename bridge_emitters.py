"""
bridge_emitters.py — CompanionBridge 的薄 emit 包裝層。

從 main_discord.py 抽出，讓 cogs/voice_controller.py 可以直接 import
而不產生循環依賴（main_discord.py 載入 cogs，cogs 不應再 import main_discord）。
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


def emit_stt_to_bridge(bot, speaker: str, text: str, engine: str) -> None:
    """STT 完成後呼叫；同步 wrapper，內部排 task。
    所在執行緒：可能是 sink 同步回呼或 pipeline async；故用 loop.create_task。
    """
    bridge = getattr(bot, "companion_bridge", None)
    if bridge is None or not getattr(bridge, "is_running", False):
        return
    try:
        loop = getattr(bot, "loop", None)
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
        loop.create_task(bridge.emit_stt_chunk(speaker, text, engine))
    except Exception as e:
        logger.debug(f"[Companion_Bridge] emit_stt skipped: {e}")


async def emit_tts_started_to_bridge(bot, text: str, voice: str, target=None) -> None:
    """TTS 開始播放時呼叫。失敗不擾亂 TTS 流程。"""
    bridge = getattr(bot, "companion_bridge", None)
    if bridge is None or not getattr(bridge, "is_running", False):
        return
    try:
        await bridge.emit_tts_started(text, voice, target)
    except Exception as e:
        logger.debug(f"[Companion_Bridge] emit_tts_started skipped: {e}")


async def emit_tts_done_to_bridge(bot) -> None:
    """TTS 結束/中斷/錯誤都應在 finally 呼叫。"""
    bridge = getattr(bot, "companion_bridge", None)
    if bridge is None or not getattr(bridge, "is_running", False):
        return
    try:
        await bridge.emit_tts_done()
    except Exception as e:
        logger.debug(f"[Companion_Bridge] emit_tts_done skipped: {e}")


async def emit_music_started_to_bridge(bot, song_info: dict, requested_by: str) -> None:
    """音樂播放前呼叫；失敗不擾亂播放流程。"""
    bridge = getattr(bot, "companion_bridge", None)
    if bridge is None or not getattr(bridge, "is_running", False):
        return
    try:
        await bridge.emit_music_started(song_info, requested_by)
    except Exception as e:
        logger.debug(f"[Companion_Bridge] emit_music_started skipped: {e}")


async def emit_music_ended_to_bridge(bot, song_info: dict, completion: str) -> None:
    """音樂結束/中斷/跳過時呼叫。"""
    bridge = getattr(bot, "companion_bridge", None)
    if bridge is None or not getattr(bridge, "is_running", False):
        return
    try:
        await bridge.emit_music_ended(song_info, completion)
    except Exception as e:
        logger.debug(f"[Companion_Bridge] emit_music_ended skipped: {e}")


async def emit_music_reaction_to_bridge(bot, username: str, song_info: dict, reaction: str) -> None:
    """玩家對音樂的反應廣播；失敗不擾亂主流程。"""
    bridge = getattr(bot, "companion_bridge", None)
    if bridge is None or not getattr(bridge, "is_running", False):
        return
    try:
        await bridge.emit_music_reaction(username, song_info, reaction)
    except Exception as e:
        logger.debug(f"[Companion_Bridge] emit_music_reaction skipped: {e}")


async def emit_member_joined_to_bridge(bot, speaker: str, payload_extras: dict | None) -> None:
    """玩家加入 Marvin 所在語音頻道時呼叫；失敗不擾亂主流程。"""
    bridge = getattr(bot, "companion_bridge", None)
    if bridge is None or not getattr(bridge, "is_running", False):
        return
    try:
        await bridge.emit_member_joined(speaker, payload_extras)
    except Exception as e:
        logger.debug(f"[Companion_Bridge] emit_member_joined skipped: {e}")


async def emit_member_left_to_bridge(bot, speaker: str) -> None:
    """玩家離開 Marvin 所在語音頻道時呼叫；失敗不擾亂主流程。"""
    bridge = getattr(bot, "companion_bridge", None)
    if bridge is None or not getattr(bridge, "is_running", False):
        return
    try:
        await bridge.emit_member_left(speaker)
    except Exception as e:
        logger.debug(f"[Companion_Bridge] emit_member_left skipped: {e}")
