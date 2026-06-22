"""
SystemLoopsMixin — VoiceController 的週期性系統維護迴圈。

從 voice_controller.py 抽出（減肥），以 mixin 併入 VoiceController。self 仍是
VoiceController 實例；三個迴圈經 cog_load 的 self.X.start() 由 MRO 正常啟動。
呼叫的 self._send_social_intervention_visual / _schedule_reaction_check 留在 VC，
經 self 取用，行為零改動。

  - slow_system_loop（10 分鐘：氣氛/狀態維護）
  - daily_log_export_loop（每日 12:00 +08：匯出日誌）
  - reset_stt_counter_loop（每分鐘：重設 STT 計數）
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import time

import discord
from discord.ext import tasks

import ack_templates
from speak_bus import SpeakBus
from proactive_topic_agent import ProactiveTopicAgent

logger = logging.getLogger(__name__)


class SystemLoopsMixin:
    @tasks.loop(minutes=10.0)
    async def slow_system_loop(self):
        """[Slow System] 每 10 分鐘進行一次對話彙整與馬文評論"""
        try:
            if not self.bot.engine.conv_buffer:
                return
                
            # 1. 取得最近的增量對話紀錄
            # 🧬 [Incremental Fix] 由 Buffer 內部指標控制，杜絕跨任務重複性
            new_entries = self.bot.engine.conv_buffer.pop_new_entries()
            self.slow_loop_accumulator.extend(new_entries)
            
            # 🧬 [APM Economy] 判定是否具備足夠內容 (100字) 以觸發日記生成
            total_chars = sum(len(e.get("text", "")) for e in self.slow_loop_accumulator)
            
            if not self.slow_loop_accumulator or total_chars < 200:
                if not self.slow_loop_accumulator:
                    print("📭 [SlowLoop] 本輪無新對話，跳過摘要。", flush=True)
                else:
                    print(f"⏳ [SlowLoop] 內容不足 ({total_chars}/200 字)，繼續累積...", flush=True)
                
                # 🚀 [Proactive Social] 就算沒有對話，也檢查是否靜默過久 (Operation Social Gap)
                now = time.time()
                silence = now - self.last_player_speech_time

                # 📓 [DiaryComic] 靜默 ≥5 分鐘 = 對話場次收尾 → 把剛結束那段畫成漫畫貼回日記頻道
                # 聊天進行中不出（沒人看、浪費 API）；同場次只出一次（poster 內部去重）；全防禦
                if silence > 300 and not self.stream_mode:
                    try:
                        from diary_comic_poster import maybe_post_comic
                        await maybe_post_comic(self.bot, self.active_text_channel)
                    except Exception as _ce:
                        logger.warning(f"⚠️ [DiaryComic] hook 失敗（已吞）: {_ce}")

                # 📻 [Marvin Radio] 10 分鐘靜默自動啟動電台（stream_mode 播放中則跳過）
                if silence > 600 and not self.radio_mode and not self.stream_mode and self.bot.voice_clients:
                    print("🕒 [Slow System] 偵測到 10 分鐘靜默，自動啟動馬文電台...")
                    if self.active_text_channel:
                        await self.active_text_channel.send(
                            "📻 **【馬文電台：自動啟動】**\n十分鐘了... 你們都死了嗎。既然沒人說話，就讓我播點音樂填補這毫無意義的寂靜吧。"
                        )
                    await self.start_radio(trigger="10分鐘靜默自動")

                elif not self.radio_mode and now - self.last_proactive_time > 1800:
                    # 🔇 [Freq Adj Op 32] 依 24h 內嚴重率動態更新主動發話閾值
                    _feedback_path = os.path.normpath(
                        os.path.join(os.path.dirname(__file__), "..", "records", "response_feedback.jsonl")
                    )
                    try:
                        _cutoff = now - 86400  # 24h
                        _rows = []
                        if os.path.exists(_feedback_path):
                            with open(_feedback_path, "r", encoding="utf-8") as _f:
                                _lines = _f.readlines()[-20:]
                            import json as _json
                            for _line in _lines:
                                try:
                                    _row = _json.loads(_line)
                                    if _row.get("timestamp", 0) >= _cutoff:
                                        _rows.append(_row)
                                except Exception:
                                    pass
                        if len(_rows) >= 5:
                            _severe = sum(1 for r in _rows if r.get("reaction") == "嚴重")
                            _ratio = _severe / len(_rows)
                            # P0: 整體降一個量級（北極星 = 讓 bot 真的有機會發聲）
                            # 用戶嫌 bot 太吵時 ("嚴重" 比例 >30%) 才回升，正常情況 90s 就觸發
                            if _ratio > 0.30:
                                self.proactive_silence_threshold = 240
                            elif _ratio == 0.0:
                                self.proactive_silence_threshold = 90
                            else:
                                self.proactive_silence_threshold = 120
                            print(f"🔇 [Freq Adj] 嚴重={_ratio:.0%} ({len(_rows)}行/24h), proactive_silence_threshold={self.proactive_silence_threshold}s")
                    except Exception as _fe:
                        logger.warning(f"⚠️ [Freq Adj] 讀取 feedback 失敗: {_fe}")

                    # 🚀 [Proactive Social] 靜默主動發起話題
                    # 2026-05-26: 已遷至 SpeakBus（ProactiveTopicAgent），由 5s tick 統一 dispatch
                    # 保留 proactive_silence_threshold 動態調整（上面 _ratio 那段），agent 讀同一個值
                return
            
            # 使用最新條目的時間作為快照參考
            self.last_snapshot_time = max(e["timestamp"] for e in self.slow_loop_accumulator)
            print(f"🕒 [Slow System] 執行增量總結 (累積筆數: {len(self.slow_loop_accumulator)}, 總字數: {total_chars})...")

            # 遊戲期間不貼日記，避免打斷遊戲流程；留住累積器內容，等遊戲結束後繼續
            if self.bot.router.current_game:
                print(f"🎮 [SlowLoop] 遊戲進行中 ({self.bot.router.current_game})，跳過日記生成。")
                return

            # 將累積的內容取出進行處理，並清空累積器
            processing_entries = self.slow_loop_accumulator
            self.slow_loop_accumulator = []

            # 過濾馬文自己的回應：避免把 TTS 輸出當成對話內容餵回 diary
            human_entries = [e for e in processing_entries if e.get("speaker", "") != "Marvin"]
            if not human_entries:
                print("📭 [SlowLoop] 本輪只有馬文自言自語，跳過日記生成。")
                return

            # 2. 並行備料
            full_new_text = "\n".join([f"{e.get('speaker', '未知')}: {e.get('text', '...')}" for e in new_entries])
            online_members = self.get_online_members()
            can_analyze = self._stt_call_counter <= 10
            if not can_analyze:
                print(f"⚠️ [Slow System] STT 頻率過高 ({self._stt_call_counter}/min)，跳過本輪社交分析。")

            # 🔇 [社交補位 OFF — 2026-06-03] analyze_social_dynamics（社交知識圖譜，長上下文）
            # 的結果 analysis 唯一消費者就是下方社交補位；補位關閉時算了也直接丟掉 = 純浪費。
            # → 補位關閉就連這支 LLM 都不呼叫，省免費池。重啟：_SOCIAL_INTERVENTION_ENABLED=True，
            # call 與消費者一起復活。（記憶萃取早已改每日 off-peak，見下方 gather 註解。）
            _SOCIAL_INTERVENTION_ENABLED = False
            _do_social_analysis = can_analyze and _SOCIAL_INTERVENTION_ENABLED

            async def _noop(): return None

            # 3. 並行執行：日記 + 社交分析（記憶萃取改由每日 web LLM 整體處理）
            results = await asyncio.gather(
                self.bot.router.generate_slow_summary(human_entries),
                self.bot.router.analyze_social_dynamics(new_entries, full_new_text, online_members=online_members) if _do_social_analysis else _noop(),
                return_exceptions=True
            )
            summary  = results[0] if not isinstance(results[0], BaseException) else None
            analysis = results[1] if can_analyze and not isinstance(results[1], BaseException) else None

            # 4. 寫入本地日誌 (RAG 來源)
            def _write_rag_log(text):
                os.makedirs("records", exist_ok=True)
                with open("records/chat_summary_log.txt", "a", encoding="utf-8") as f:
                    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    f.write(f"[{ts}] --- 10分鐘對話總結 ---\n{text}\n\n")

            if summary is None:
                print("📭 [SlowLoop] LLM 判斷本輪內容不值得記錄，跳過發文。")
                await asyncio.to_thread(_write_rag_log, "[SKIPPED - 內容無新意]")
            else:
                await asyncio.to_thread(_write_rag_log, summary)

                # 5. 發送到專屬頻道 (#馬文的厭世日記)
                diary_channel = None
                if self.active_text_channel and self.active_text_channel.guild:
                    guild = self.active_text_channel.guild
                    diary_channel = discord.utils.get(guild.text_channels, name="馬文的厭世日記")
                    if not diary_channel:
                        diary_channel = discord.utils.get(guild.text_channels, name="marvin-diary")

                target = diary_channel
                if not target and self.active_text_channel and self.active_text_channel.guild:
                    try:
                        guild = self.active_text_channel.guild
                        print(f"🛠️ [Slow System] 嘗試為伺服器 '{guild.name}' 建立專屬日記頻道...")
                        target = await guild.create_text_channel(
                            name="馬文的厭世日記",
                            topic="Ambient Presence: 馬文在這裡默默鄙視所有人。",
                            reason="馬文的厭世日記系統啟動"
                        )
                    except Exception as e:
                        print(f"❌ [Slow System] 建立頻道失敗: {e}")
                        target = self.active_text_channel

                if target:
                    if self.pending_intervention:
                        unplayed_text = self.pending_intervention.get("text", "")
                        summary += f"\n\n*[未放送的內心獨白：{unplayed_text}]* (環境參數：Confidence={self.current_confidence}, VAD={self.current_vad_delay}s)"
                        old_path = self.pending_intervention.get("file_path")
                        if old_path and os.path.exists(old_path):
                            try: os.remove(old_path)
                            except: pass
                        self.pending_intervention = None

                    await target.send(f"📓 **【馬文的厭世日記】** (10min 增量彙整)\n\n{summary}")

            # 6. 處理社交缺口（使用並行取回的 analysis 結果）
            # 🔇 [社交補位 OFF — 2026-06-03] flag 與 analyze_social_dynamics call gate 都在上方
            #    （補位關閉時連社交分析 LLM 都不算，不空轉）。觸發源 Marvin Autonomous
            #    Intelligence v2.5；補位接話率僅 ~4%（records/speak_outcomes.jsonl）。
            if _SOCIAL_INTERVENTION_ENABLED and analysis:
                gap_type = analysis.get("social_gap", "none")
                if gap_type != "none":
                    # 用 suki_inner_monologue（已蒸餾的場景觀察）取代原始對話紀錄，避免 LLM 複述聊天內容
                    gap_context = analysis.get("suki_inner_monologue") or full_new_text
                    gap_response = await self.bot.router.generate_gap_filling_response(gap_type, gap_context)
                    if gap_response and self.active_text_channel:
                        print(f"🤫 [Social Awareness] 執行社交補位 ({gap_type})。")
                        await self._send_social_intervention_visual(gap_type, gap_response, gap_context)
                        self.stt_logger.info(f"[BOT慢循環補位] 類型={gap_type} | {gap_response[:120]}")
                        _last_spk = human_entries[-1]["speaker"] if human_entries else "頻道"
                        asyncio.create_task(self._schedule_reaction_check(
                            _last_spk, gap_response, time.time(),
                            wake_latency=None, atmosphere=None,
                        ))
                        # emotional_support = 抱怨共鳴，只發文字不打斷語音
                        if gap_type != "emotional_support":
                            asyncio.create_task(self.play_tts(gap_response, already_in_channel=True, silent_during_stream=True, priority=2))
        except Exception as e:
            logger.error(f"🚨 [Slow System Error] 背景循環發生異常 (已截斷防止崩潰): {e}")
            import traceback
            logger.error(traceback.format_exc())

    @tasks.loop(time=datetime.time(hour=12, minute=0, tzinfo=datetime.timezone(datetime.timedelta(hours=8))))
    async def daily_log_export_loop(self):
        """每天中午 12:00 (UTC+8) 將前一天的 STT log 與 feedback 另存為 records/daily/YYYY-MM-DD.log"""
        try:
            tz = datetime.timezone(datetime.timedelta(hours=8))
            now = datetime.datetime.now(tz)
            today_noon = now.replace(hour=12, minute=0, second=0, microsecond=0)
            yesterday_noon = today_noon - datetime.timedelta(days=1)
            date_label = today_noon.strftime("%Y-%m-%d")

            os.makedirs("records/daily", exist_ok=True)
            out_path = f"records/daily/{date_label}.log"

            lines = []

            # --- A. STT History ---
            lines.append(f"=== STT LOG ({yesterday_noon.strftime('%Y-%m-%d %H:%M')} ~ {today_noon.strftime('%Y-%m-%d %H:%M')}) ===\n")
            try:
                with open("stt_history.log", "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.rstrip("\n")
                        try:
                            # 格式: 2026-04-23 23:21:23,281 - [玩家] ...
                            dt_str = line[:23]
                            dt = datetime.datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=tz)
                            if yesterday_noon <= dt < today_noon:
                                lines.append(line + "\n")
                        except (ValueError, IndexError):
                            pass
            except FileNotFoundError:
                lines.append("(stt_history.log 不存在)\n")

            # --- B. Response Feedback ---
            lines.append(f"\n=== RESPONSE FEEDBACK ({date_label}) ===\n")
            feedback_count = 0
            try:
                with open("records/response_feedback.jsonl", "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                            ts = float(entry.get("timestamp", 0) or 0)
                            dt = datetime.datetime.fromtimestamp(ts, tz=tz)
                            if yesterday_noon.timestamp() <= ts < today_noon.timestamp():
                                lines.append(line + "\n")
                                feedback_count += 1
                        except (json.JSONDecodeError, ValueError):
                            pass
            except FileNotFoundError:
                lines.append("(response_feedback.jsonl 尚未建立)\n")

            content = "".join(lines)
            await asyncio.to_thread(lambda: open(out_path, "w", encoding="utf-8").write(content))
            logger.info(f"📋 [Daily Export] 已輸出 {out_path} ({len(lines)} 行 STT, {feedback_count} 筆 feedback)")

        except Exception as e:
            logger.error(f"❌ [Daily Export] 每日匯出失敗: {e}", exc_info=True)

    @tasks.loop(time=datetime.time(hour=13, minute=45, tzinfo=datetime.timezone(datetime.timedelta(hours=8))))
    async def daily_watchdog_loop(self):
        """每天 13:45 (UTC+8) 檢查每日 cron 任務健康 → 貼 Discord 心跳/告警。

        跑在 bot 內（不另開 launchd 看 launchd）；bot 死了你本來就會發現。
        正向心跳：沒問題也報「✅ 全健康」→ 完全安靜 = 連這個都掛了，silence 即警報。
        """
        try:
            try:
                from scripts.cron_watchdog import check_cron_health, CHECKS
            except Exception:
                from cron_watchdog import check_cron_health, CHECKS
            problems = check_cron_health(CHECKS, now_ts=time.time())
            if problems:
                body = "🚨 **每日任務看門狗** 發現問題：\n" + "\n".join(f"• {p}" for p in problems)
            else:
                body = "✅ **每日任務看門狗**：全健康（心跳）"
            ch = self._find_ops_channel()
            if ch:
                await ch.send(body)
            else:
                logger.warning(f"[Watchdog] 無頻道可貼：{body}")
        except Exception as e:
            logger.error(f"❌ [Watchdog] 看門狗失敗: {e}", exc_info=True)

    def _find_ops_channel(self):
        """貼看門狗的頻道：馬文系統狀態 > 馬文的厭世日記 > active_text_channel。"""
        guild = None
        if self.active_text_channel and getattr(self.active_text_channel, "guild", None):
            guild = self.active_text_channel.guild
        elif self.bot.guilds:
            guild = self.bot.guilds[0]
        if guild:
            for name in ("馬文系統狀態", "marvin-ops", "馬文的厭世日記", "marvin-diary"):
                ch = discord.utils.get(guild.text_channels, name=name)
                if ch:
                    return ch
        return self.active_text_channel

    @tasks.loop(seconds=60.0)
    async def reset_stt_counter_loop(self):
        """[STT Rate Limit] 每分鐘重設 STT 計數器"""
        if self._stt_call_counter > 0:
            logger.debug(f"🧹 [Rate Limit] 重設 STT 計數器 (上分鐘總計: {self._stt_call_counter})")
        self._stt_call_counter = 0

