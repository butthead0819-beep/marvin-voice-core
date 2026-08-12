"""
MarvinCommandsMixin — VoiceController 的「表演 / 觀察報告 / 系統診斷」slash 指令。

從 voice_controller.py 抽出（減肥），以 mixin 形式併入 VoiceController：
    class VoiceController(MarvinCommandsMixin, commands.Cog): ...
因此 self 仍是 VoiceController 實例，play_tts / play_dual_dialogue /
_tts_protected / manual_sing_request / get_online_members / bot.router 等
全部沿用原本的 self 存取，行為零改動。

留在 VoiceController 的：summon / dismiss（連線生命週期）、marvin_reboot /
marvin_tts_clear / marvin_optin / marvin_optout（控制面）。
"""
from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands

logger = logging.getLogger(__name__)


class MarvinCommandsMixin:
    @app_commands.command(name="marvin_say", description="[Voice] 讓馬文用他的聲音念出你打的字")
    @app_commands.describe(text="要馬文念出來的文字")
    async def marvin_say(self, interaction: discord.Interaction, text: str):
        # 刻意不走 SpeakBus：SpeakBus 是「主動發話」的仲裁（idle/mood 觸發 agent 競標
        # 該不該插嘴），這裡是使用者下的直接命令，沒有「要不要開口」可競標——走 bus
        # 反而可能被 MIN_CONFIDENCE / DuckingAgent 壓制而不發聲，違背指令本意。仍受
        # play_tts 的播放鎖鏈（playback_lock / tts_queue_lock / mixer）正確序列化。
        await interaction.response.defer(thinking=True)
        await interaction.followup.send(f"🗣️ 「{text}」")
        self.stt_logger.info(f"[MarvinSay←{interaction.user.display_name}] {text}")
        self._tts_interrupted = False
        _prev_protected = self._tts_protected
        self._tts_protected = True
        try:
            await self.play_tts(text, already_in_channel=True, protected=True, force_macos=True)
        finally:
            self._tts_protected = _prev_protected
