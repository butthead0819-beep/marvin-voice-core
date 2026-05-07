import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio
import time
from typing import List

# 常見遊戲清單，用於 /set_game 自動完成
GAME_SUGGESTIONS = [
    "GTA Online", "Minecraft", "Apex Legends", "Valorant",
    "League of Legends", "Overwatch 2", "CS2", "Forza Horizon 5",
    "Elden Ring", "Fortnite", "Rocket League", "PUBG",
    "Dead by Daylight", "Among Us", "Stardew Valley", "Terraria",
    "Rust", "7 Days to Die", "Phasmophobia", "It Takes Two",
]

class GMCommands(commands.Cog):
    """
    [Operation GM] 
    馬文 (Marvin) 的上帝權限模組：負責 Minecraft 聯動、遊戲切換與 GM 測試指令。
    """
    def __init__(self, bot):
        self.bot = bot

    def cog_load(self):
        print("🛡️ [GM Commands] Cog 已掛載。")

    def cog_unload(self):
        print("🛡️ [GM Commands] Cog 已卸載。")

    @app_commands.command(name="gm_check", description="[Admin] 檢查馬文與 Minecraft 的連線狀態")
    async def gm_check(self, interaction: discord.Interaction):
        await interaction.response.defer()
        # 透過 bot 實例存取 gm_operator
        resp = await self.bot.gm_operator.execute_command("seed") # 測試指令
        await interaction.followup.send(f"🛡️ **【GM 診斷】**：馬文的回應是「{resp}」")

    async def game_name_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> List[app_commands.Choice[str]]:
        """為 game_name 提供自動完成建議"""
        return [
            app_commands.Choice(name=game, value=game)
            for game in GAME_SUGGESTIONS
            if current.lower() in game.lower()
        ][:25]  # Discord 上限 25 個選項

    @app_commands.command(name="set_game", description="[Admin] 手動切換馬文目前關注的無聊遊戲")
    @app_commands.describe(game_name="遊戲名稱（例如：Apex Legends, Valorant, 戰棋）")
    @app_commands.autocomplete(game_name=game_name_autocomplete)
    async def set_game(self, interaction: discord.Interaction, game_name: str):
        print(f"DEBUG: /set_game called with {game_name}")
        await interaction.response.defer()
        
        try:
            # 呼叫 bot 上的切換邏輯 (或直接執行)
            print(f"🎮 [系統指令] 切換遊戲背景至: {game_name}")
            dict_str = await self.bot.router.set_game_async(game_name)
            self.bot.engine.game_dict_string = dict_str
            
            await interaction.followup.send(f"🎮 **懶散對齊中**：我就算關注《{game_name}》，這世界也不會變得更好。")
        except Exception as e:
            print(f"ERROR: /set_game failed: {e}")
            import traceback
            traceback.print_exc()
            await interaction.followup.send(f"⚠️ 指令執行失敗：{e}")

    @app_commands.command(name="link_mc", description="[GM] 綁定你的 Discord 帳號與 Minecraft ID")
    @app_commands.describe(mc_id="你的 Minecraft 遊戲名稱")
    async def link_mc(self, interaction: discord.Interaction, mc_id: str):
        await interaction.response.defer()
        
        # 透過 bot 實例存取 router
        self.bot.router.memory.set_minecraft_id(interaction.user.display_name, mc_id)
        
        # 馬文風格的回覆
        msg = f"「唉... 寫入完畢。我把你的 Discord 靈魂跟這個無聊方塊世界裡的 ID [{mc_id}] 綁在一起了。希望這能為你空虛的人生帶來一點意義... 雖然我很懷疑。」"
        await interaction.followup.send(f"🧱 {msg}")
        # 呼叫 bot 上的 TTS 播放
        if hasattr(self.bot, "play_tts"):
            await self.bot.play_tts(msg)

async def setup(bot):
    await bot.add_cog(GMCommands(bot))
