import os
import logging
import discord
import asyncio

logger = logging.getLogger(__name__)

class GMOperator:
    """
    Marvin 專用消極 Game Master 模組 (Operation Paranoid Android)
    負責透過 Discord 特定文字頻道與 Minecraft 伺服器通訊，執行上帝指令。
    """
    def __init__(self, bot: discord.Client):
        self.bot = bot
        self.channel_id = os.getenv("MC_CHANNEL_ID")
        if self.channel_id:
            self.channel_id = int(self.channel_id)
        
        # 【白名單設定】：確保馬文不會真的毀滅世界（或者只毀滅一點點）
        self.ALLOWED_COMMANDS = [
            "time", "weather", "summon", "gamemode", "tp", "give", "say"
        ]
        
        logger.info(f"🕹️ GM Operator 初始化完畢 (Target Channel ID: {self.channel_id})")

    async def execute_command(self, full_command: str) -> str:
        """
        將 Minecraft 指令發送至指定的 Discord 頻道。
        """
        if not full_command:
            return "（馬文嘆了口氣，什麼也沒做）"

        # 0. 🛡️ [Hardening] 濾除常見的 LLM 誤解產物 (如 "null", "/cmd|null", "None")
        normalized_cmd = str(full_command).strip().lower()
        if normalized_cmd in ["null", "none", "/cmd|null", ""]:
            return "（馬文不想動彈）"

        if not self.channel_id:
            logger.warning("🚫 [GM] 找不到 MC_CHANNEL_ID，無法發送指令。")
            return "「連日誌頻道都找不到... 果然這世界充滿缺陷。」"

        # 1. 基礎驗證 (移除字首 / 或 !c)
        cmd_root = full_command.strip()
        if cmd_root.startswith("/") or cmd_root.startswith("!c"):
            # 移除前方的符號
            cmd_root = cmd_root.lstrip("/!c ")

        base_cmd = cmd_root.split()[0] if cmd_root else ""

        if base_cmd not in self.ALLOWED_COMMANDS:
            logger.warning(f"🚫 [GM] 拒絕執行未授權指令: {full_command}")
            return f"「這指令 {base_cmd} 太無聊了，我不屑執行。」"

        # 2. 獲取頻道並發送
        try:
            channel = self.bot.get_channel(self.channel_id)
            # 如果 cache 中找不到，嘗試 fetch
            if channel is None:
                channel = await self.bot.fetch_channel(self.channel_id)
                
            if channel:
                # 麥塊後台日誌聊天室，直接發送指令字串
                await channel.send(cmd_root)
                logger.info(f"✅ [GM] 指令已發送至頻道: {cmd_root}")
                return "已發送"
            else:
                 logger.error(f"❌ [GM] 找不到對應的頻道 ID: {self.channel_id}")
                 return "「頻道都不存在，這是要我對著虛無吶喊嗎？」"
                 
        except Exception as e:
            logger.error(f"❌ [GM] 發送 Discord 訊息失敗: {e}")
            return f"「發個訊息都能失敗... 這宇宙沒救了。」"

if __name__ == "__main__":
    # 單體測試不再適用，因為需要 Discord Bot 實例
    pass
