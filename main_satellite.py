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
  - 🧊 預設啟用 ephemeral 記憶沙盒＝唯讀繼承正本、寫入全 no-op、斷線丟棄
    ∴ **可與 24/7 Discord bot 並存、不必停一啟一**（見 design_ephemeral_sandbox_memory）。
    代價：satellite 講的話/點的歌不進正本（session 內 RAM cache 連貫、進程結束即忘）。
    escape hatch：設 MARVIN_MEMORY_SANDBOX=0 關沙盒直接寫正本，但須先停 Discord bot。
  - 衛星斷線會自動 5s 重連（不炸腦）；驗收天梯見 docs/device/S4_integration.md
  - 按 Ctrl-C 乾淨結束
"""
import asyncio
import logging
import os

from dotenv import load_dotenv

import memory_sandbox
from car_http_app import inject_text, start_text_http_server

logger = logging.getLogger(__name__)


def maybe_activate_memory_sandbox(env) -> bool:
    """satellite 預設啟用 ephemeral 記憶沙盒＝唯讀繼承正本、寫入 no-op、斷線丟棄。

    這是「satellite/discord 模式共存不搶寫正本」的關鍵：沙盒下 satellite 進程
    絕不寫 marvin.db / music_memory.json / 等正本，∴ 24/7 Discord bot 可同時活著、
    不必停一啟一（見 design_ephemeral_sandbox_memory）。

    env `MARVIN_MEMORY_SANDBOX=0` ＝escape hatch：關沙盒、讓 satellite 直接寫正本
    （舊工作流，需先停掉 Discord bot 避免 lost-update）。回傳是否啟用。
    """
    if env.get("MARVIN_MEMORY_SANDBOX", "1").strip() == "0":
        logger.warning(
            "⚠️ [Satellite] 記憶沙盒關閉（MARVIN_MEMORY_SANDBOX=0）＝直接寫正本；"
            "務必先停掉 24/7 Discord bot，否則並行寫會 lost-update")
        return False
    memory_sandbox.activate()
    logger.info(
        "🧊 [Satellite] ephemeral 記憶沙盒啟用：唯讀繼承正本、寫入全 no-op、斷線丟棄"
        "（可與 24/7 Discord bot 並存不搶寫）")
    return True


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
    """構建 MarvinBot 腦（不登入 Discord）。"""
    import marvin_speech_log
    marvin_speech_log.set_origin("satellite")
    from main_discord import MarvinBot
    return MarvinBot()


async def setup_satellite(bot):
    """載入必要 cog 並啟動衛星聆聽（可測試的 wiring 層）。

    順序對齊 setup_hook：music_cog 必須先於 voice_controller。

    回傳 (vc, stream_out)：MARVIN_CAR_HARDWARE=pi_bt 才有 stream_out（車 puck 用的
    StreamSpeakerOutput，見下方說明）；其餘情況 stream_out=None，行為跟改動前一致。

    2026-08-20：車 puck（Pi Zero 2W）換歌決策/DJ口白全部改回跟家用喇叭共用同一顆
    mixer——之前 pi_bt 走一套獨立的「Mac 送 play/queue_next/crossfade 指令、Pi 自己
    resolve+decode+crossfade」架構，反覆踩到 deck 尾段被腰斬、口白跟換歌時機各自
    一個時鐘對不上（跟 ESP32 car puck 早期繞的彎路是同一類問題）。ESP32 車 puck 真正
    在響的音訊其實走 /audio_stream（見 handle_audio_stream docstring）——Mac 端
    mixer 混好的音訊直接連續廣播出去，裝置端純粹「有訊號就播」，不需要理解「歌」
    這個概念，crossfade/DJ 口白/音量 ducking 全部在 Mac 這顆 mixer 裡就已經處理好。
    pi_bt 現在比照這條路：StreamSpeakerOutput 掛進 start_satellite_listening() 的
    extra_output（跟家用衛星的 WyomingSpeakerOutput 用 TeeSpeakerOutput 共用同一份
    mixer 輸出，互不影響），呼叫端把回傳的 stream_out 接進 start_text_http_server()
    的 stream_source，車 puck（device/puck_mixer.py 新版）連上 /audio_stream 就跟
    ESP32 一樣「像收音機」連續播放。"""
    await bot.load_extension("cogs.music_cog")
    await bot.load_extension("cogs.voice_controller")
    bot.engine.start()
    vc = bot.cogs.get("VoiceController")
    if vc is None:
        raise RuntimeError("VoiceController cog 未載入，無法啟動衛星聆聽")

    stream_out = None
    if os.getenv("MARVIN_CAR_HARDWARE", "").strip().lower() == "pi_bt":
        from marvin_voice_core.stream_speaker_output import StreamSpeakerOutput
        stream_out = StreamSpeakerOutput(bot.loop)
        vc.start_satellite_listening(extra_output=stream_out)
        # 比照 setup_browser_satellite 的車載模式：開機立刻 arm 泵，讓靜音幀先流動，
        # 車 puck 一連上 /audio_stream 就有東西可讀，不用等第一句話/第一首歌才出聲。
        vc._ensure_mixer_playing(vc._resolve_playback_device())
        logger.info("🚗 [CarMode/pi_bt] StreamSpeakerOutput 已接進家用衛星 mixer（/audio_stream 可用）")
    else:
        vc.start_satellite_listening()

    return vc, stream_out


async def setup_browser_satellite(bot):
    """純軟體 satellite wiring：載 cog + 綁輸出（不連 Pi）。

    一般模式 → BrowserSpeakerOutput（靜音切段快取，GET /reply 給瀏覽器）。
    MARVIN_CAR_MODE=1 → 改用 StreamSpeakerOutput（逐 frame 即時轉送，GET /audio_stream
    給 ESP32 puck；不緩衝整段，音樂/歌單長度不受 PSRAM 限制）+ persistent=True（比照
    Pi 常駐喇叭連續泵，見 [[project_marvin_physical_speaker]]/mk2）。

    回 (vc, browser_out, stream_out)：非車載模式 stream_out=None；車載模式 browser_out=None
    （/reply 停用，全走 /audio_stream）。
    """
    await bot.load_extension("cogs.music_cog")
    await bot.load_extension("cogs.voice_controller")
    bot.engine.start()
    vc = bot.cogs.get("VoiceController")
    if vc is None:
        raise RuntimeError("VoiceController cog 未載入，無法啟動純軟體 satellite")

    if os.getenv("MARVIN_CAR_MODE", "").strip().lower() in ("1", "true", "yes", "on"):
        from marvin_voice_core.stream_speaker_output import StreamSpeakerOutput
        # 2026-07-25：mono_downmix 曾懷疑是頻寬瓶頸解法，後來確認瓶頸其實是雙重 TLS
        # 解密（見 audioNetworkTask 改明碼直連區網 IP）+ mixer 端即時解碼 underrun，
        # 跟聲道數無關；plain HTTP 修好後實測 throughput 180-340+ KB/s，遠超 stereo
        # 需要的 192KB/s，沒必要犧牲音質，改回 stereo。
        stream_out = StreamSpeakerOutput(bot.loop)
        vc.start_browser_satellite_listening(stream_out, persistent=True)
        # 泵預設 on-demand（只有真的有 TTS/音樂要推才 arm）。車載模式若開機後還沒東西
        # 觸發（例如 on_arrive 選歌失敗），/audio_stream 訂閱者會收不到任何 frame——
        # ESP32 firmware 用 Arduino Stream 預設 1s 讀取逾時，空等會誤判斷線、重連，
        # 形成永久 1 秒重連迴圈（2026-07-23 實測）。啟動當下立刻 arm，讓 silence 幀
        # 先流動，不等第一句話。
        vc._ensure_mixer_playing(vc._resolve_playback_device())
        return vc, None, stream_out

    from marvin_voice_core.browser_speaker_output import BrowserSpeakerOutput
    browser_out = BrowserSpeakerOutput()
    vc.start_browser_satellite_listening(browser_out)
    return vc, browser_out, None


async def _stdin_text_input_loop(vc):
    """監聽 stdin 輸入文字，直接注入 Marvin pipeline（本機終端手打／貼 Siri 轉錄）。"""
    import sys
    # ⚠️ stdin 非互動終端（launchd / nohup </dev/null / 背景進程）：readline 立即回 ""
    # 不阻塞，且 EOFError 不會觸發（readline 回 "" 不 raise）→ while 迴圈瘋狂空轉、狂丟
    # run_in_executor 任務 → 燒滿一核 CPU。非 tty 就不啟用（本來也沒終端可打字）。
    if not sys.stdin or not sys.stdin.isatty():
        logger.info("📝 [TextInput] stdin 非互動終端，跳過 stdin 輸入迴圈（避免 EOF busy-spin）")
        return

    loop = asyncio.get_event_loop()
    speaker = os.getenv("MARVIN_SATELLITE_SPEAKER", "狗與露")

    logger.info(f"📝 [TextInput] stdin 模式啟用（speaker={speaker}）；打字後按 Enter 送出")

    while True:
        try:
            text = await loop.run_in_executor(None, sys.stdin.readline)
            if not text:   # EOF：readline 回 "" 不 raise EOFError；不 break 會 busy-spin
                break
            await inject_text(vc, speaker, text)
        except EOFError:
            break
        except Exception as e:  # noqa: BLE001
            logger.error(f"❌ [TextInput] 處理文字失敗: {e}", exc_info=True)


def _start_selftest_if_configured(bot) -> None:
    """Selftest：免喚醒直接播放，測音訊路徑（腦 mixer→衛星/串流→喇叭）。

    不設任何 SELFTEST_* env＝一般模式、零影響。兩種模式：
      MARVIN_SATELLITE_SELFTEST_MP3   ＝本地 mp3 檔或資料夾，連續播（繞過 YouTube／yt-dlp
                                        限流＝乾淨測音訊路徑＋換歌轉場，車載模式下即測
                                        /audio_stream 真實 mixer 輸出，不必靠 PTT 觸發）
      MARVIN_SATELLITE_SELFTEST_QUERY ＝語音點歌 query（走 yt-dlp）
    兩種 satellite 分支（browser/Pi）共用同一套邏輯，故抽成獨立函式。
    """
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
                    # 真實 mixer 路徑，連續播＝測音訊路徑 + 換歌轉場。
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


async def main():
    # 錨定 repo 根目錄：相對路徑的正本記憶(marvin.db/music_memory.json/records/)+assets+
    # models+repo 的 .env(GUILD_ID) 全用正本，不論從哪啟動都不會漂到別的 worktree。
    os.chdir(repo_root())
    load_dotenv()
    # 沙盒必須在建 bot（→建各 store）之前啟用，否則 store 會以讀寫模式開連線
    maybe_activate_memory_sandbox(os.environ)
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
        # 純軟體 satellite（MARVIN_SATELLITE_BROWSER=1）：手機瀏覽器收音+放音，完全不連 Pi。
        # 與 Pi 衛星是兩條獨立路，Pi 模式（else）一行不受影響。
        if os.getenv("MARVIN_SATELLITE_BROWSER", "").strip().lower() in ("1", "true", "yes", "on"):
            vc, browser_out, stream_out = await setup_browser_satellite(bot)
            port = os.getenv("MARVIN_TEXT_PORT", "8790")
            logger.info(f"🛰️ [Satellite] 純軟體瀏覽器模式（無 Pi）：手機開 http://<mac>:{port}/satellite")
            asyncio.create_task(_stdin_text_input_loop(vc))
            await start_text_http_server(vc, reply_source=browser_out, stream_source=stream_out)
            _start_selftest_if_configured(bot)
            await asyncio.Event().wait()
            return

        vc, stream_out = await setup_satellite(bot)
        host = os.getenv("MARVIN_SATELLITE_HOST", "marvinpi.local")
        logger.info(f"🛰️ [Satellite] 衛星模式啟動完成，連向 {host}，等 Pi 麥喚醒...")

        # 文字輸入：stdin（本機手打）+ HTTP（Siri 捷徑走 Tailscale POST /say）
        # stream_out：MARVIN_CAR_HARDWARE=pi_bt 才非 None（見 setup_satellite docstring），
        # 接進 /audio_stream 給車 puck 連續收音；其餘情況 None，/audio_stream 404，零行為改變。
        asyncio.create_task(_stdin_text_input_loop(vc))
        await start_text_http_server(vc, stream_source=stream_out)
        _start_selftest_if_configured(bot)
        await asyncio.Event().wait()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 [Satellite] 收到 Ctrl-C，正在結束...")
