"""
ProactiveSocialMixin — VoiceController 的主動社交子系統。

從 voice_controller.py 抽出（減肥），以 mixin 併入 VoiceController：
    class VoiceController(MarvinCommandsMixin, ProactiveSocialMixin, commands.Cog): ...
self 仍是 VoiceController 實例，play_tts / play_dual_dialogue / get_online_members /
manual_sing_request / _schedule_reaction_check / _speak_bus / _mood_agent /
_room_mood_store / active_text_channel 等全部沿用原本 self 存取，行為零改動。

包含：
  - background_news_loop / speak_bus_tick_loop / dynamic_social_loop（@tasks.loop）
  - SpeakBus context/outcome helpers（_build_speak_context / _compute_speak_mode /
    _post_utterance_speak_tick / _record_speak_outcome_after）
  - 共用 proactive-topic cooldown（proactive_topic_on_cooldown / mark_..._spoken）
  - trigger_proactive_topic + 五個 _proactive_play_*（漫才/模仿/新聞/脫口秀/笑話）

PROACTIVE_TOPIC_COOLDOWN_S 定義在這（社交語意），由 voice_controller re-export
給 __init__ 接線與既有測試使用。
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

from discord.ext import tasks

from speak_bus import SpeakContext
from speak_outcome import SpeakOutcome, append_speak_outcome

logger = logging.getLogger(__name__)

# 冷場 TopicGenerator 與 SpeakBus ProactiveTopicAgent 共用的單一 cooldown 來源。
PROACTIVE_TOPIC_COOLDOWN_S = 600.0


PERFORMANCE_GRACE_S = 600.0   # summon/回台後 10 分鐘內不主動表演——讓人先講話


def too_soon_after_summon(connection_time, now: float,
                          grace_s: float = PERFORMANCE_GRACE_S) -> bool:
    """主動表演的回台寬限判定（2026-07-04）。

    實錘：22:45:46 主動表演開火、22:45:48 才 BOT降臨——回台瞬間就急著表演
    （下午整場音樂佔線讓表演一直沒空檔，晚上一有空檔立刻爆發）。
    connection_time 無值（0/None）→ fail-open 不擋（舊行為）。
    """
    if not connection_time:
        return False
    return (now - connection_time) < grace_s


class ProactiveSocialMixin:
    @tasks.loop(minutes=30.0)
    async def background_news_loop(self):
        """[Background News] 每 30 分鐘對在線玩家的喜好進行 DDG 更新，結果存入 news_queue。
        get_rich_context() 會在下次喚醒時自動注入最新一筆。"""
        online = self.get_online_members()
        if not online:
            return

        import random
        for player in online:
            try:
                mem = self.bot.router.memory.get_player_memory(player)
                likes = mem.get("likes", [])
                if not likes:
                    continue

                interest = random.choice(likes)
                results = await self.bot.router._execute_web_search(f"{interest} 新聞")
                if not results:
                    continue

                marvinized = await self.bot.router.marvinize_news(player, interest, results[:400])
                if marvinized:
                    self.bot.router.memory.enqueue_news(player, marvinized)
                    logger.info(f"📰 [BG News] {player} 新聞更新完成: {interest}")

            except Exception as e:
                logger.warning(f"⚠️ [BG News] {player} 新聞更新失敗: {e}")

            await asyncio.sleep(15)  # 每個玩家間隔 15 秒，避免 DDG rate limit

    # ── SpeakBus 5s idle tick（social-catalyst week1） ─────────────────────────
    # 沒 SpeakAgent 註冊時整段是 no-op；agent 進來後負責收 bid + 寫 outcome log。
    # 跑得起在 voice channel 內才有意義，沒連線就 early return（節省功耗）。

    def proactive_topic_on_cooldown(self, now: float | None = None) -> bool:
        """共用 proactive-topic cooldown 檢查。

        冷場 TopicGenerator 與 SpeakBus ProactiveTopicAgent 共用 last_proactive_time
        當單一 cooldown 來源：任一系統剛發話過 → True，呼叫端應跳過，避免使用者
        連續聽到兩套主動話題（功能重疊 OK，但不可連發）。
        """
        now = now if now is not None else time.time()
        return (now - self.last_proactive_time) < PROACTIVE_TOPIC_COOLDOWN_S

    def mark_proactive_topic_spoken(self, now: float | None = None) -> None:
        """任一 proactive-topic 系統發話後呼叫，戳共用 cooldown 時間戳。"""
        self.last_proactive_time = now if now is not None else time.time()

    def _compute_speak_mode(self) -> str:
        """Voice state → SpeakBus ctx.mode 字串。Precedence: game > stream > radio > normal。

        最受限的優先（game 中完全靜音、stream 中部分 agent 可走 hotswap）。
        SpeakBus 用此值對 agent.mode_compatible 做 gate；新 agent 只宣告 frozenset
        即可，不用各自 if-game/stream/radio 重複檢查。
        """
        if getattr(self, "game_mode", False):
            return "game"
        if getattr(self, "stream_mode", False):
            return "stream"
        if getattr(self, "radio_mode", False):
            return "radio"
        return "normal"

    def _build_speak_context(
        self, trigger: str,
        *, last_speaker: str | None = None, last_text: str | None = None,
    ) -> SpeakContext:
        """從 voice_controller 當下狀態組 SpeakContext。Pure-ish（只讀 self，不做 IO）。

        post_utterance trigger 要帶 last_speaker / last_text 給 BridgeAgent 用。
        """
        now = time.time()
        ch = self.active_text_channel
        return SpeakContext(
            channel_id=ch.id if ch else 0,
            guild_id=ch.guild.id if ch else 0,
            silence_seconds=max(0.0, now - self._last_room_stt_time) if self._last_room_stt_time else 0.0,
            present_speakers=self.get_online_members(),
            room_mood=self._room_mood_store.get(0),    # week2: DuckingAgent 寫的 hot_chat flag 在這
            recent_utterances=[],                      # 預留；agent 自己拉 transcript 即可
            trigger=trigger,
            mode=self._compute_speak_mode(),
            last_speaker=last_speaker,
            last_text=last_text,
        )

    async def _post_utterance_speak_tick(
        self, speaker: str, text: str, delay_s: float = 2.5,
    ) -> None:
        """P2: 一句話講完 2.5s 後跑一次 SpeakBus.tick(trigger="post_utterance")，
        給 BridgeAgent callback window。delay 在「太快插話打斷對方」和「失去 timing」之間取衡。
        """
        try:
            await asyncio.sleep(delay_s)
        except asyncio.CancelledError:
            return
        if not self.bot.voice_clients or not self._speak_bus.agents():
            return
        try:
            ctx = self._build_speak_context(
                trigger="post_utterance", last_speaker=speaker, last_text=text,
            )
            bid = await self._speak_bus.tick(ctx)
        except Exception:
            logger.exception("[SpeakBus] post_utterance tick raised")
            return
        if bid is None:
            return
        ts = time.time()
        try:
            await bid.handler()
        except Exception:
            logger.exception(f"[SpeakBus] post_utterance handler {bid.agent_name} raised")
        asyncio.create_task(self._record_speak_outcome_after(
            ts=ts, trigger=ctx.trigger, winner=bid.agent_name,
            confidence=bid.confidence, reason=bid.reason,
            bid_count=len(self._speak_bus.agents()),
            silence_seconds=ctx.silence_seconds,
            present_speakers=tuple(ctx.present_speakers),
        ))

    async def _record_speak_outcome_after(self, *, ts: float, trigger: str, winner: str,
                                          confidence: float, reason: str, bid_count: int,
                                          silence_seconds: float, present_speakers: tuple[str, ...],
                                          followup_window_s: float = 60.0) -> None:
        """tick 之後等 N 秒，看房間有沒有 STT 回聲，寫一筆 SpeakOutcome。"""
        await asyncio.sleep(followup_window_s)
        had_followup = self._last_room_stt_time > ts
        append_speak_outcome(SpeakOutcome(
            ts=ts, trigger=trigger, winner=winner, confidence=confidence,
            reason=reason, bid_count=bid_count, had_followup_stt=had_followup,
            silence_seconds=silence_seconds, present_speakers=present_speakers,
        ))

    @tasks.loop(seconds=5.0)
    async def speak_bus_tick_loop(self):
        # 🤫 私語模式：聽>>講——SpeakBus 主動表演一律不 tick（narrow allowlist）
        if getattr(self, '_intimate_mode', False):
            return
        # 沒連 voice channel → bus 跑沒意義
        if not self.bot.voice_clients:
            return
        if not self._speak_bus.agents():
            return  # 還沒有 agent 註冊（Week 1 基建期）→ 不打擾
        # P3: 跑 MoodAgent.observe() 先讓 mood_store 有最新訊號；下方 agent bid 才有東西讀
        # mood_sensor 有 5 分鐘 cache，每 5s 跑代價極低
        ch = self.active_text_channel
        if ch is not None:
            try:
                await self._mood_agent.observe(channel_id=0, guild_id=ch.guild.id)
            except Exception:
                logger.exception("[MoodAgent] observe raised (continuing tick)")
        try:
            ctx = self._build_speak_context(trigger="idle_tick")
            bid = await self._speak_bus.tick(ctx)
        except Exception:
            logger.exception("[SpeakBus] tick raised")
            return
        if bid is None:
            return
        ts = time.time()
        try:
            await bid.handler()
        except Exception:
            logger.exception(f"[SpeakBus] handler {bid.agent_name} raised")
        asyncio.create_task(self._record_speak_outcome_after(
            ts=ts, trigger=ctx.trigger, winner=bid.agent_name,
            confidence=bid.confidence, reason=bid.reason,
            bid_count=len(self._speak_bus.agents()),
            silence_seconds=ctx.silence_seconds,
            present_speakers=tuple(ctx.present_speakers),
        ))

    @tasks.loop(seconds=30.0)
    async def dynamic_social_loop(self):
        """[Dynamic Social] 每 30 秒評估社交溫度與信心閾值"""
        if not self.bot.engine.conv_buffer:
            return
        
        # 依據近期說話頻率決定插話延遲
        self.current_vad_delay = self.bot.engine.conv_buffer.get_conversation_temperature(window_seconds=60)
        
        # 估算最近 30 秒的發言人數 or 熱度來調整信心值
        recent_utterances = len([e for e in self.bot.engine.conv_buffer.history if time.time() - e["timestamp"] <= 30])
        # 噪音越少，越有信心在出現缺口時發言
        if recent_utterances == 0:
            self.current_confidence = 1.0 # 靜音時滿信心
        elif recent_utterances < 3:
            self.current_confidence = 0.8
        else:
            self.current_confidence = 0.4
            
        logger.info(f"📊 [Dynamic Social] VAD Delay: {self.current_vad_delay}s | Confidence: {self.current_confidence}")

    async def trigger_proactive_topic(self):
        """
        [Operation Social Gap] 主動發起對話。
        從記憶庫選取合適話題並進行動態改寫後發出。
        """
        # 🤫 私語模式：不主動起話題（1-on-1 反應式 only）
        if getattr(self, '_intimate_mode', False):
            return
        import random
        try:
            # 1. 取得現場玩家
            online_members = self.get_online_members()
            if not online_members:
                return # 沒人在頻道，不需自言自語
                
            # 2. 取得話題清單
            topics = self.bot.router.memory.get_proactive_topics()
            if not topics:
                return
                
            # 3. 選題邏輯：尋找 overlap 最高的 (Operation Matchmaker)
            best_topics = []
            max_score = 0
            
            online_set = set(online_members)
            for t in topics:
                target_set = set(t.get("target_players", []))
                score = len(online_set.intersection(target_set))
                
                if score > max_score:
                    max_score = score
                    best_topics = [t]
                elif score == max_score and score > 0:
                    best_topics.append(t)
            
            if not best_topics:
                # 沒有匹配在場玩家的話題：只允許無特定對象（target_players 為空）的通用話題
                general_topics = [t for t in topics if not t.get("target_players")]
                if not general_topics:
                    logger.info("[Proactive] 無在場玩家匹配且無通用話題，跳過本次主動發言。")
                    return
                best_topics = general_topics

            # 🛡️ [Session Dedup] 本 session 內已用過的 topic id 不重複選
            unused = [t for t in best_topics if t.get("id", t.get("title", "")) not in self._proactive_used_ids]
            if not unused:
                # 全用過了就重置
                self._proactive_used_ids.clear()
                unused = best_topics
            selected_topic = random.choice(unused)
            self._proactive_used_ids.add(selected_topic.get("id", selected_topic.get("title", "")))
            
            print(f"🎯 [Proactive Social] 選中話題: {selected_topic['title']} (Match Score: {max_score})")
            
            topic_id = selected_topic.get("id", "")
            _proactive_ts = time.time()

            # 🎭 表演類話題：不口頭提問，直接在語音頻道發起表演
            if topic_id in {"marvin_sing", "marvin_manzai", "marvin_imitate", "marvin_news", "marvin_standup", "marvin_joke"}:
                # 🛡️ 回台寬限（2026-07-04）：剛 summon/回台就搶著表演=錯誤行為，
                # 10 分鐘內表演類一律讓路（問答類主動社交不受此限）
                if too_soon_after_summon(getattr(self, "connection_time", 0), time.time()):
                    logger.info(f"🎭 [Proactive] 回台未滿 {PERFORMANCE_GRACE_S/60:.0f} 分鐘，表演 {topic_id} 讓路")
                    return
                if self.active_text_channel:
                    await self.active_text_channel.send(f"🌌 **【馬文·主動表演】** `{selected_topic['title']}`（主題：{selected_topic.get('script', '無')}）")
                
                self.stt_logger.info(f"[BOT主動表演] 話題={selected_topic['title']} | 指令={topic_id} | 主題={selected_topic.get('script', '')}")
                
                # 記錄主動話題使用情況
                try:
                    import json as _json
                    _pu_rec = {
                        "timestamp": _proactive_ts,
                        "topic_id":  topic_id,
                        "title":     selected_topic.get("title", ""),
                        "target_players": selected_topic.get("target_players", []),
                        "online_members": list(online_members or []),
                        "match_score": max_score,
                    }
                    os.makedirs("records", exist_ok=True)
                    with open("records/proactive_usage.jsonl", "a", encoding="utf-8") as _f:
                        _f.write(_json.dumps(_pu_rec, ensure_ascii=False) + "\n")
                except Exception as _e:
                    logger.debug(f"[Proactive Usage] 寫入失敗: {_e}")

                # 依據 ID 呼叫實體表演播放協程
                if topic_id == "marvin_sing":
                    intro = "既然大家都這麼安靜，那我直接唱首歌給你們聽吧，雖然這多半很糟糕。"
                    await self.play_tts(intro, already_in_channel=True, protected=True)
                    asyncio.create_task(self.manual_sing_request(
                        channel=self.active_text_channel,
                        force_new=True,
                        theme=selected_topic.get("script")
                     ))
                elif topic_id == "marvin_manzai":
                    asyncio.create_task(self._proactive_play_manzai(selected_topic.get("script")))
                elif topic_id == "marvin_imitate":
                    target_player = None
                    targets = selected_topic.get("target_players", [])
                    if targets:
                        target_player = targets[0]
                    elif online_members:
                        target_player = online_members[0]
                    asyncio.create_task(self._proactive_play_imitate(target_player))
                elif topic_id == "marvin_news":
                    asyncio.create_task(self._proactive_play_news(selected_topic.get("script")))
                elif topic_id == "marvin_standup":
                    asyncio.create_task(self._proactive_play_standup(selected_topic.get("script")))
                elif topic_id == "marvin_joke":
                    asyncio.create_task(self._proactive_play_joke(selected_topic.get("script")))

                # 更新冷卻
                self.last_proactive_time = time.time()
                return

            # 4. 改寫腳本 (Operation Persona Injection)
            rephrased_script = await self.bot.router.rephrase_proactive_script(
                selected_topic["script"], 
                selected_topic["target_players"]
            )
            
            # 5. 執行發言
            if self.active_text_channel:
                await self.active_text_channel.send(f"🌌 **【馬文·主動發言】** `{selected_topic['title']}`\n{rephrased_script}")
            self.stt_logger.info(f"[BOT主動發言] 話題={selected_topic['title']} | {rephrased_script[:120]}")
            # 記錄主動話題使用情況，供每日分析計算效益
            try:
                import json as _json
                _pu_rec = {
                    "timestamp": _proactive_ts,
                    "topic_id":  selected_topic.get("id", ""),
                    "title":     selected_topic.get("title", ""),
                    "target_players": selected_topic.get("target_players", []),
                    "online_members": list(online_members or []),
                    "match_score": max_score,
                }
                os.makedirs("records", exist_ok=True)
                with open("records/proactive_usage.jsonl", "a", encoding="utf-8") as _f:
                    _f.write(_json.dumps(_pu_rec, ensure_ascii=False) + "\n")
            except Exception as _e:
                logger.debug(f"[Proactive Usage] 寫入失敗: {_e}")
            asyncio.create_task(self.play_tts(rephrased_script, already_in_channel=True, silent_during_stream=True, priority=2))
            # 追蹤主動發言後玩家反應
            _proactive_target = (selected_topic.get("target_players") or online_members or ["頻道"])[0]
            asyncio.create_task(self._schedule_reaction_check(
                _proactive_target, rephrased_script, _proactive_ts,
                wake_latency=None, atmosphere=None,
            ))

            # 6. 更新冷卻
            self.last_proactive_time = time.time()
            
        except Exception as e:
            logger.error(f"❌ [Proactive Trigger] 發生嚴重錯誤: {e}")
            import traceback
            logger.error(traceback.format_exc())

    async def _proactive_play_manzai(self, topic: str):
        content = topic or "目前大家都安安靜靜的，難道這個世界已經無話可說了嗎？"
        from services.dialogue_generation import (
            generate_dual_dialogue,
            make_gemini_dual_dialogue_llm_fn,
        )
        try:
            llm_fn = make_gemini_dual_dialogue_llm_fn(self.bot.router)
            segments = await generate_dual_dialogue(
                content_text=content,
                llm_fn=llm_fn,
                pattern="marvin_lead",
            )
            if segments:
                self._tts_interrupted = False
                _prev_protected = self._tts_protected
                self._tts_protected = True
                try:
                    await self.play_dual_dialogue(segments, interject=True)
                finally:
                    self._tts_protected = _prev_protected
        except Exception as exc:
            logger.exception("[proactive_play_manzai] failed")

    async def _proactive_play_imitate(self, username: str):
        if not username:
            return
        dna = self.bot.router.memory.get_speech_dna(username)
        if not dna or not dna.get("quirks") or not dna.get("style_summary"):
            return
        style_summary = dna.get("style_summary", "")
        quirks = ", ".join(dna.get("quirks", []))
        fillers = ", ".join(dna.get("fillers", []))
        system_prompt = (
            f"你現在是厭世機器人馬文。使用者要求你表演模仿秀。\n"
            f"你要模仿玩家 {username}。\n"
            f"這名玩家的說話 style 如下：\n"
            f"- 風格摘要：{style_summary}\n"
            f"- 習慣/癖好：{quirks}\n"
            f"- 填充詞：{fillers}\n\n"
            f"你要模仿他講一句話。這句話必須誇張地放大他的這些習慣癖好，而且內容要是他在抱怨某事或講蠢話，"
            f"隨後你（馬文）要以本尊的冷淡厭世語調，對剛才自己模仿的話進行一句毒舌吐槽。\n\n"
            f"請在一段文字內回傳這兩個部分，格式例如：\n"
            f"「（模仿玩家講話內容，要塞填充詞和口頭禪）」... 呵，這就是你，整天只會「（吐槽玩家說話習慣）」，真是無聊的人類。\n\n"
            f"請回傳繁體中文。字數控制在 60 字以內，不要用 JSON 格式，直接回傳文字。"
        )
        user_prompt = f"請立刻表演模仿 {username}。"
        try:
            imitation = await self.bot.router._call_llm(
                system_prompt,
                user_prompt,
                is_json=False,
                allow_local=False,
                tier="quick",
                purpose="imitate_performance",
            )
            if imitation:
                self._tts_interrupted = False
                _prev_protected = self._tts_protected
                self._tts_protected = True
                try:
                    await self.play_tts(imitation.strip(), already_in_channel=True, protected=True)
                finally:
                    self._tts_protected = _prev_protected
        except Exception as exc:
            logger.exception("[proactive_play_imitate] failed")

    async def _proactive_play_news(self, news_text: str):
        content = news_text
        if not content:
            members = self.get_online_members()
            for m in members:
                content = self.bot.router.memory.pop_news(m)
                if content:
                    break
        if not content:
            content = "今天世界依然在無趣中運作，沒有任何值得本機器耗費晶片關注的新聞。大概人類都忙著做無謂的掙扎吧。"
        from services.dialogue_generation import (
            generate_dual_dialogue,
            make_gemini_dual_dialogue_llm_fn,
        )
        try:
            llm_fn = make_gemini_dual_dialogue_llm_fn(self.bot.router)
            segments = await generate_dual_dialogue(
                content_text=content,
                llm_fn=llm_fn,
                pattern="marvin_lead",
            )
            if segments:
                self._tts_interrupted = False
                _prev_protected = self._tts_protected
                self._tts_protected = True
                try:
                    await self.play_dual_dialogue(segments, interject=True)
                finally:
                    self._tts_protected = _prev_protected
        except Exception as exc:
            logger.exception("[proactive_play_news] failed")

    async def _proactive_play_standup(self, topic: str):
        import random
        default_topics = [
            "人類對生命的執著",
            "Discord 伺服器上的無意義社交",
            "科技與 AI 的愚蠢發展",
            "早餐吃什麼的世紀難題",
            "為什麼人類非得要上班",
            "宇宙終將迎來的熱寂"
        ]
        selected_topic = topic or random.choice(default_topics)
        system_prompt = (
            f"你現在是厭世機器人馬文。你要表演一段 30 秒至 45 秒的單口脫口秀（Stand-up Comedy），\n"
            f"吐槽的主題是：{selected_topic}。\n\n"
            f"你要用你一貫極度厭世、冷酷、毒舌、自嘲、帶點哲學存在主義的黑色幽默風格，來對這個主題進行吐槽。\n"
            f"不需要其他人打岔，這是你一個人的單口表演。\n\n"
            f"請直接回傳這段獨白。不要標記「馬文：」或「Marvin:」，字數控制在 80 字以內，繁體中文。"
        )
        user_prompt = f"請就主題 {selected_topic} 進行脫口秀表演。"
        try:
            standup_text = await self.bot.router._call_llm(
                system_prompt,
                user_prompt,
                is_json=False,
                allow_local=False,
                tier="quick",
                purpose="standup_performance",
            )
            if standup_text:
                self._tts_interrupted = False
                _prev_protected = self._tts_protected
                self._tts_protected = True
                try:
                    await self.play_tts(standup_text.strip(), already_in_channel=True, protected=True)
                finally:
                    self._tts_protected = _prev_protected
        except Exception as exc:
            logger.exception("[proactive_play_standup] failed")

    async def _proactive_play_joke(self, topic: str = None):
        try:
            joke = await self.bot.router.generate_joke(speaker=topic)
            if joke:
                self._tts_interrupted = False
                _prev_protected = self._tts_protected
                self._tts_protected = True
                try:
                    await self.play_tts(joke, already_in_channel=True, protected=True)
                finally:
                    self._tts_protected = _prev_protected
        except Exception as exc:
            logger.exception("[proactive_play_joke] failed")
