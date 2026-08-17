"""main_local_story_arc.py — 本機喇叭測 DJ 故事弧節目，完全不登入 Discord、不動任何
Discord 使用者的播放狀態。

背景：car puck mk2（Pi Zero 2W + 藍牙 A2DP）不是走 `_resolve_playback_device()` 那套
`PlaybackDevice` 抽象，是完全獨立的 HTTP 協議、且沒有播 TTS/本地檔的介面——要接上去是
另一塊新工程。本機喇叭模式（`start_local_listening()`）走的是跟 Discord 平行的同一套
裝置抽象，`MusicCog._prepare_and_stage_story_arc`/`_play_story_arc` 現有程式碼零改動
就能直接用，天生就不會碰到 Discord 語音頻道裡任何人。

用法：
  venv_simon/bin/python main_local_story_arc.py --members 狗與露 showay --minutes 20
"""
import asyncio
import logging

from dotenv import load_dotenv

from main_local import build_local_bot, setup_local

logger = logging.getLogger(__name__)


async def run_story_arc(bot, members: list, minutes: float) -> None:
    cog = bot.cogs.get("MusicCog")
    if cog is None:
        logger.error("❌ MusicCog 未載入，無法跑故事弧。")
        return

    logger.info(f"📖 正在為 {'、'.join(members)} 編一段故事（本機喇叭測試，不影響 Discord 使用者）…")
    staged, err = await cog._prepare_and_stage_story_arc(members, float(minutes))
    if staged is None:
        logger.error(f"❌ 故事弧沒生成成功：{err}")
        return

    n = len(staged.get("nodes", []))
    logger.info(f"✅ 《{staged.get('arc_title', '')}》準備好了，{n} 首歌 + 口白已預渲染，開始播放…")
    await cog._play_story_arc(staged)
    logger.info("🎬 故事弧播放結束。")


async def main(members: list, minutes: float) -> None:
    load_dotenv()
    bot = build_local_bot()
    async with bot:
        await setup_local(bot)
        await run_story_arc(bot, members, minutes)


if __name__ == "__main__":
    import argparse
    _p = argparse.ArgumentParser(description="本機喇叭測 DJ 故事弧節目（不登入 Discord）")
    _p.add_argument("--members", nargs="+", required=True, help="故事對象（1個以上）")
    _p.add_argument("--minutes", type=float, default=20.0)
    _args = _p.parse_args()
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(main(_args.members, _args.minutes))
    except KeyboardInterrupt:
        print("\n🛑 [LocalMode] 收到 Ctrl-C，正在結束...")
