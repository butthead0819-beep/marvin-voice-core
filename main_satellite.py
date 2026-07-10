"""
main_satellite.py — 衛星模式 standalone 啟動入口（實體音箱 S4；不登入 Discord）

腦跑在 Mac，麥/喇叭在 Pi（wyoming-satellite）。與 main_local.py 唯一差別＝輸入/輸出
transport 從「Mac 本機 mic/speaker」換成「TCP 連 Pi 衛星」。

Live 執行步驟：
  1. 先在 Pi 起 wyoming-openwakeword + wyoming-satellite（見 docs/device/S3_pi_setup.md）
  2. 從**主 checkout** 跑（非獨立 worktree）＝讀寫**正本記憶**（marvin.db/music_memory.json/
     records/）＋用主 .env 的 GUILD_ID＝跟 Discord 同一個 per-person 記憶分區（同一個靈魂）。
     入口會自動 chdir 到 repo 根目錄，從哪啟動都錨到正本。
     - .env 需有 GUILD_ID（與 Discord 相同，已設）
     - 設 MARVIN_SATELLITE_SPEAKER=狗與露（身分映射→(GUILD_ID, 狗與露) 同分區＝記憶延續）
  3. /Users/jackhuang/Code/Discord-voice-bot/venv_simon/bin/python main_satellite.py
  4. 對 Pi 麥喊喚醒詞「馬文」，再說話；從 Pi 書架喇叭聽回應

注意事項：
  - 不登入 Discord——不與線上 24/7 bot 的同 token 衝突
  - ⚠️ device 直接讀寫正本記憶＝依賴「一次一具身體」：用 device 前要**停掉 24/7 Discord bot**
    （launchd），否則兩進程並行寫記憶會 lost-update（見 project_marvin_physical_speaker）
  - 衛星斷線會自動 5s 重連（不炸腦）；驗收天梯見 docs/device/S4_integration.md
  - 按 Ctrl-C 乾淨結束
"""
import asyncio
import logging
import os

from dotenv import load_dotenv

logger = logging.getLogger(__name__)


def repo_root() -> str:
    """含 main_satellite.py 的 repo 根目錄＝正本記憶/assets/models/.env 所在。"""
    return os.path.dirname(os.path.abspath(__file__))


def check_identity_alignment(env) -> list:
    """回傳記憶對齊警告清單（空＝對齊 OK）。純函式，好測。

    device 是「同一個靈魂的另一具身體」：per-person 記憶按 (GUILD_ID, speaker) 分區，
    兩者都要跟 Discord 一致，才讀得到同一份人格記憶。
    """
    warnings = []
    gid = env.get("GUILD_ID")
    if not gid or gid == "0":
        warnings.append(
            "GUILD_ID 未設或=0 → per-person 記憶會落在分區 0、讀不到 Discord 的人格記憶；"
            "請在 .env 設與 Discord 相同的 GUILD_ID"
        )
    if not env.get("MARVIN_SATELLITE_SPEAKER"):
        warnings.append(
            "MARVIN_SATELLITE_SPEAKER 未設 → 衛星講者不映射到既有身分、記憶不延續；"
            "建議設為 OWNER_SPEAKER（如 狗與露）"
        )
    return warnings


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
    # 錨定 repo 根目錄：相對路徑的正本記憶(marvin.db/music_memory.json/records/)+assets+
    # models+repo 的 .env(GUILD_ID) 全用正本，不論從哪啟動都不會漂到別的 worktree。
    os.chdir(repo_root())
    load_dotenv()
    _warnings = check_identity_alignment(os.environ)
    for _w in _warnings:
        logger.warning(f"⚠️ [Satellite] 記憶對齊：{_w}")
    if not _warnings:
        _gid = os.environ.get("GUILD_ID", "0")
        _spk = os.environ.get("MARVIN_SATELLITE_SPEAKER", "")
        logger.info(f"🛰️ [Satellite] 記憶錨定正本：repo={repo_root()} guild={_gid} speaker={_spk}（同一個靈魂）")
    bot = build_local_bot()
    # async with bot: 進入 _async_setup_hook（設 event loop）但不呼叫 setup_hook，
    # 不觸發 tree.sync 或任何 Discord 連線動作。
    async with bot:
        await setup_satellite(bot)
        host = os.getenv("MARVIN_SATELLITE_HOST", "marvinpi.local")
        logger.info(f"🛰️ [Satellite] 衛星模式啟動完成，連向 {host}，等 Pi 麥喚醒...")
        # Selftest：免喚醒直接播放，測音訊路徑（腦 mixer→衛星→喇叭，如 BT 連續音樂穩定性）。
        # 不設任何 SELFTEST_* env＝一般模式、零影響。兩種模式：
        #   MARVIN_SATELLITE_SELFTEST_MP3   ＝本地 mp3 檔或資料夾，連續播（繞過 YouTube／yt-dlp
        #                                     限流＝乾淨測 BT 音訊路徑＋換歌轉場）
        #   MARVIN_SATELLITE_SELFTEST_QUERY ＝語音點歌 query（走 yt-dlp）
        _mp3 = os.getenv("MARVIN_SATELLITE_SELFTEST_MP3", "").strip()
        _q = os.getenv("MARVIN_SATELLITE_SELFTEST_QUERY", "").strip()
        if _mp3:
            import glob
            import discord
            async def _selftest_local():
                await asyncio.sleep(6)
                mc = bot.cogs.get("MusicCog")
                vc = bot.cogs.get("VoiceController")
                if mc is None or vc is None:
                    logger.warning("⚠️ [Selftest] cog 未載入，跳過")
                    return
                files = sorted(glob.glob(os.path.join(_mp3, "*.mp3"))) if os.path.isdir(_mp3) else [_mp3]
                device = vc._resolve_playback_device()
                if device is None:
                    logger.warning("⚠️ [Selftest] 無播放裝置，跳過")
                    return
                logger.info(f"🎵 [Selftest] 本地 MP3 連續播放 {len(files)} 首（繞過 YouTube）")
                mc.stream_mode = True
                for f in files:
                    if not mc.stream_mode:
                        break
                    logger.info(f"🎵 [Selftest] ▶ {os.path.basename(f)}")
                    vc._current_stream_url = f
                    try:
                        # 乾淨 FFmpegPCMAudio（無 -reconnect 網路參數，本地檔才開得起來）→
                        # 真實 mixer 路徑，連續播＝測 BT 音訊 + 換歌轉場。
                        await vc._mixer_play_music(
                            device, discord.FFmpegPCMAudio(f),
                            still_active=lambda: mc.stream_mode, volume_attr="stream_volume")
                    except Exception as e:   # noqa: BLE001
                        logger.warning(f"⚠️ [Selftest] 播放失敗 {os.path.basename(f)}: {e}")
                mc.stream_mode = False
                logger.info("🎵 [Selftest] 本地 MP3 全部播完")
            asyncio.create_task(_selftest_local())
        elif _q:
            _spk = os.getenv("MARVIN_SATELLITE_SPEAKER", "狗與露")
            async def _selftest_play():
                await asyncio.sleep(6)   # 等衛星橋連上 Pi + Pi 端就緒
                mc = bot.cogs.get("MusicCog")
                if mc is None:
                    logger.warning("⚠️ [Selftest] MusicCog 未載入，跳過")
                    return
                logger.info(f"🎵 [Selftest] 免喚醒直接點歌：{_q}（speaker={_spk}）")
                await mc._safe_music_command(_spk, _q, "play")
            asyncio.create_task(_selftest_play())
        await asyncio.Event().wait()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 [Satellite] 收到 Ctrl-C，正在結束...")
