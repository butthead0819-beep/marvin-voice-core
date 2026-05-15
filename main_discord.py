import discord
from discord import app_commands
from discord.ext import commands
import asyncio
import os
import sys
import time
import logging
import logging.handlers

logger = logging.getLogger("MarvinBot")
from dotenv import load_dotenv
load_dotenv()

class _StreamToLogger:
    """File-like stream that sends print()/traceback output to a rotating logger."""
    def __init__(self, target_logger: logging.Logger, level: int):
        self.target_logger = target_logger
        self.level = level
        self._buffer = ""

    def write(self, message: str):
        if not message:
            return
        self._buffer += message
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if line.strip():
                self.target_logger.log(self.level, line)

    def flush(self):
        if self._buffer.strip():
            self.target_logger.log(self.level, self._buffer.strip())
        self._buffer = ""

def setup_early_logging():
    logging.basicConfig(
        level=logging.WARNING, # 🛑 [Optimization] 降低日誌冗餘，只記錄警告與錯誤
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    main_handler = logging.handlers.RotatingFileHandler(
        filename="bot_main.log",
        maxBytes=10*1024*1024, 
        backupCount=5,
        encoding="utf-8"
    )
    main_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logging.getLogger().addHandler(main_handler)
    logging.getLogger("cogs.voice_controller").setLevel(logging.INFO)

    stdout_logger = logging.getLogger("MarvinBot.Stdout")
    stdout_logger.setLevel(logging.INFO)
    stdout_logger.propagate = False
    stdout_handler = logging.handlers.RotatingFileHandler(
        filename="bot_stdout.log",
        maxBytes=int(os.getenv("STDOUT_LOG_MAX_MB", "5")) * 1024 * 1024,
        backupCount=int(os.getenv("STDOUT_LOG_BACKUPS", "3")),
        encoding="utf-8"
    )
    stdout_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    stdout_logger.addHandler(stdout_handler)
    sys.stdout = _StreamToLogger(stdout_logger, logging.INFO)
    sys.stderr = _StreamToLogger(stdout_logger, logging.ERROR)
    
    # [Early Progress]
    print("🚀 Marvin Bot is waking up...")
    logger.info("🚀 Marvin Bot is waking up (Logging Initialized)...")

setup_early_logging()

# 🚀 [Injection] DAVE & macOS UDP Patch
import davey_bridge
davey_bridge.apply_davey_fix()
davey_bridge.apply_macos_udp_patch()

from davey_bridge import apply_davey_fix
apply_davey_fix() # 🚀 [Security Fix] 在載入任何語音模組前，先修正 DAVE 容器

# 🛡️ [Environment Patch] 確保 macOS (Apple Silicon) 的 Homebrew 路徑在 PATH 中，避免找不到 ffmpeg
for path in ["/opt/homebrew/bin", "/usr/local/bin"]:
    if path not in os.environ["PATH"]:
        os.environ["PATH"] = path + os.path.pathsep + os.environ["PATH"]

# 載入核心引擎
# print("📦 Loading core engines...")
# from gemini_router import GeminiRouter
# from discord_voice_engine import DiscordVoiceEngine
# from screen_capture import ScreenCaptureEngine, VisualBuffer
# from tts_engine import SukiTTS
# from music_engine import SukiMusicEngine
# from gm_operator import GMOperator
# print("✅ All core engines imported.")

# ── CompanionBridge wiring（Phase 3a）─────────────────────────────────────
# 模組層級匯入 + 輔助 function，方便測試 patch 與 mock。
from marvin_voice_core.companion_bridge import CompanionBridge


async def start_companion_bridge(bot, voice_controller=None):
    """根據 env 啟動 CompanionBridge，掛到 bot.companion_bridge。

    依賴：bot.router.atmosphere_tracker、bot.router.memory（suki_memory）、
    bot.music_memory；guild_id 由 COMPANION_GUILD_ID 環境變數取（預設 0）。
    """
    enabled = os.getenv("COMPANION_BRIDGE_ENABLED", "true").lower() != "false"
    if not enabled:
        logger.info("[Companion_Bridge] disabled via env, skipping startup")
        bot.companion_bridge = None
        return

    # 從 router 取 atmosphere_tracker 與 suki_memory（既有實例，不重建）
    tracker = getattr(getattr(bot, "router", None), "atmosphere_tracker", None)
    suki = getattr(getattr(bot, "router", None), "memory", None)
    music = getattr(bot, "music_memory", None)
    # vector_store：voice_controller 內部持有，取用其 _vector_store；fallback 新建
    vs = getattr(voice_controller, "_vector_store", None)
    if vs is None:
        from vector_store import VectorStore
        vs = VectorStore()

    guild_id = int(os.getenv("COMPANION_GUILD_ID", "0") or 0)
    port = int(os.getenv("COMPANION_BRIDGE_PORT", "8766"))

    music_engine = getattr(bot, "music_engine", None)

    bridge = CompanionBridge(
        atmosphere_tracker=tracker,
        vector_store=vs,
        music_memory=music,
        suki_memory=suki,
        voice_controller=voice_controller,
        music_engine=music_engine,
        guild_id=guild_id,
    )
    await bridge.start(host="127.0.0.1", port=port)
    bot.companion_bridge = bridge
    logger.info(f"[Companion_Bridge] started on 127.0.0.1:{port}")


async def _atmosphere_emit_loop(bridge, interval: float = 10.0):
    """周期廣播 atmosphere snapshot。由 bot 啟動時 spawn，shutdown 時 cancel。"""
    while True:
        await asyncio.sleep(interval)
        try:
            await bridge.emit_atmosphere_snapshot()
        except Exception as e:
            logger.warning(f"[Companion_Bridge] periodic emit failed: {e}")


async def _voice_snapshot_loop(bridge, bot, interval: float = 15.0):
    """周期廣播 voice channel snapshot，讓晚連的 companion 能拿到當前成員。"""
    while True:
        await asyncio.sleep(interval)
        try:
            vcs = list(bot.voice_clients)
            if not vcs:
                continue
            channel = vcs[0].channel
            members = [
                {
                    "speaker": m.display_name,
                    "avatar_url": str(m.display_avatar.url),
                }
                for m in channel.members if not m.bot
            ]
            await bridge.emit_voice_channel_snapshot(members)
        except Exception as e:
            logger.warning(f"[Companion_Bridge] voice snapshot loop failed: {e}")




class MarvinBot(commands.Bot):
    """
    馬文 (Marvin) 戰術指揮中心 (Operation Paranoid Android)
    核心 Bot 類別，負責引擎初始化與模組載入。
    """
    def __init__(self):
        # 🟢 [Optimization] 設置 Intents
        intents = discord.Intents.all()
        super().__init__(
            command_prefix="!",
            intents=intents,
            help_command=None # 既然馬文不屑幫助人類，就關掉它
        )
        
        # 1. 初始化日誌系統 (細節配置)
        self._configure_special_loggers()
        
        # 2. 初始化核心引擎與變數
        self.api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not self.api_key and os.getenv("LLM_PROVIDER") == "gemini":
            raise ValueError("請先在 .env 檔案中設定 GEMINI_API_KEY")
            
        self.vision_enabled = os.getenv("VISION_ENABLED", "True").lower() == "true"
        self.visual_buffer = None
        self.screen_capture = None
        
        if self.vision_enabled:
            from screen_capture import ScreenCaptureEngine, VisualBuffer
            self.visual_buffer = VisualBuffer(max_seconds=30)
            self.screen_capture = ScreenCaptureEngine(self.visual_buffer)
            print("👁️  視覺系統已啟動。")
        
        from gemini_router import GeminiRouter
        self.router = GeminiRouter(self.api_key)
        import atexit as _atexit
        _atexit.register(lambda: logging.getLogger(__name__).info(
            f"⚡ [Prefetch Stats] HITs={self.router._prefetch_hits}/{self.router._prefetch_attempts}"
            + (f" ({self.router._prefetch_hits/self.router._prefetch_attempts:.0%})"
               if self.router._prefetch_attempts else " (no attempts)")
        ))

        from discord_voice_engine import DiscordVoiceEngine
        self.engine = DiscordVoiceEngine(self)

        from tts_engine import SukiTTS
        self.tts_engine = SukiTTS()
        
        from music_engine import SukiMusicEngine
        self.music_engine = SukiMusicEngine(self.api_key)
        
        from gm_operator import GMOperator
        self.gm_operator = GMOperator(self) # 傳入 bot 實例
        self.last_restart_time = time.time()

        from sticker_manager import StickerManager
        self.sticker_manager = StickerManager()

        from music_memory import MusicMemory
        self.music_memory = MusicMemory()

    def _configure_special_loggers(self):
        # 🛡️ [Noise Control] 屏蔽 discord.ext.voice_recv 的大量 INFO 雜訊 (RTCP, CryptoError 等)
        logging.getLogger("discord.ext.voice_recv").setLevel(logging.DEBUG)
        logging.getLogger("discord.ext.voice_recv.reader").setLevel(logging.DEBUG)

        # STT 歷史日誌
        stt_logger = logging.getLogger("STTHistory")
        stt_logger.setLevel(logging.INFO)
        stt_handler = logging.handlers.RotatingFileHandler(
            filename="stt_history.log",
            maxBytes=5*1024*1024,
            backupCount=3,
            encoding="utf-8"
        )
        stt_handler.setFormatter(logging.Formatter("%(asctime)s - %(message)s"))
        stt_logger.addHandler(stt_handler)
        stt_logger.info("--- Marvin Bot (Cog Edition) Initialized ---")

    async def setup_hook(self):
        """Discord.py 啟動鉤子：載入 Cogs 並同步指令樹"""
        logger.info("="*60)
        logger.info("🚀 [系統啟動中] 準備執行指令樹清理與模組載入...")
        
        # 1. 清除 Discord 伺服器端的全域指令殘留 (防止重複顯示)
        # 策略：先送出空的全域指令表，讓 Discord 刪掉之前 global sync 留下的舊版本。
        # ⚠️ global sync 受 Discord rate limit 影響，加 timeout 避免 setup_hook 卡死
        logger.info("🗑️ [Cleanup] 清除 Discord 全域指令...")
        self.tree.clear_commands(guild=None)
        try:
            await asyncio.wait_for(self.tree.sync(), timeout=10.0)
            logger.info("✅ [Cleanup] 全域指令清除完畢。")
        except asyncio.TimeoutError:
            logger.warning("⚠️ [Cleanup] 全域指令 sync 逾時（Discord rate limit？），跳過繼續啟動。")
        except Exception as e:
            logger.warning(f"⚠️ [Cleanup] 全域指令 sync 失敗: {e}，跳過繼續啟動。")

        # 2. 載入 Cogs (在清空樹之後載入，確保指令登記在正確的 local 狀態)
        cogs = ["cogs.gm_commands", "cogs.voice_controller", "cogs.game_cog", "cogs.busted99_cog", "cogs.detective_cog"]
        for cog in cogs:
            try:
                await self.load_extension(cog)
                logger.info(f"✅ [Cog] 已載入模組: {cog}")
            except Exception as e:
                logger.error(f"❌ [Cog] 載入模組 {cog} 失敗: {e}")
        
        # 🛡️ [Security] 為 Tree 加上全域錯誤處理器
        @self.tree.error
        async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
            logger.error(f"❌ [App Command Error] {error} (Command: {interaction.command.name if interaction.command else 'N/A'})")
            if not interaction.response.is_done():
                await interaction.response.send_message(f"⚠️ **馬文的系統出錯了**: {error}", ephemeral=True)
        logger.info("="*60)

        # 3. [Lifecycle] 視覺系統
        if self.vision_enabled and self.screen_capture:
            logger.info("👁️  視覺系統已就緒 (等待召喚啟動)。")

        # 4. 啟動語音引擎背景任務
        logger.info("🎙️  啟動語音引擎背景任務...")
        self.engine.start()

        # 5. 啟動 Marmo webhook 伺服器 (must be after load_extension so VoiceController is ready)
        from marvin_voice_core.marmo_server import MarmoServer
        vc_cog = self.cogs.get("VoiceController")
        if vc_cog:
            self.marmo_server = MarmoServer(voice_controller=vc_cog)
            await self.marmo_server.start()
        else:
            logger.warning("[MarmoServer] VoiceController cog not found — Marmo webhook not started")

        # 6. 啟動 CompanionBridge（Phase 3a）— 與 MarmoServer 並列
        try:
            await start_companion_bridge(self, voice_controller=vc_cog)
            if getattr(self, "companion_bridge", None) is not None:
                # 周期廣播 atmosphere snapshot；shutdown 時 cancel
                self._atmosphere_emit_task = self.loop.create_task(
                    _atmosphere_emit_loop(self.companion_bridge, interval=10.0)
                )
                self._voice_snapshot_task = self.loop.create_task(
                    _voice_snapshot_loop(self.companion_bridge, self, interval=15.0)
                )
        except Exception as e:
            logger.warning(f"[Companion_Bridge] startup failed: {e}")

        # 7. ── 環境智能助理 — DiscordTemperatureMonitor + TopicGenerator ──
        if vc_cog is not None:
            from topic_generator import TopicGenerator
            from discord_temperature_monitor import DiscordTemperatureMonitor
            import asyncio as _asyncio

            groq_client = getattr(getattr(self, 'router', None), 'groq_dedicated_client', None)
            _topic_gen = TopicGenerator(
                vector_store=vc_cog._vector_store,
                transcript_store=vc_cog._transcript_store,
                groq_client=groq_client,
            )

            async def _tts_fn(text: str) -> None:
                await vc_cog.play_tts(text, already_in_channel=True)

            _wake_detector = getattr(getattr(self, 'router', None), 'wake_fusion', None)
            _companion_bridge = getattr(self, "companion_bridge", None)
            _temp_monitor = DiscordTemperatureMonitor(
                wake_detector=_wake_detector,
                topic_generator=_topic_gen,
                tts_fn=_tts_fn,
                companion_bridge=_companion_bridge,
            )
            vc_cog.temperature_monitor = _temp_monitor
            vc_cog.topic_generator = _topic_gen

            # on_message → 文字溫度計數
            _temp_channel_id_str = os.environ.get("TEMP_TEXT_CHANNEL_ID", "0")
            _temp_channel_id = int(_temp_channel_id_str) if _temp_channel_id_str.isdigit() else 0

            async def _on_message_for_temperature(message) -> None:
                if _temp_channel_id and message.channel.id == _temp_channel_id:
                    _temp_monitor.record_message_event(str(message.channel.id))
            self.add_listener(_on_message_for_temperature, "on_message")

            # voice state update → session reset（Jack 離開語音頻道時）
            async def _on_voice_state_update_for_temp(_member, before, after) -> None:
                if before.channel and not after.channel:
                    _temp_monitor.reset_session()
            self.add_listener(_on_voice_state_update_for_temp, "on_voice_state_update")

            # 每分鐘溫度檢查 task
            async def _temperature_check_loop() -> None:
                await self.wait_until_ready()
                while not self.is_closed():
                    await _asyncio.sleep(60)
                    try:
                        await _temp_monitor.check_and_trigger()
                    except Exception:
                        pass
            self.loop.create_task(_temperature_check_loop())

            logger.info("[AmbientIntelligence] DiscordTemperatureMonitor + TopicGenerator initialized")

    async def on_ready(self):
        logger.info(f"🤖 馬文已連線。帳號: {self.user} (ID: {self.user.id})")
        logger.info(f"🏘️  本尊已潛入以下 {len(self.guilds)} 個伺服器：")
        
        # [Sync Logic] 不再需要重新載入 Cog，直接同步至各 Guild 即可
        for guild in self.guilds:
            logger.info(f" - {guild.name} (ID: {guild.id})")
            try:
                self.tree.copy_global_to(guild=guild)
                guild_synced = await self.tree.sync(guild=guild)
                logger.info(f"   ✅ [Guild Sync] 同步成功: {guild.name} (指令數: {len(guild_synced)})")
            except Exception as e:
                logger.error(f"   ❌ [Guild Sync] 同步失敗 ({guild.name}): {e}")


        logger.info("💡 [Admin Tip] 目前已執行自動同步。若指令仍未出現，請嘗試重啟 Discord 客戶端。")

        # 🎭 [Sticker] 載入 Clyde 貼圖包
        await self.sticker_manager.load(self)

    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type == discord.InteractionType.application_command:
            logger.info(f"📥 [Interaction] 接收到指令: /{interaction.command.name if interaction.command else 'Unknown'} 由 {interaction.user}")
        # 注意：不要呼叫 self.tree.process_interaction(interaction)
        # 因為 commands.Bot 已經內建了這個邏輯。手動呼叫會導致 AttributeError 或 重複處理。
        # 我們只在這裡做日誌記錄。

    # --- 🛠️ [Operation Overlord] 系統級 prefix 指令 ---
    @commands.command(name="sync")
    async def sync(self, ctx: commands.Context):
        """手動同步當前伺服器的 Slash Commands"""
        print(f"🔄 正在為伺服器 {ctx.guild.name} (ID: {ctx.guild.id}) 進行強制指令同步...")
        await ctx.send("⚙️ 既然你堅持... 我就強行把那些無意義的指令塞進這個伺服器的喉嚨裡。")
        try:
            self.tree.copy_global_to(guild=ctx.guild)
            synced = await self.tree.sync(guild=ctx.guild)
            print(f"✨ 同步成功：已將 {len(synced)} 個指令同步至 {ctx.guild.name}")
            await ctx.send(f"✅ **同步成功**：已將 {len(synced)} 個指令強制對齊至此伺服器。")
        except Exception as e:
            print(f"❌ 同步失敗: {e}")
            await ctx.send(f"⚠️ **同步失敗**：{e}")

    async def close(self):
        """[Lifecycle Cleanup] 確保在關閉 Bot 時，釋放所有擷取資源"""
        if self.screen_capture:
            logger.info("🛑 [Shutdown] 正在釋放視覺系統資源...")
            self.screen_capture.stop()
        if hasattr(self, "marmo_server"):
            await self.marmo_server.stop()
        # 關閉 CompanionBridge（Phase 3a）
        for attr in ("_atmosphere_emit_task", "_voice_snapshot_task"):
            task = getattr(self, attr, None)
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        bridge = getattr(self, "companion_bridge", None)
        if bridge is not None:
            try:
                await bridge.stop()
            except Exception as e:
                logger.warning(f"[Companion_Bridge] stop failed: {e}")
        await super().close()

    # --- 🛡️ [Error Handlers] ---
    async def on_command_error(self, ctx, error):
        if isinstance(error, commands.CommandNotFound):
            return
        print(f"❌ [Prefix Command Error] {error}")
        await ctx.send(f"⚠️ 發生錯誤: {error}")

async def main():
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        print("❌ 錯誤：找不到 DISCORD_BOT_TOKEN")
        return

    bot = MarvinBot()
    try:
        async with bot:
            await bot.start(token)
    except KeyboardInterrupt:
        print("\n🛑 收到終止訊號，馬文 終於可以休息了。")
    except Exception as e:
        print(f"\n❌ [系統未預期錯誤] {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())
