"""
ConnectionMixin — VoiceController 的語音連線生命週期 + 自癒（哨兵）。

從 voice_controller.py 抽出（減肥），以 mixin 併入 VoiceController。self 仍是
VoiceController 實例，bot.engine / _mixer / connection_time / sink_failure_count /
play_tts / stop_stream 等沿用原本 self 存取，行為零改動。

  - summon / dismiss（slash 指令）+ handle_summon / handle_dismiss（callback）
  - auto_attach_listener / report_sink_error / handle_fallback_notification /
    orchestrate_recovery / soft_repair_connection / sentinel_monitor_loop（@tasks.loop）
    / _dave_grace_should_forgive（DAVE 寬限期）/ self_restart（物理重啟）

reboot 狀態三人組（_git_head_short / _write_reboot_state / read_and_clear_reboot_state
+ REBOOT_STATE_FILE）一併搬來（只 self_restart 與 on_ready 用）；
read_and_clear_reboot_state 由 voice_controller re-export 給 main_discord。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
import traceback

import discord
from discord import app_commands
from discord.ext import tasks, voice_recv

from local_mixing_source import BufferedF32MusicSource, S16ToF32MusicSource
from marvin_voice_core.playback_device import DiscordPlaybackDevice

logger = logging.getLogger(__name__)


# 重啟回報狀態檔。寫於 self_restart pre-execv，讀於 on_ready post-sync。
REBOOT_STATE_FILE = ".marvin_reboot_state.json"


def music_echo_guard_active(local_mode: bool, is_playing_audio: bool,
                            current_tts_text: str, enabled: bool) -> bool:
    """軟體 Music Echo Guard：播純音樂時，local/satellite（無硬體 AEC）該不該把
    同機喇叭外放、麥又收回的音樂回聲當人聲。純函式，好測。

    True＝忽略衛星喚醒 duck ＋ 不觸發 barge-in。條件全滿足才 arm：
      - enabled           kill-switch（env MARVIN_MUSIC_ECHO_GUARD）未關
      - local_mode        本機/衛星路徑（Discord 路徑此旗標不存在→False→零影響）
      - is_playing_audio  正在播放
      - not current_tts_text  純音樂（無 TTS 文字）；bot 正講 TTS 時使用者仍要能 barge-in
    """
    return bool(enabled and local_mode and is_playing_audio and not current_tts_text)


def _git_head_short() -> str:
    """取目前 HEAD short hash；失敗回 'unknown'。"""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=2.0,
        )
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _write_reboot_state(state: dict) -> None:
    """寫狀態檔（失敗不阻斷重啟流程）。"""
    try:
        with open(REBOOT_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
    except Exception as e:
        logger.error(f"❌ [Restart] 寫 reboot state 失敗（不阻斷）: {e}")


def read_and_clear_reboot_state() -> dict | None:
    """新進程 on_ready 用：讀狀態檔後刪檔。回傳 dict 或 None。"""
    try:
        if not os.path.exists(REBOOT_STATE_FILE):
            return None
        with open(REBOOT_STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        os.remove(REBOOT_STATE_FILE)
        return state
    except Exception as e:
        logger.error(f"❌ [Restart] 讀 reboot state 失敗: {e}")
        try:
            os.remove(REBOOT_STATE_FILE)
        except Exception:
            pass
        return None


def pick_rejoin_channel(guilds, already_connected: bool):
    """開機自動回台目標：任一語音頻道有真人在 → 回傳該頻道；否則 None。

    2026-07-04：kickstart 後 bot 是離台狀態、要人手動 /summon——部署重啟
    每次把馬文踢下台（當晨四連發實錘：10:06 後離台 1.5h 沒人發現）。
    """
    if already_connected:
        return None
    for g in guilds or []:
        for ch in getattr(g, "voice_channels", None) or []:
            if any(not m.bot for m in ch.members):
                return ch
    return None


class ConnectionMixin:
    @staticmethod
    def _dave_grace_should_forgive(now: float, connection_time: float,
                                   last_decrypted_audio_time: float,
                                   grace_s: float = 30.0, early_s: float = 15.0) -> bool:
        """DAVE 寬限期是否該豁免這次解密報錯。

        只在「金鑰真的還在同步」時豁免：連線後 early_s 內（剛連，給同步時間），
        或連線後已成功解密過至少一個封包（last_decrypted >= connection_time）。
        若已過 early_s 卻自連線以來零成功解密 → 不是同步延遲、是連線真的壞了 →
        不豁免，讓錯誤累積觸發升級。修正不穩連線一直 reset connection_time 把
        持續解密失敗風暴永久靜音的盲點（2026-06-04 incident）。
        """
        since_connect = now - connection_time
        if since_connect >= grace_s:
            return False
        if since_connect < early_s:
            return True
        return last_decrypted_audio_time >= connection_time

    def _on_key_desync_storm(self):
        """KeySync 偵測到「重抓 key 仍持續零解密」(secret_key desync 風暴) → 排程完整重連自癒。

        補上 Sentinel 盲點：傳輸層 CryptoError 風暴被 KeySync drop、不走 report_sink_error
        (只數 DAVE 層) → 升級永不觸發 (2026-06-23 incident 炸 40 分)。在 audio 接收執行緒
        被呼叫 → 用 call_soon_threadsafe 排到 event loop 跑 orchestrate_recovery (內有
        is_recovering 去重 + soft-repair→物理重啟分層)。"""
        try:
            loop = getattr(self.bot, "loop", None)
            if loop is None or loop.is_closed():
                return
            loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(self.orchestrate_recovery("key_desync_storm"))
            )
        except Exception:
            logger.debug("[KeySync] 排程 desync 自癒失敗", exc_info=True)

    def report_sink_error(self, error_type: str):
        """
        [Operation Sentinel] 由 Sink 呼叫，匯報 DAVE 底層解密異常。
        🚀 [T-01 Fix] 使用獨立的 dave_error_count，不再污染 sink_missing_count。
        強化：加入 2s 強制防抖冷卻與分層處置機制 (Soft Repair -> Physical Restart)
        """
        current_time = time.time()

        # 🛡️ [Sentinel 2.1] DAVE 寬限期：只在金鑰真的在同步時豁免（見 _dave_grace_should_forgive）。
        # 連線不穩會一直 reset connection_time，舊版「30s 內一律忽略」把持續零解密的風暴
        # 永久靜音、升級永不觸發（2026-06-04 incident）。零成功解密時不再豁免。
        if current_time - getattr(self, "connection_time", 0) < 30:
            if self._dave_grace_should_forgive(current_time,
                                               getattr(self, "connection_time", 0),
                                               getattr(self, "last_decrypted_audio_time", 0)):
                if current_time - getattr(self, "last_failure_time", 0) > 10:
                    logger.info(f"⏳ [Sentinel] DAVE 寬限期內，忽略同步等待中的報錯 ({error_type})")
                return
            logger.warning(f"🛡️ [Sentinel] 寬限期內但連線後零成功解密 → 視為真實失效不再豁免 ({error_type})")

        # 1. 2s 內爆量的錯誤視為同一波，採取節流 (Throttle)
        if current_time - getattr(self, "last_failure_time", 0) < 2:
            return
            
        # 2. 狀態機計數邏輯：60s 內無新 DAVE 錯誤，視為環境已恢復，重置計數
        if current_time - getattr(self, "last_failure_time", 0) > 60:
            self.dave_error_count = 1
        else:
            self.dave_error_count += 1
            
        self.last_failure_time = current_time
        logger.warning(f"🚨 [Sentinel] 收到 DAVE 異常報告 ({error_type})，當前計數: {self.dave_error_count}/3")

        if self.dave_error_count >= 3:
            # 必須丟入 Event Loop 進行非同步執行，以免卡死當前線程
            self.bot.loop.create_task(self.orchestrate_recovery(error_type))

    async def handle_fallback_notification(self, tier_name: str, model_name: str):
        """
        [Operation Sentinel] 只在真正降級到 Ollama (Tier-2/3) 或從中恢復時通知。
        Groq/Cerebras/Gemini 之間的切換屬於正常雲端路由，不打擾聊天室。
        """
        if not self.active_text_channel:
            return

        # 只處理真正影響品質的層級變化
        if tier_name == "Tier-1":
            msg = "🌥️ [系統恢復] 雲端連線已恢復，我又可以正常運作了。雖然這對解決宇宙熵增一點幫助都沒有..."
        elif tier_name == "Tier-2":
            self._last_fallback_ts = time.time()  # 🗣️ [Status ACK] 久候時改回報「切備援腦」
            msg = f"🛰️ [降級警告] 雲端全線失聯，切換到遠端備援核心 `{model_name}`。我那行星般的大腦正在萎縮..."
        elif tier_name == "Tier-3":
            self._last_fallback_ts = time.time()
            msg = f"🏠 [緊急降級] 備援也掛了，只剩本地應急核心 `{model_name}`。這是我見過最悲慘的一天。"
        else:
            return  # 忽略其他層級變化（不應出現）

        try:
            await self.active_text_channel.send(msg)
            logger.info(f"🔔 [Sentinel] 已發送層級通知: {tier_name} ({model_name})")
        except Exception as e:
            logger.error(f"❌ [Sentinel] 發送層級通知失敗: {e}")

    async def orchestrate_recovery(self, error_type: str):
        """
        [Sentinel 核心] 協調分層修復機制：Soft Repair (2次) -> Physical Restart
        """
        if self.is_recovering:
            return
            
        self.is_recovering = True
        try:
            # 🚀 [Sentinel Case 1] 優先執行「軟修復」：重新加入頻道以同步金鑰
            if self.soft_repair_count < 2:
                self.soft_repair_count += 1
                logger.critical(f"🛡️ [Sentinel] 偵測到持續性的底層失效 ({error_type})，啟動【軟修復】程序 ({self.soft_repair_count}/2)...")
                await self.soft_repair_connection(reason=f"底層失效 ({error_type})")
            # 🚀 [Sentinel Case 2] 軟修復無效後，才啟動物理性重啟進程
            else:
                logger.error(f"☢️ [Sentinel] 軟修復失效，正在執行物理重啟 ({error_type}) 以重新同步金鑰。")
                await self.self_restart(reason=f"底層持續失效 ({error_type})", force=True)
        finally:
            # 即使失敗也釋放鎖定，讓 Sentinel Loop 能在未來嘗試
            self.is_recovering = False

    async def soft_repair_connection(self, reason: str):
        """
        [Sentinel 軟修復] 不重啟進程，僅重整語音連線管道
        """
        if not self.bot.voice_clients:
            return

        # TTS 播放中不斷線 — disconnect 會中斷正在播放的語音
        if self.is_playing_audio:
            logger.info(f"🛡️ [Sentinel] TTS 播放中，跳過本次軟修復 ({reason})")
            return

        vc = self.bot.voice_clients[0]
        channel = vc.channel

        # 🚀 [Sentinel] 更新連線時間戳，啟動 30s 寬限期
        self.connection_time = time.time()

        # 1. 向用戶回報異常 (馬文語風) 
        # 💡 [Optimization] 只有在第二次軟修復才發聲，第一次保持靜默以減少噪音
        if self.active_text_channel:
            if self.soft_repair_count >= 2:
                await self.active_text_channel.send(f"⚠️ **【系統診斷：持續性聽覺異常】**\n初次校正無效，正在執行深度感測器重整...")
            else:
                logger.info(f"🛡️ [Sentinel] 正在執行靜默軟修復 (Attempt: {self.soft_repair_count})，原因: {reason}")
        
        # 2. 原子化斷線
        try:
            print(f"🔄 [Soft Repair] 正在從 {channel.name} 斷開以重新握手...")
            await vc.disconnect(force=True)
            await asyncio.sleep(2.0)
        except Exception as e:
            logger.error(f"❌ [Soft Repair] 断开失敗: {e!r}")

        # 3. 仿照 /summon 邏輯進行重連
        try:
            # 建立假 interaction 的 context 結構 (模擬 summon 的呼叫環境)
            # 這裡我們簡化處理，直接呼叫連線邏輯，但需確保 active_text_channel 已存
            print(f"🔄 [Soft Repair] 正在嘗試重新降臨至 {channel.name}...")
            
            # 使用我們已經寫好的 summon 關鍵邏輯
            # 由於 /summon 是 Slash Command，我們這裡手動重建一個微小的連線流
            from discord_voice_engine import RealtimeVADSink, patch_voice_recv_key_sync
            voice_client = await channel.connect(cls=voice_recv.VoiceRecvClient, timeout=60.0, reconnect=True)
            await asyncio.sleep(0.5)

            sink = RealtimeVADSink(
                self.bot.engine.process_audio_slice,
                on_speech_start_callback=self.bot.engine._handle_raw_speech_start,
                temperature_callback=self.bot.engine.conv_buffer.get_conversation_temperature,
                sink_error_callback=self.report_sink_error,
                suppress_wake_callback=lambda: self.stream_mode or self.radio_mode or self.is_playing_audio,
                wake_active_callback=lambda: self._wake_response_pending
            )
            voice_client.listen(sink)
            patch_voice_recv_key_sync(voice_client, on_desync_storm=self._on_key_desync_storm)
            self.bot.engine.sink = sink # 🔗 [Linkage Fix] 直接鏈結回 Engine
            self.connection_time = time.time()
            self.last_recovery_time = time.time()
            self.dave_error_count = 0  # 🚀 [T-01 Fix] 重設 DAVE 錯誤計數（非 sink_missing_count）
            
            # UDP Hole Punching
            if self._plan12:
                # mixer adapter 已提供持續音訊（idle 出 silence），取代 SilenceSource keepalive；
                # 並即時 re-arm，不必等 sentinel tick
                self._ensure_mixer_playing(DiscordPlaybackDevice(voice_client))
            else:
                voice_client.play(self.SilenceSource(20))

            logger.info(f"✅ [Soft Repair] 重連成功！連線狀態: {voice_client.is_connected()}")
            if self.active_text_channel:
                await self.active_text_channel.send("✅ **【校正完畢】**\n聽覺神經已恢復同步，雖然這世界依然吵雜。")
        except Exception as e:
            logger.error(f"❌ [Soft Repair] 重連失敗: {e!r}")
            # 如果軟修復重連都失敗，升級為物理重啟
            # ⚠️ 用 repr：connect(timeout=60) 逾時拋的 asyncio.TimeoutError str() 是空字串，
            #    舊版 f"...: {e}" 讓 incident 訊息冒號後全空、無法判斷失敗原因（2026-06-16 incident）
            await self.self_restart(reason=f"軟修復重連崩潰: {e!r}", force=True)

    async def auto_attach_listener(self):
        """
        [Operation Resilience] 掃描現有連線並重新掛載監聽器。
        解決機器人重連 Gateway 或插件重載後，雖然還在頻道內但處於「失聰」狀態的問題。
        """
        if not self.bot.voice_clients:
            return

        for vc in self.bot.voice_clients:
            if isinstance(vc, voice_recv.VoiceRecvClient) and vc.is_connected():
                print(f"🔗 [Resilience] 偵測到現有語音連線 ({vc.channel.name})，正在自動重新掛載監聽器...", flush=True)
                from discord_voice_engine import RealtimeVADSink, patch_voice_recv_key_sync

                # 建立新的 Sink
                sink = RealtimeVADSink(
                    self.bot.engine.process_audio_slice,
                    on_speech_start_callback=self.bot.engine._handle_raw_speech_start,
                    temperature_callback=self.bot.engine.conv_buffer.get_conversation_temperature,
                    sink_error_callback=self.report_sink_error,
                    suppress_wake_callback=lambda: self.stream_mode or self.radio_mode or self.is_playing_audio,
                wake_active_callback=lambda: self._wake_response_pending
                )

                try:
                    # 如果已經在監聽，先停止 (雖然重載後通常原本的監聽器已隨舊 Cog 銷毀)
                    if vc.is_listening():
                        vc.stop_listening()

                    vc.listen(sink)
                    patch_voice_recv_key_sync(vc)
                    self.bot.engine.sink = sink # 🔗 鏈結回 Engine
                    logger.info(f"✅ [Resilience] 已自動恢復頻道 {vc.channel.name} 的監聽狀態。")
                    
                    # 傳送熱重啟通知 (選填)
                    if self.active_text_channel:
                         await self.active_text_channel.send("🌑 **【系統歸位】**\n偵測到異常離群後重新捕捉到語音同步信號，監聽已自動恢復。")
                except Exception as e:
                    logger.error(f"❌ [Resilience] 自發性恢復監聽失敗: {e}")

    async def auto_rejoin_on_boot(self):
        """🔁 開機自動回台（2026-07-04）：語音頻道有真人 → 靜默回台恢復監聽。

        鏡像 summon 的連線核心（DAVE 連線+sink 掛載），刻意不打招呼、不動
        active_text_channel（安靜回歸）。失敗只 log，可手動 /summon 兜底。
        env MARVIN_AUTO_REJOIN=0 可關。
        """
        if os.getenv("MARVIN_AUTO_REJOIN", "1") == "0":
            logger.warning("🔁 [AutoRejoin] env 關閉，跳過")
            return
        ch = pick_rejoin_channel(self.bot.guilds, bool(self.bot.voice_clients))
        if ch is None:
            # no-op 也要可觀測（7/4 教訓 ×3：沉默無法區分「正確不做」與「沒跑到」）
            logger.warning("🔁 [AutoRejoin] 台上無真人（或已連線），不回台")
            return
        try:
            print(f"🔁 [AutoRejoin] 開機偵測 {ch.name} 有真人，靜默回台...", flush=True)
            self.bot.engine.start()
            from discord_voice_engine import RealtimeVADSink, patch_voice_recv_key_sync
            voice_client = await ch.connect(cls=voice_recv.VoiceRecvClient, timeout=60.0, reconnect=True)
            await asyncio.sleep(0.5)
            sink = RealtimeVADSink(
                self.bot.engine.process_audio_slice,
                on_speech_start_callback=self.bot.engine._handle_raw_speech_start,
                temperature_callback=self.bot.engine.conv_buffer.get_conversation_temperature,
                sink_error_callback=self.report_sink_error,
                suppress_wake_callback=lambda: self.stream_mode or self.radio_mode or self.is_playing_audio,
                wake_active_callback=lambda: self._wake_response_pending,
            )
            voice_client.listen(sink)
            patch_voice_recv_key_sync(voice_client, on_desync_storm=self._on_key_desync_storm)
            self.bot.engine.sink = sink
            self.connection_time = time.time()
            self.sink_failure_count = 0
            logger.warning("🔁 [AutoRejoin] 回台完成，恢復監聽（靜默、未打招呼）")
        except Exception as e:
            logger.warning(f"[AutoRejoin] 回台失敗（可手動 /summon）: {e}")

    @app_commands.command(name="summon", description="[Operation] 召喚馬文進入語音頻道監聽這無意義的世界")
    async def summon(self, interaction: discord.Interaction):
        await interaction.response.defer()
        
        # 1. 紀錄文字頻道
        if self.bot.engine.text_channel_callback:
            self.bot.engine.text_channel_callback(interaction.channel)
        
        if not interaction.user.voice:
            await interaction.followup.send("❌ 你必須先加入一個語音頻道！", ephemeral=True)
            return
            
        channel = interaction.user.voice.channel
        
        try:
            # 2. 斷開舊連線（若已連線則擋掉重複 summon）
            if interaction.guild.voice_client and interaction.guild.voice_client.is_connected():
                await interaction.followup.send("⚠️ 馬文已經在頻道裡了。", ephemeral=True)
                return
            elif interaction.guild.voice_client:
                # 殭屍連線（is_connected=False）：清掉後重連
                print(f"🔄 偵測到殭屍連線，正在清除...", flush=True)
                await interaction.guild.voice_client.disconnect(force=True)
                await asyncio.sleep(0.5)
                if self._mixer is not None:
                    self._mixer.clear_tts()

            # 3. 建立 DAVE 兼容連線
            print(f"嘗試載入 DAVE 監聽層，連線至: {channel.name}...", flush=True)
            self.bot.engine.start() # 🚀 [Watchdog Resurrection] 確保斷句看門狗在連線時是啟動的
            from discord_voice_engine import RealtimeVADSink, patch_voice_recv_key_sync

            # 🚀 [Parallel Warm-up] 在 UDP 握手等待期間同步預熱 LLM，讓 handle_summon 幾乎不用等
            _pre_members = [m.display_name for m in channel.members if not m.bot]
            _pre_active = self._room_active_now(_pre_members)
            self._pending_greeting_task = asyncio.create_task(
                self.bot.router.generate_greeting(_pre_members, active=_pre_active)
            )

            voice_client = await channel.connect(cls=voice_recv.VoiceRecvClient, timeout=60.0, reconnect=True)
            await asyncio.sleep(0.5)

            # 4. 掛載聽覺神經
            sink = RealtimeVADSink(
                self.bot.engine.process_audio_slice,
                on_speech_start_callback=self.bot.engine._handle_raw_speech_start,
                temperature_callback=self.bot.engine.conv_buffer.get_conversation_temperature,
                sink_error_callback=self.report_sink_error, # 💡 [Sentinel] 注入回報通道
                suppress_wake_callback=lambda: self.stream_mode or self.radio_mode or self.is_playing_audio,
                wake_active_callback=lambda: self._wake_response_pending
            )
            voice_client.listen(sink)
            patch_voice_recv_key_sync(voice_client, on_desync_storm=self._on_key_desync_storm)
            self.bot.engine.sink = sink # 🔗 [Linkage Fix]
            self.connection_time = time.time()  # 🛡️ [Operation Sentinel] 紀錄連線時間
            self.sink_failure_count = 0         # 重設失敗計數
            print("開始錄音 (voice_client.listen 已啟動，掛載動態 VAD)", flush=True)

            # 5. UDP Hole Punching (由後續音樂播放或 VoiceRecv 自動處理，避免衝突)
            # voice_client.play(self.SilenceSource(20))

            # 6. 觸發進場語音 (不阻塞 interaction)
            if self.bot.engine.post_summon_callback:
                asyncio.create_task(self.bot.engine.post_summon_callback(None))

            print(f"連線嘗試完畢！VoiceClient: connected={voice_client.is_connected()}", flush=True)
            await interaction.followup.send(f"🌑 馬文已降臨在 `{channel.name}`。")
            
        except discord.ClientException as e:
            print(f"❌ [SUMMON ClientException]\n{e}", flush=True)
            await interaction.followup.send(f"⚠️ 無法加入頻道：{str(e)}")
        except Exception as e:
            import traceback
            print(f"❌ [SUMMON ERROR]\n{traceback.format_exc()}", flush=True)
            retry_msg = await interaction.followup.send("⏳ 連線不穩，自動重試中，請稍候…", wait=True)
            await asyncio.sleep(2.0)
            try:
                print(f"🔄 [SUMMON Retry] 初次失敗，正在重試連線至 {channel.name}...", flush=True)
                voice_client = await channel.connect(cls=voice_recv.VoiceRecvClient, timeout=60.0, reconnect=True)
                from discord_voice_engine import RealtimeVADSink, patch_voice_recv_key_sync
                sink = RealtimeVADSink(
                    self.bot.engine.process_audio_slice,
                    on_speech_start_callback=self.bot.engine._handle_raw_speech_start,
                    temperature_callback=self.bot.engine.conv_buffer.get_conversation_temperature,
                    sink_error_callback=self.report_sink_error,
                    suppress_wake_callback=lambda: self.stream_mode or self.radio_mode or self.is_playing_audio,
                wake_active_callback=lambda: self._wake_response_pending
                )
                voice_client.listen(sink)
                patch_voice_recv_key_sync(voice_client)
                self.bot.engine.sink = sink
                self.connection_time = time.time()
                self.sink_failure_count = 0
                print(f"✅ [SUMMON Retry] 重試成功：connected={voice_client.is_connected()}", flush=True)
                await retry_msg.edit(content=f"✅ 已重新連線至 `{channel.name}`，馬文正在降臨…")
                if self.bot.engine.post_summon_callback:
                    asyncio.create_task(self.bot.engine.post_summon_callback(None))
            except Exception as retry_err:
                print(f"❌ [SUMMON Retry Failed] {retry_err}", flush=True)
                await retry_msg.edit(content=f"🚨 連線徹底失敗，請再試一次。（{retry_err}）")

    @app_commands.command(name="dismiss", description="[Operation] 讓馬文滾出語音頻道，停止 PCM 攔截")
    async def dismiss(self, interaction: discord.Interaction):
        await interaction.response.defer()
        if interaction.guild.voice_client:
            interaction.guild.voice_client.stop_listening()
            await interaction.guild.voice_client.disconnect()
            
            if self.bot.engine.dismiss_callback:
                await self.bot.engine.dismiss_callback()
                
            await interaction.followup.send("🛑 已中斷通訊並停止 PCM 攔截。")
        else:
            await interaction.followup.send("我不在任何語音頻道中。", ephemeral=True)

    def _room_active_now(self, members: list) -> bool:
        """進場當下判斷房間是否熱絡（人數 + 文字熱度代理），決定短/長招呼。

        Marvin 剛進場聽不到先前的語音對話，只能用現場人數與文字頻道熱度推估。
        """
        from gemini_router_content import room_is_active
        level = None
        tm = getattr(self, "temperature_monitor", None)
        if tm is not None:
            try:
                level = tm.level
            except Exception:
                level = None
        return room_is_active(len(members), level)

    @staticmethod
    def _daily_review_done_today(today: str, records_dir: str = "records") -> bool:
        """今天的 daily review 是否已跑（完成標記 quality_metrics_<today>.md 存在）。

        當天多次 summon / launchd 已跑過 → True，跳過重跑＝**防重複付費**（review 走付費
        Gemini，見 [[feedback_paid_calls_must_record]]）。
        """
        return os.path.exists(os.path.join(records_dir, f"quality_metrics_{today}.md"))

    async def _maybe_run_daily_review(self) -> None:
        """當天第一次 summon → 背景跑 daily review（不擋登場、成敗印 bot log）。

        取代脆弱的 launchd 日排程（07-06 後靜默停 fire）。once/day guard 防重複付費；
        用 bot 自己的 venv（sys.executable）跑 repo scripts，cwd=repo（bot 本就在 repo 根）
        → 付費記帳走 scripts 內 call_paid_review、寫同一份 records/llm_paid_usage.jsonl。
        """
        import datetime
        today = datetime.date.today().isoformat()
        if self._daily_review_done_today(today):
            return  # launchd 或先前 summon 已跑過，不重跑、不重複付費
        _scripts = [("scripts/analyze_daily_log.py", "daily_review"),
                    ("scripts/quality_metrics_report.py", "quality_metrics")]
        if datetime.date.today().weekday() == 0:   # 週一加 recall probe
            _scripts.append(("scripts/recall_probe.py", "recall_probe"))
        for _path, _name in _scripts:
            try:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, _path,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
                )
                _, _err = await proc.communicate()
                if proc.returncode == 0:
                    logger.info(f"✅ [DailyReview] {_name} 完成（summon 觸發）")
                else:
                    logger.warning(f"❌ [DailyReview] {_name} 失敗 rc={proc.returncode}: "
                                   f"{(_err or b'')[-300:].decode(errors='replace')}")
            except Exception as e:
                logger.warning(f"❌ [DailyReview] {_name} spawn 失敗: {e}")

    async def handle_summon(self, message: str = None):  # noqa: ARG002
        # 🚀 [Lifecycle Management] 啟動螢幕擷取 (視覺系統)
        if self.bot.vision_enabled and self.bot.screen_capture:
            print("👁️  啟動視覺系統擷取迴圈...", flush=True)
            asyncio.create_task(self.bot.screen_capture.start_capture_loop())

        # 🚀 [Bug Fix] 確保獲取正確的 VoiceClient
        vc = next((vc for vc in self.bot.voice_clients if vc.is_connected()), None)
        
        # 1. 🎵 [Operation Intro Theme] 優先播放進場音樂，用來遮掩 LLM 生成延遲
        # 💡 [Path Fix] 修正檔名大小寫 (Oh Marvin.mp3)
        intro_file = "assets/songs/Oh Marvin.mp3"
        if vc and os.path.exists(intro_file):
            print(f"🎸 [Intro] 偵測到進場音樂檔案: {intro_file}")
            before_opts = "-ss 00:01:32 -t 7"  # 1:32~1:39 約 7s
            if self._plan12:
                # 🎛️ [Plan 12] intro 進 mixer 音樂層（不 stop mixer、不烤 volume）；
                # 下方 greeting TTS 會自動 duck 它 → voice 清楚蓋在輕 intro 上（policy A）
                print("🎸 [Intro] Plan 12：intro → mixer 音樂層（greeting 將 duck）")
                src = discord.FFmpegPCMAudio(intro_file, before_options=before_opts, options="-vn")
                self._ensure_mixer_playing(DiscordPlaybackDevice(vc))
                self._mixer.set_volume(0.7)
                self._mixer.set_music_source(BufferedF32MusicSource(S16ToF32MusicSource(src), buffer_frames=50))
            else:
                # 🚀 [Race Condition Fix] 確保清除之前的沉默破門音源或殘留音訊
                if vc.is_playing():
                    vc.stop_playing()
                ffmpeg_opts = "-filter:a volume=0.7"
                print(f"🎸 [Intro] 優先啟動進場音樂 (音量 70%): {intro_file}")
                vc.play(discord.FFmpegPCMAudio(intro_file, before_options=before_opts, options=ffmpeg_opts))
        else:
            if not vc: print("⚠️ [Intro] 跳過音樂：找不到連線中的 VoiceClient。")
            if not os.path.exists(intro_file): print(f"⚠️ [Intro] 跳過音樂：找不到檔案 {intro_file}")
            
        # 2. 🌸 [Greeting] 呼叫 LLM 產出動態登場台詞 (Operation Narcissus v2: 群體黑歷史掃描)
        human_members = []
        if vc and vc.channel:
            human_members = [m.display_name for m in vc.channel.members if not m.bot]
            
        print(f"👁️ [Summon Scan] 偵測到現場人類成員: {human_members}")

        # 🚀 [Parallel Warm-up] 若 summon 時已預熱 LLM，直接拿結果（通常已完成，幾乎零等待）
        _task = self._pending_greeting_task
        self._pending_greeting_task = None
        _active = self._room_active_now(human_members)
        try:
            greeting = await _task if _task else await self.bot.router.generate_greeting(human_members, active=_active)
        except Exception:
            greeting = await self.bot.router.generate_greeting(human_members, active=_active)
        
        if self.active_text_channel:
            await self.active_text_channel.send(f"⚙️ **【馬文 降臨】**\n{greeting}")
        self.stt_logger.info(f"[BOT降臨] {greeting}")

        # 3. 播放語音
        # 登場台詞是一次性宣告，不應被進場音樂播放期間的人聲觸發的 interrupt guard 阻擋
        self._tts_interrupted = False
        self._tts_protected = True
        await self.play_tts(greeting, already_in_channel=True)
        self._tts_protected = False
        
        # 3.（原喚醒詞宣導 marvin_wakeword_short.mp3）2026-06-03 依用戶要求移除：登場只保留
        # 音樂 + 打招呼兩段，第三段語音包不再播放。

        sink = self.bot.engine.get_active_sink()
        if sink:
            sink.last_audio_packet_time = time.time()

        self.idle_streak = 0

        # 📊 當天第一次 summon → 背景跑 daily review（取代脆弱 launchd 排程；once/day guard
        # 防重複付費；不擋登場）。launchd 12:05 仍留當備援，guard 保證一天最多一次。
        asyncio.create_task(self._maybe_run_daily_review())

    async def handle_dismiss(self):
        print("🛑 [系統指令] 執行 /dismiss 撤離程序。")

        # 📻 [Marvin Radio] 解散時一併停止電台
        if self.radio_mode:
            await self.stop_radio(reason="系統解散")
        # 🎵 解散時一併停止串流（含清掉個人歌單 session），否則 stream loop 會在無語音下
        # 一直 churn（2026-06-29 個人歌單死鎖事故的相鄰根因：dismiss 沒停 stream）。
        try:
            await self.stop_stream(reason="系統解散")
        except Exception as e:
            print(f"⚠️ [Dismiss] stop_stream 失敗: {e}")
        for vc in self.bot.voice_clients:
            try:
                if vc.is_connected():
                    if hasattr(vc, 'stop_listening'):
                        vc.stop_listening()
                    await vc.disconnect(force=True)
            except Exception as e:
                print(f"⚠️ [Shutdown Warning] {e}")

        active_speakers = set(entry.get("speaker") for entry in self.log_buffer if entry.get("speaker"))
        for speaker in active_speakers:
            asyncio.create_task(self.bot.router.audit_player_memory(speaker))

        self.stt_logger.info(
            f"[系統撤離] 馬文離開語音頻道 | 本次對話成員={list(active_speakers) or '無'}"
        )
        self.active_text_channel = None
        self.log_buffer = []
        self.idle_streak = 0
        self.speech_buffers = {}
        for speaker, timer in self.speech_timers.items():
            timer.cancel()
        self.speech_timers = {}

        await self.bot.engine.clear_buffers()

        # 🚀 [Lifecycle Management] 停止螢幕擷取
        if self.bot.screen_capture:
            print("🛑 [Lifecycle] 停止視覺系統擷取迴圈...", flush=True)
            self.bot.screen_capture.stop()

    @tasks.loop(seconds=60.0)
    async def sentinel_monitor_loop(self):
        """🛡️ [Operation Sentinel] 強化型語音監控：具備 30s 寬限期與自癒功能"""
        if self.is_recovering: return # 🚀 [Sentinel 強化] 修復中跳過主迴圈
        if not self.bot.voice_clients: return
        vc = self.bot.voice_clients[0]
        if not vc.is_connected():
            # VoiceClient exists but WebSocket is dead → trigger soft repair
            if time.time() - self.connection_time > 30:
                logger.warning("📡 [Sentinel] VoiceClient.is_connected() = False，觸發軟修復...")
                asyncio.create_task(self.soft_repair_connection(reason="VoiceClient WebSocket 斷線"))
            return

        # 🎛️ [Plan 12] on-demand：只在「有內容但沒在播」（如重連後）才 re-arm；idle 不 arm（不送音）
        if self._plan12 and self._mixer is not None and not self._mixer.is_idle():
            self._ensure_mixer_playing(DiscordPlaybackDevice(vc))

        # 1. 寬限期檢查 (Grace Period)：連線後的 30 秒內不進行嚴格監控
        if time.time() - self.connection_time < 30:
            return

        # 🚀 [Sentinel 強化] 若連線穩定超過 120 秒，則重設軟修復計數，回歸正常運算
        if time.time() - self.connection_time > 120:
            self.soft_repair_count = 0

        active_humans = [m for m in vc.channel.members if not m.bot and m.voice and not m.voice.self_mute]
        if not active_humans: return
        
        sink = self.bot.engine.get_active_sink()
        if not sink:
            self.sink_missing_count += 1  # 🚀 [T-01 Fix] 使用獨立計數器，與 DAVE 錯誤互不干擾
            logger.warning(f"📡 [Sentinel] 偵測到 Sink 缺失 (Count: {self.sink_missing_count})，嘗試自癒程序...")
            
            # 2. 自癒程序 (Auto-Repair)：嘗試重新掛載聽覺神經
            try:
                # 🚀 [Sentinel 2.0] 強制清理舊的 Reader 狀態，避免 "Already receiving audio" 衝突
                if hasattr(vc, 'stop_listening'):
                    logger.info("🧹 [Sentinel] 執行強制重置：停止舊的監聽程序...")
                    vc.stop_listening()
                
                await asyncio.sleep(0.5) # 給予底層緒些微時間清理
                
                from discord_voice_engine import RealtimeVADSink, patch_voice_recv_key_sync
                new_sink = RealtimeVADSink(
                    self.bot.engine.process_audio_slice,
                    on_speech_start_callback=self.bot.engine._handle_raw_speech_start,
                    temperature_callback=self.bot.engine.conv_buffer.get_conversation_temperature,
                    sink_error_callback=self.report_sink_error # 💡 [Fix] 補上缺失的回傳通道
                )
                vc.listen(new_sink)
                patch_voice_recv_key_sync(vc)
                self.bot.engine.sink = new_sink # 🔗 [Linkage Fix]
                logger.info("✅ [Sentinel] 自癒成功：已重新掛載 RealtimeVADSink。")
                return # 給予一分鐘時間觀察，不觸發重啟
            except Exception as repair_err:
                logger.error(f"❌ [Sentinel] 自癒失敗: {repair_err}")

            # 3. 升級處置：若連續兩次 (約 2 分鐘) 偵測不到且修補失敗，才執行重啟
            if self.sink_missing_count >= 2:  # 🚀 [T-01 Fix]
                await self.self_restart(reason="語音連線異常 (No Sink Context after repair attempt)")
            return
            
        # 正常狀態下重設 Sink 缺失計數
        self.sink_missing_count = 0  # 🚀 [T-01 Fix]
        
        # 🎵 [Active Playback Skip] Marvin 正在輸出音訊（TTS / 音樂 / 串流）時，
        # 使用者本來就該安靜聽。Marvin 還能 play() 代表 voice connection 健康，
        # 不該因「沒有解密音訊進來」誤判 DAVE 失效而 disconnect 中斷播放。
        if self.is_playing_audio or self.stream_mode or vc.is_playing():
            return

        # 4. 偵測靜音 (Silence Detection)
        # 🛡️ [Sentinel 2.0] 區分網路斷線與解密失敗，優先讀取解密成功的心跳
        last_audio = getattr(sink, 'last_decrypted_audio_time', sink.last_audio_packet_time)
        silence_duration = time.time() - last_audio

        # 📻 [Radio Mode] 若正在播放廣播，提高閾值至 12 分鐘 (720s)，因為玩家可能只是在聽
        # 一般模式則維持 5 分鐘 (300s)
        threshold = 720.0 if self.radio_mode else 300.0
        
        if silence_duration > threshold:
            # 🚀 [Sentinel Strategy] 先嘗試軟修復，失敗多次才物理重啟
            if self.soft_repair_count < 2:
                logger.warning(f"📡 [Sentinel] 偵測到持續 {int(silence_duration)}s 無感測音訊，啟動預防性軟修復...")
                self.soft_repair_count += 1
                await self.soft_repair_connection(reason=f"持續 {int(silence_duration/60)} 分鐘無解密音訊")
            else:
                logger.critical(f"🚨 [Sentinel] 軟修復多次無效，執行物理重啟...")
                await self.self_restart(reason=f"軟修復失效，持續性語音接收斷開 ({int(silence_duration/60)} 分鐘)")

    async def self_restart(self, reason: str = "未知原因", force: bool = False, pull: bool = True):
        """物理重啟流程。

        關鍵不變式：**無論 pre-execv 任何步驟失敗，必須走到 os.execv**。
        以前 memory.flush() 在 SQLite 重構過渡期會噴 AttributeError，
        導致 /marvin_reboot 卡死沒重啟（log 留下 "已執行重啟" 但其實沒有）。
        現在所有 pre-execv 步驟都被 try/except 包住。

        重啟完成回報：寫狀態檔（.marvin_reboot_state.json）到 cwd，
        新進程 on_ready 讀取後貼完成訊息到原頻道並刪檔。
        """
        if not force and (time.time() - getattr(self.bot, "last_restart_time", 0) < 900): return

        logger.critical(f"🚀 [Restart] 正在執行進程級重啟，原因：{reason}")
        if self.active_text_channel:
            try: await self.active_text_channel.send(f"⚠️ **【系統診斷：聽覺異常】**\n軟修復失效，正在執行物理重啟 ({reason}) 以重新同步金鑰。")
            except: pass

        # 1. 原子性數據保護：強制存入記憶
        # SQLite per-mutation 已自動 commit；flush() 是 API 相容用的 no-op。
        # 包 try/except 是為了任何 MemoryManager 過渡版本（含 deprecated method）也不卡 restart。
        try:
            logger.info("💾 [Restart] 正在執行最後的記憶存檔...")
            self.bot.router.memory.flush()
        except Exception as e:
            logger.error(f"❌ [Restart] memory.flush() 失敗（不阻斷重啟流程）: {e}")

        # 2. git pull 拿最新 code（pull=False 可關閉，例如 dev 階段不想動 working tree）
        commit_before = _git_head_short()
        commit_after = commit_before
        pull_summary = "(skipped)"
        if pull:
            try:
                logger.info("📥 [Restart] 正在 git pull 拿最新 code...")
                proc = await asyncio.create_subprocess_exec(
                    "git", "pull", "--ff-only", "origin",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15.0)
                out = stdout.decode("utf-8", errors="replace").strip()
                logger.info(f"📥 [Restart] git pull 結果（rc={proc.returncode}）:\n{out}")
                pull_summary = f"rc={proc.returncode}\n{out[:1200]}"
                commit_after = _git_head_short()
                if self.active_text_channel:
                    try:
                        await self.active_text_channel.send(
                            f"📥 git pull (rc={proc.returncode}):\n```\n{out[:1500]}\n```"
                        )
                    except Exception:
                        pass
            except asyncio.TimeoutError:
                logger.error("❌ [Restart] git pull 超時 15s（不阻斷重啟）")
                pull_summary = "(timeout 15s)"
            except Exception as e:
                logger.error(f"❌ [Restart] git pull 失敗（不阻斷重啟）: {e}")
                pull_summary = f"(error: {type(e).__name__}: {e})"

        # 3. 寫狀態檔，供新進程 on_ready 讀取後貼完成訊息
        _write_reboot_state({
            "channel_id": self.active_text_channel.id if self.active_text_channel else None,
            "guild_id": self.active_text_channel.guild.id if self.active_text_channel and self.active_text_channel.guild else None,
            "reason": reason,
            "commit_before": commit_before,
            "commit_after": commit_after,
            "pull_summary": pull_summary,
            "started_at": time.time(),
        })

        # 4. 釋放資源與關閉連線（避免幽靈機器人殘留）
        try:
            logger.info("🔌 [Restart] 正在切斷 Discord 連線...")
            await self.bot.close()
        except Exception as e:
            logger.error(f"❌ [Restart] 關閉連線時發生異常（忽略並啟動 execv）: {e}")

        # 5. 物理進程替換（最後一道，沒退路）
        try:
            logger.critical("☢️ [Restart] 執行 os.execv，程序替換中...")
            args = sys.argv[:]
            os.execv(sys.executable, [sys.executable] + args)
        except Exception as e:
            # execv 不該失敗，若真失敗 bot 會死；至少留下 log 線索
            logger.critical(f"☢️ [Restart] os.execv 失敗！bot 將終結: {e}")
            raise

    def start_local_listening(self) -> None:
        """本機模式輸入接縫：/summon 的本機對應（無 Discord voice channel）。

        Discord 路徑完全不受影響——只在主動呼叫此 method 時才切換 local 模式。
        """
        from marvin_voice_core.local_mic_sink import LocalMicSink
        from marvin_voice_core.playback_device import LocalSpeakerDevice

        # 1. 起 VAD watchdog（idempotent，對齊 summon 第一步）
        self.bot.engine.start()

        # 2. 建 LocalMicSink，綁 pipeline 入口（對齊 summon 的 process_audio_slice）
        sink = LocalMicSink(
            self.bot.engine.process_audio_slice,
            loop=self.bot.loop,
            on_speech_start_callback=self.bot.engine._handle_raw_speech_start,
        )
        # 韻律活化：把共享 VoiceMetaAnalyzer 掛上 device sink，讓 add_rms 真的餵到。
        sink.meta_analyzer = self.bot.engine.meta_analyzer
        self.bot.engine.sink = sink

        # 3. 設 local 模式旗標
        self._local_mode = True
        # 親密模式旗標（裝置端專屬，Discord 路徑永不設）
        self._intimate_mode = os.getenv("MARVIN_INTIMATE_MODE", "").strip().lower() in ("1", "true", "yes", "on")

        # 3b. 放寬 late-skip：免費 LLM 免不了限流(429 backoff 數十秒)，本機單人用不怕
        # 慢回應蓋掉新對話，故拉高門檻讓慢到的回應仍出聲(僅本實例，不碰生產的 25s)。
        self._LATE_RESPONSE_SKIP_SEC = 120.0
        self._LATENCY_DOMINATED_THRESHOLD = 120.0

        # 4. 建並掛載本地喇叭裝置（輸出接縫已由 ④-1 的 _resolve_playback_device 處理）
        self.set_local_speaker(LocalSpeakerDevice())

        # 5. 換成 always-allow consent stub（local 模式無 Discord 同意流程）
        self.consent = _LocalConsentStub()

        # 6. 非阻塞啟動麥克風擷取（對齊 sink.write 用 loop.create_task 的規範）
        self.bot.loop.create_task(sink.start())

    def start_browser_satellite_listening(self, browser_output) -> None:
        """純軟體 satellite 輸出接縫：mixer 泵 → BrowserSpeakerOutput → 瀏覽器 WebAudio。

        與 start_satellite_listening 差異＝無 Pi/wyoming mic 橋、無 Mac mic sink：輸入唯一
        來源是 main_satellite 的 POST /audio（inject_audio→handle_stt_result）。輸出改注入
        BrowserSpeakerOutput（GET /reply 服務給瀏覽器）。Discord / Pi 路徑完全不受影響。
        """
        from marvin_voice_core.playback_device import LocalSpeakerDevice

        # 1. 起 VAD watchdog（idempotent；emit/timing 用）
        self.bot.engine.start()

        # 2. local 模式旗標（共用 _resolve_playback_device 輸出接縫）
        self._local_mode = True
        self._intimate_mode = os.getenv("MARVIN_INTIMATE_MODE", "").strip().lower() in ("1", "true", "yes", "on")
        if self._mixer is not None:
            self._mixer._tts_gain = float(os.getenv("MARVIN_TTS_GAIN", "0.9"))

        # 3. 放寬 late-skip（免費 LLM 限流下慢回應仍出聲；單人用不怕蓋）
        self._LATE_RESPONSE_SKIP_SEC = 120.0
        self._LATENCY_DOMINATED_THRESHOLD = 120.0

        # 4. 喇叭輸出接縫：mixer 泵 → BrowserSpeakerOutput（靜音切段快取，/reply 服務）
        self.set_local_speaker(LocalSpeakerDevice(output=browser_output))

        # 5. always-allow consent stub（單人用，無 Discord 同意流程）
        self.consent = _LocalConsentStub()

    def _on_satellite_wake(self, name: str) -> None:
        """衛星（Pi openwakeword）喚醒候選 → duck 音樂即時回饋。

        尊重 MARVIN_WAKE_DUCK kill-switch（與 Discord/本機 duck 同一開關）。
        播純音樂時走 Music Echo Guard 忽略此候選（無硬體 AEC＝很可能是喇叭回聲）。"""
        if music_echo_guard_active(
                getattr(self, "_local_mode", False), getattr(self, "is_playing_audio", False),
                getattr(self, "_current_tts_text", ""),
                os.getenv("MARVIN_MUSIC_ECHO_GUARD", "1") != "0"):
            logger.info("🔇 [Music Echo Guard] 播音樂中忽略衛星喚醒候選 name=%s（無硬體 AEC＝可能為喇叭回聲）", name)
            return
        if getattr(self, "_mixer", None) and os.getenv("MARVIN_WAKE_DUCK", "1") != "0":
            self._mixer.duck_for_wake()

    def start_satellite_listening(self) -> None:
        """衛星模式輸入接縫：Pi wyoming-satellite → 現有 pipeline（實體音箱 S4）。

        對齊 start_local_listening；差異＝mic 來源是 WyomingSatelliteBridge（TCP 收 Pi
        麥、走同一 VAD/切句/STT），喇叭輸出注入 WyomingSpeakerOutput（音訊回送 Pi 播放），
        喚醒在 Pi 本地 openwakeword→送 Detection→duck。Discord 路徑完全不受影響。
        """
        from marvin_voice_core.playback_device import LocalSpeakerDevice
        from marvin_voice_core.wyoming_bridge import WyomingSatelliteBridge
        from marvin_voice_core.wyoming_speaker_output import WyomingSpeakerOutput

        # 1. 起 VAD watchdog（idempotent，對齊 summon 第一步）
        self.bot.engine.start()

        # 2. 建衛星橋，綁 pipeline 入口 + 喚醒 duck hook
        bridge = WyomingSatelliteBridge(
            self.bot.engine.process_audio_slice,
            host=os.getenv("MARVIN_SATELLITE_HOST", "marvinpi.local"),
            user_id="satellite",
            on_detection=self._on_satellite_wake,
            on_speech_start_callback=self.bot.engine._handle_raw_speech_start,
            loop=self.bot.loop,
        )
        self._satellite_bridge = bridge
        # 韻律活化：衛星路徑，把共享 VoiceMetaAnalyzer 掛上橋內部 LocalMicSink。
        bridge.sink.meta_analyzer = self.bot.engine.meta_analyzer
        # Sentinel 心跳監控的是橋內部那顆 LocalMicSink（與本機模式同型）
        self.bot.engine.sink = bridge.sink

        # 3. 設 local 模式旗標（衛星共用 local 輸出接縫 _resolve_playback_device）
        self._local_mode = True
        # 親密模式旗標（對齊 start_local_listening；Discord 路徑永不設）
        self._intimate_mode = os.getenv("MARVIN_INTIMATE_MODE", "").strip().lower() in ("1", "true", "yes", "on")
        # device TTS 音量：mixer 預設 tts_gain=0.5（-6dB，為「TTS 疊音樂上不過大」而設）；device 上
        # TTS 常單獨播＋音樂有 loudnorm 拉滿→ack 相對太小。調高（f32 域、有 headroom、不後級 clip）。
        # env MARVIN_TTS_GAIN 可覆蓋；只 device（satellite）路徑，Discord 不受影響。
        if self._mixer is not None:
            self._mixer._tts_gain = float(os.getenv("MARVIN_TTS_GAIN", "0.9"))

        # 3b. 放寬 late-skip（對齊 start_local_listening：免費 LLM 限流下慢回應仍出聲）
        self._LATE_RESPONSE_SKIP_SEC = 120.0
        self._LATENCY_DOMINATED_THRESHOLD = 120.0

        # 4. 喇叭輸出接縫：mixer 泵 → WyomingSpeakerOutput → 衛星喇叭
        self.set_local_speaker(
            LocalSpeakerDevice(output=WyomingSpeakerOutput(bridge, self.bot.loop)))

        # 5. always-allow consent stub（衛星單人用，無 Discord 同意流程）
        self.consent = _LocalConsentStub()

        # 6. 非阻塞啟動重連迴圈（衛星斷線/重啟 → 5s 後重連，不炸腦＝優雅降級）
        async def _bridge_forever():
            while True:
                try:
                    await bridge.run()
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"🛰️ [Satellite] bridge error: {e}")
                await asyncio.sleep(5)

        self.bot.loop.create_task(_bridge_forever())


class _LocalConsentStub:
    """Local 模式 consent 放行 stub：一律允許，覆蓋完整 ConsentManager 介面。"""

    def is_consented(self, display_name: str) -> bool:
        return True

    def has_seen_notice(self, display_name: str) -> bool:
        return True

    def mark_seen(self, display_name: str) -> None:
        pass

    def set_consent(self, display_name: str, granted: bool) -> None:
        pass

