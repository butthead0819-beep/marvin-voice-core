"""
main_local.py — 本機 standalone 啟動入口（完全不登入 Discord）

Live 執行步驟：
  1. 把 .env 複製進 worktree 根目錄（和 main_discord.py 同層）
  2. /Users/jackhuang/Code/Discord-voice-bot/venv_simon/bin/python main_local.py
  3. 對麥克風先喊喚醒詞「馬文」，再說話
  4. 從喇叭聽回應

注意事項：
  - 不登入 Discord——不與線上 24/7 bot 的同 token 衝突
  - DB 為 worktree 全新（records/、suki_memory.db 等從零建立）
  - 故此次不具記憶延續（先前對話歷史不帶入）
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


async def setup_local(bot) -> object:
    """載入必要 cog 並啟動本機聆聽（可測試的 wiring 層）。

    順序對齊 setup_hook：music_cog 必須先於 voice_controller。
    """
    await bot.load_extension("cogs.music_cog")
    await bot.load_extension("cogs.voice_controller")
    bot.engine.start()
    vc = bot.cogs.get("VoiceController")
    if vc is None:
        raise RuntimeError("VoiceController cog 未載入，無法啟動本機聆聽")
    vc.start_local_listening()
    return vc


async def run_selftest(vc) -> None:
    """繞過 mic/STT/LLM，直接測輸出半邊：先播本地音樂、再唸一句 edge-tts。

    有聽到聲音＝play_music / play_tts → mixer → LocalSpeakerDevice → 喇叭 端到端通。
    """
    default_mp3 = "/Users/jackhuang/Code/Discord-voice-bot/records/suki_voice_95a107bcac0093cf875ae74fe1706c8b.mp3"
    music = os.getenv("SELFTEST_MP3", default_mp3)
    logger.info(f"🧪 [SelfTest] 1/2 play_music（純本地檔、無網路無 LLM）：{music}")
    try:
        if os.path.exists(music):
            await vc.play_music(music, "[SelfTest]")
        else:
            logger.warning(f"🧪 [SelfTest] 找不到音檔：{music}（設 SELFTEST_MP3 指定）")
    except Exception as e:
        logger.warning(f"🧪 [SelfTest] play_music 失敗：{e}")

    await asyncio.sleep(8)

    logger.info("🧪 [SelfTest] 2/2 play_tts（edge-tts 真嗓、無 LLM，需網路）...")
    try:
        await vc.play_tts("哈囉，我是馬文。這是本機輸出測試，一二三。")
    except Exception as e:
        logger.warning(f"🧪 [SelfTest] play_tts 失敗（可能 edge-tts 網路）：{e}")

    logger.info("🧪 [SelfTest] 完成。有聽到＝輸出路徑端到端通（繞過 mic/STT/LLM）。")


async def main(selftest: bool = False):
    load_dotenv()
    bot = build_local_bot()
    # async with bot: 進入 _async_setup_hook（設 event loop）但不呼叫 setup_hook，
    # 不觸發 tree.sync 或任何 Discord 連線動作。
    async with bot:
        vc = await setup_local(bot)
        logger.info("🎙️ [LocalMode] 本機模式啟動完成，對麥克風說話開始互動...")
        if selftest:
            await run_selftest(vc)
        await asyncio.Event().wait()


if __name__ == "__main__":
    import argparse
    _p = argparse.ArgumentParser(description="Marvin 本機 standalone（不登入 Discord）")
    _p.add_argument("--selftest", action="store_true",
                    help="啟動後直接播音樂+TTS 測輸出（繞過 mic/STT/LLM）")
    _args = _p.parse_args()
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(main(selftest=_args.selftest))
    except KeyboardInterrupt:
        print("\n🛑 [LocalMode] 收到 Ctrl-C，正在結束...")
