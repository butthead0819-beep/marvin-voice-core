"""
main_satellite.py — 衛星模式 standalone 啟動入口（實體音箱 S4；不登入 Discord）

腦跑在 Mac，麥/喇叭在 Pi（wyoming-satellite）。與 main_local.py 唯一差別＝輸入/輸出
transport 從「Mac 本機 mic/speaker」換成「TCP 連 Pi 衛星」。

Live 執行步驟：
  1. 先在 Pi 起 wyoming-openwakeword + wyoming-satellite（見 docs/device/S3_pi_setup.md）
  2. 把 .env 複製進 worktree 根目錄（和 main_discord.py 同層）
     - 設 MARVIN_SATELLITE_HOST=marvinpi.local（或 Pi 的 IP）
     - 設 MARVIN_SATELLITE_SPEAKER=狗與露（身分映射→記憶延續，可選）
  3. /Users/jackhuang/Code/Discord-voice-bot/venv_simon/bin/python main_satellite.py
  4. 對 Pi 麥喊喚醒詞「馬文」，再說話；從 Pi 書架喇叭聽回應

注意事項：
  - 不登入 Discord——不與線上 24/7 bot 的同 token 衝突
  - 衛星斷線會自動 5s 重連（不炸腦）；驗收天梯見 docs/device/S4_integration.md
  - 按 Ctrl-C 乾淨結束
"""
import asyncio
import logging
import os

from dotenv import load_dotenv

logger = logging.getLogger(__name__)


def build_local_bot():
    """構建 MarvinBot 腦（不登入 Discord）。VISION_ENABLED 強制 false，避免螢幕擷取依賴。"""
    os.environ["VISION_ENABLED"] = "false"
    from main_discord import MarvinBot
    return MarvinBot()


async def setup_satellite(bot) -> object:
    """載入必要 cog 並啟動衛星聆聽（可測試的 wiring 層）。

    順序對齊 setup_hook：music_cog 必須先於 voice_controller。
    """
    await bot.load_extension("cogs.music_cog")
    await bot.load_extension("cogs.voice_controller")
    bot.engine.start()
    vc = bot.cogs.get("VoiceController")
    if vc is None:
        raise RuntimeError("VoiceController cog 未載入，無法啟動衛星聆聽")
    vc.start_satellite_listening()
    return vc


async def main():
    load_dotenv()
    bot = build_local_bot()
    # async with bot: 進入 _async_setup_hook（設 event loop）但不呼叫 setup_hook，
    # 不觸發 tree.sync 或任何 Discord 連線動作。
    async with bot:
        await setup_satellite(bot)
        host = os.getenv("MARVIN_SATELLITE_HOST", "marvinpi.local")
        logger.info(f"🛰️ [Satellite] 衛星模式啟動完成，連向 {host}，等 Pi 麥喚醒...")
        await asyncio.Event().wait()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 [Satellite] 收到 Ctrl-C，正在結束...")
