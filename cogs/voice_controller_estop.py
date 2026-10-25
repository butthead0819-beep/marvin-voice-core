"""🛑 緊急停止接線（核心在 emergency_stop.py）。

喊停語在 handle_stt_result 最前面（consent 之後、所有喚醒防護之前）攔截 → 穿透 Echo Guard 等。
只有 /marvin_estop 解除。狀態放 mixin 懶建立（不進 VoiceController.__init__，防胖棘輪）。
"""
from __future__ import annotations

import asyncio
import logging
import time

import discord
from discord import app_commands

from emergency_stop import EmergencyStop, is_estop_phrase

logger = logging.getLogger(__name__)


class EmergencyStopMixin:
    def _estop_state(self) -> EmergencyStop:
        st = getattr(self, "_estop", None)
        if st is None:
            st = self._estop = EmergencyStop()
        return st

    def _estop_active(self) -> bool:
        return self._estop_state().active

    def _estop_intercept(self, speaker: str, raw_text: str) -> bool:
        """喊停語 → 立刻靜音（同步）＋背景停歌貼文，回 True＝這句吞掉不往下走。其他句子回 False。"""
        if not is_estop_phrase(raw_text):
            return False
        if self._estop_state().engage(speaker, time.time()):
            self._estop_apply(True)
            logger.warning(f"🛑 [EStop] {speaker} 喊停：'{raw_text}' → 全靜音＋停歌，/marvin_estop 解除")
            asyncio.create_task(self._estop_after_engage(speaker))
        return True

    def _estop_apply(self, on: bool) -> None:
        mixer = getattr(self, "_mixer", None)
        if mixer is not None:
            mixer.set_estop(on)

    async def _estop_after_engage(self, by: str) -> None:
        mc = self.bot.cogs.get("MusicCog")
        if mc is not None:
            try:
                await mc.stop_stream("緊急停止")
                await mc.stop_radio("緊急停止")
            except Exception:
                logger.exception("🛑 [EStop] 停歌失敗")
        ch = getattr(self, "active_text_channel", None)
        if ch is not None:
            try:
                await ch.send(f"🛑 **緊急停止**（{by}）：Marvin 不出聲、音樂已停。輸入 `/marvin_estop` 選「解除」才會恢復。")
            except Exception:
                pass

    @app_commands.command(name="marvin_estop", description="[Emergency] 緊急停止 Marvin（全靜音＋停歌）／解除")
    @app_commands.describe(action="停止或解除")
    @app_commands.choices(action=[
        app_commands.Choice(name="stop — 緊急停止", value="stop"),
        app_commands.Choice(name="release — 解除緊急停止", value="release"),
    ])
    async def marvin_estop(self, interaction: discord.Interaction, action: app_commands.Choice[str]):
        await self._estop_command(interaction.user.display_name, action.value, interaction.response.send_message)

    async def _estop_command(self, by: str, action: str, reply) -> None:
        """斜線指令本體（抽出來好測）。reply＝interaction.response.send_message。"""
        st = self._estop_state()
        if action == "release":
            if st.release(by, time.time()):
                self._estop_apply(False)
                logger.warning(f"✅ [EStop] {by} 解除緊急停止")
                await reply("✅ 已解除緊急停止，Marvin 恢復正常（音樂不會自動續播）。")
            else:
                await reply("目前沒有在緊急停止。", ephemeral=True)
            return
        if st.engage(by, time.time()):
            self._estop_apply(True)
            logger.warning(f"🛑 [EStop] {by} 用斜線指令緊急停止")
            await reply("🛑 已緊急停止：Marvin 不出聲、音樂停止。用 `/marvin_estop` 選「解除」恢復。")
            mc = self.bot.cogs.get("MusicCog")
            if mc is not None:
                try:
                    await mc.stop_stream("緊急停止")
                    await mc.stop_radio("緊急停止")
                except Exception:
                    logger.exception("🛑 [EStop] 停歌失敗")
        else:
            await reply("已經在緊急停止中。", ephemeral=True)
