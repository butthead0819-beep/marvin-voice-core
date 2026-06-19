"""
MarvinCommandsMixin — VoiceController 的「表演 / 觀察報告 / 系統診斷」slash 指令。

從 voice_controller.py 抽出（減肥），以 mixin 形式併入 VoiceController：
    class VoiceController(MarvinCommandsMixin, commands.Cog): ...
因此 self 仍是 VoiceController 實例，play_tts / play_dual_dialogue /
_tts_protected / manual_sing_request / get_online_members / bot.router 等
全部沿用原本的 self 存取，行為零改動。

留在 VoiceController 的：summon / dismiss（連線生命週期）、marvin_reboot /
marvin_tts_clear / marvin_optin / marvin_optout（控制面）。
"""
from __future__ import annotations

import asyncio
import datetime
import logging

import discord
from discord import app_commands

from quality_metrics import (
    read_metrics,
    summarize_false_responding,
    summarize_interruption,
    summarize_latency,
    summarize_recall,
)

logger = logging.getLogger(__name__)


class MarvinCommandsMixin:
    @app_commands.command(name="marvin_bias", description="[Admin] 手動耳語：更新馬文對某位玩家的潛意識偏見")
    @app_commands.describe(username="玩家的 Discord 顯示名稱", impression="新的偏見描述")
    async def marvin_bias(self, interaction: discord.Interaction, username: str, impression: str):
        if self.bot.engine.bias_update_callback:
            print(f"👂 [Admin] 手動更新偏見: {username} -> {impression}")
            await self.bot.engine.bias_update_callback(username, impression)
            await interaction.response.send_message(f"👁️ **潛意識已修正**：馬文對 `{username}` 的評價已更新。")
        else:
            await interaction.response.send_message("❌ 無法執行指令：回饋函式未註冊。", ephemeral=True)

    @app_commands.command(name="marvin_sing", description="[Paranoid Android] 讓馬文即興製作一首低沉單曲")
    @app_commands.describe(theme="[選填] 手動指定歌曲主題（例：祝大肚生日快樂）")
    async def marvin_sing(self, interaction: discord.Interaction, theme: str = None):
        await interaction.response.defer(thinking=True)
        scrap = await self.bot.router.generate_dynamic_system_msg("songs_request")
        await interaction.followup.send(f"🎵 {scrap}")
        await self.play_tts(scrap, already_in_channel=True)
        asyncio.create_task(self.manual_sing_request(channel=interaction.channel, force_new=True, theme=theme))

    @app_commands.command(name="marvin_joke", description="[Operation Joke] 聽馬文講一個關於宇宙多麼糟糕的笑話")
    async def marvin_joke(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        joke = await self.bot.router.generate_joke(speaker=interaction.user.display_name)
        scrap = await self.bot.router.generate_dynamic_system_msg("joke_request")
        await interaction.followup.send(f"🃏 {scrap}\n「{joke}」")
        self._tts_interrupted = False
        _prev_protected = self._tts_protected
        self._tts_protected = True
        try:
            await self.play_tts(joke, already_in_channel=True, protected=True)
        finally:
            self._tts_protected = _prev_protected


    @app_commands.command(name="marvin_say", description="[Voice] 讓馬文用他的聲音念出你打的字")
    @app_commands.describe(text="要馬文念出來的文字")
    async def marvin_say(self, interaction: discord.Interaction, text: str):
        # 刻意不走 SpeakBus：SpeakBus 是「主動發話」的仲裁（idle/mood 觸發 agent 競標
        # 該不該插嘴），這裡是使用者下的直接命令，沒有「要不要開口」可競標——走 bus
        # 反而可能被 MIN_CONFIDENCE / DuckingAgent 壓制而不發聲，違背指令本意。仍受
        # play_tts 的播放鎖鏈（playback_lock / tts_queue_lock / mixer）正確序列化。
        # 同 marvin_sing / marvin_joke。
        await interaction.response.defer(thinking=True)
        await interaction.followup.send(f"🗣️ 「{text}」")
        # protected：手動拉起 _tts_protected（比照進場招呼），讓 play_tts 的靜默閘 /
        # queue-drop guard 一律放行，確保整句念完不被砍；_tts_interrupted 先清掉避免
        # 被前一次中斷旗標吞掉。結束還原原值，不 clobber 既有保護播放。
        self._tts_interrupted = False
        _prev_protected = self._tts_protected
        self._tts_protected = True
        try:
            # force_macos=True：走 macOS say 男聲（中文 Liao→Han 備援、英文 Alex），
            # 不走 edge-tts 的 Marvin 預設聲。
            await self.play_tts(text, already_in_channel=True, protected=True, force_macos=True)
        finally:
            self._tts_protected = _prev_protected

    @app_commands.command(name="marvin_manzai", description="[Operation] 立刻讓馬文與 Marmo 進行雙人漫才表演")
    @app_commands.describe(topic="可選：指定要表演/吐槽的主題")
    async def marvin_manzai(self, interaction: discord.Interaction, topic: str = None):
        await interaction.response.defer(thinking=True)
        if topic:
            content = topic
        else:
            history = []
            if self.bot.engine.conv_buffer and self.bot.engine.conv_buffer.history:
                history = [e for e in self.bot.engine.conv_buffer.history][-5:]
            if history:
                content = "\n".join(
                    f"{e.get('speaker', '?')}: {e.get('text', '')}"
                    for e in history
                ).strip()
            else:
                content = "目前大家都安安靜靜的，難道這個世界已經無話可說了嗎？"

        await interaction.followup.send(f"🎭 漫才主題：\n「{content}」\n(開始生成中...)")

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
        except Exception as exc:
            logger.exception("[marvin_manzai] generate_dual_dialogue failed")
            await interaction.followup.send(f"❌ 漫才生成失敗: {exc}")
            return

        if not segments:
            await interaction.followup.send("❌ 漫才生成結果為空。")
            return

        try:
            self._tts_interrupted = False
            _prev_protected = self._tts_protected
            self._tts_protected = True
            try:
                await self.play_dual_dialogue(segments, interject=True)
            finally:
                self._tts_protected = _prev_protected
        except Exception as exc:
            logger.exception("[marvin_manzai] play_dual_dialogue failed")
            await interaction.followup.send(f"❌ 漫才播放失敗: {exc}")


    @app_commands.command(name="marvin_imitate", description="[Operation] 讓馬文模仿某位玩家的說話風格並進行吐槽")
    @app_commands.describe(target="[選填] 指定要模仿的玩家（預設為自己）")
    async def marvin_imitate(self, interaction: discord.Interaction, target: discord.Member = None):
        await interaction.response.defer(thinking=True)
        target_user = target or interaction.user
        username = target_user.display_name
        
        dna = self.bot.router.memory.get_speech_dna(username)
        
        # 檢查 dna 是否有效，如果為空或缺少關鍵欄位則走 fallback
        if not dna or not dna.get("quirks") or not dna.get("style_summary"):
            fallback_text = f"我對 `{username}` 這卑微的人類毫無頭緒。看來你對我不夠敞開心房，多跟我講點話讓我收集 DNA 吧。"
            await interaction.followup.send(f"👁️ {fallback_text}")
            self._tts_interrupted = False
            _prev_protected = self._tts_protected
            self._tts_protected = True
            try:
                await self.play_tts(fallback_text, already_in_channel=True, protected=True)
            finally:
                self._tts_protected = _prev_protected
            return

        # 組合 Prompt 呼叫 LLM
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
            imitation = imitation.strip()
        except Exception as exc:
            logger.exception("[marvin_imitate] LLM call failed")
            await interaction.followup.send(f"❌ 模仿秀生成失敗: {exc}")
            return

        if not imitation:
            await interaction.followup.send("❌ 模仿秀生成結果為空。")
            return

        await interaction.followup.send(f"🎭 **馬文的玩家模仿秀：{username}**\n{imitation}")

        self._tts_interrupted = False
        _prev_protected = self._tts_protected
        self._tts_protected = True
        try:
            await self.play_tts(imitation, already_in_channel=True, protected=True)
        finally:
            self._tts_protected = _prev_protected

    @app_commands.command(name="marvin_news", description="[Operation] 讓馬文與 Marmo 播報近期玩家討論話題或新聞的漫才秀")
    @app_commands.describe(target="[選填] 指定播報對象（獲取其個人新聞）")
    async def marvin_news(self, interaction: discord.Interaction, target: discord.Member = None):
        await interaction.response.defer(thinking=True)
        news_text = None
        target_name = None
        
        if target:
            target_name = target.display_name
            news_text = self.bot.router.memory.pop_news(target_name)
        else:
            # 遍歷當前語音頻道在線人類，尋找有積累新聞的
            members = self.get_online_members()
            for m in members:
                news_text = self.bot.router.memory.pop_news(m)
                if news_text:
                    target_name = m
                    break
        
        if not news_text:
            news_text = "今天世界依然在無趣中運作，沒有任何值得本機器耗費晶片關注的新聞。大概人類都忙著做無謂的掙扎吧。"
            topic_desc = "冷場全域新聞（無累積個人新聞）"
        else:
            topic_desc = f"{target_name} 的個人化新聞"

        await interaction.followup.send(f"🗞️ 新聞主題：{topic_desc}\n「{news_text}」\n(開始播報中...)")

        from services.dialogue_generation import (
            generate_dual_dialogue,
            make_gemini_dual_dialogue_llm_fn,
        )
        try:
            llm_fn = make_gemini_dual_dialogue_llm_fn(self.bot.router)
            segments = await generate_dual_dialogue(
                content_text=news_text,
                llm_fn=llm_fn,
                pattern="marvin_lead",
            )
        except Exception as exc:
            logger.exception("[marvin_news] generate_dual_dialogue failed")
            await interaction.followup.send(f"❌ 新聞對白生成失敗: {exc}")
            return

        if not segments:
            await interaction.followup.send("❌ 新聞對白生成結果為空。")
            return

        # 發送對白文字到 Discord 頻道
        lines = []
        for s in segments:
            spk = "🤖 馬文" if s["voice"] == "marvin" else "🦧 馬末"
            lines.append(f"{spk}：「{s['text']}」")
        await interaction.followup.send("\n".join(lines))

        try:
            self._tts_interrupted = False
            _prev_protected = self._tts_protected
            self._tts_protected = True
            try:
                await self.play_dual_dialogue(segments, interject=True)
            finally:
                self._tts_protected = _prev_protected
        except Exception as exc:
            logger.exception("[marvin_news] play_dual_dialogue failed")
            await interaction.followup.send(f"❌ 新聞對白播放失敗: {exc}")

    @app_commands.command(name="marvin_standup", description="[Operation] 讓馬文來一段關於某主題的厭世單口脫口秀")
    @app_commands.describe(topic="[選填] 指定脫口秀吐槽主題（預設為隨機）")
    async def marvin_standup(self, interaction: discord.Interaction, topic: str = None):
        await interaction.response.defer(thinking=True)
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
        await interaction.followup.send(f"🎤 脫口秀主題：{selected_topic}\n(馬文正在登台...)")
        
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
            standup_text = standup_text.strip()
        except Exception as exc:
            logger.exception("[marvin_standup] LLM call failed")
            await interaction.followup.send(f"❌ 脫口秀生成失敗: {exc}")
            return

        if not standup_text:
            await interaction.followup.send("❌ 脫口秀生成結果為空。")
            return

        await interaction.followup.send(f"🎤 **馬文的個人脫口秀：{selected_topic}**\n「{standup_text}」")

        self._tts_interrupted = False
        _prev_protected = self._tts_protected
        self._tts_protected = True
        try:
            await self.play_tts(standup_text, already_in_channel=True, protected=True)
        finally:
            self._tts_protected = _prev_protected

    @app_commands.command(name="marvin_status", description="[Agent Report] 查看馬文對你這卑微人類的觀察報告")
    async def marvin_status(self, interaction: discord.Interaction, target: discord.Member = None):
        await interaction.response.defer(thinking=True)
        target_user = target or interaction.user
        mem = self.bot.router.memory.get_player_memory(target_user.display_name)
        stats = mem.get("stats", {"interaction_count": 0, "pos_feedback": 0, "neg_feedback": 0})
        fragments = len(mem.get("likes", [])) + len(mem.get("dislikes", [])) + sum(1 for v in mem.get("personal_info", {}).values() if v)
        comment = await self.bot.router.generate_status_report_comment(target_user.display_name, stats, fragments)
        
        embed = discord.Embed(
            title=f"📋 馬文的低階觀察報告：{target_user.display_name}",
            description=f"「{comment}」",
            color=discord.Color.dark_grey(),
            timestamp=datetime.datetime.now()
        )
        embed.set_thumbnail(url=target_user.display_avatar.url)
        embed.add_field(name="🧬 厭世程度", value=f"{self.bot.router.dna.get('toxicity', 10)}/10", inline=True)
        embed.add_field(name="🧠 人格標籤", value=f"{self.bot.router.dna.get('persona_tag', '厭世機器人馬文')}", inline=True)
        embed.add_field(name="🗑️ 腦內垃圾數", value=f"{fragments} 片", inline=True)
        embed.add_field(name="💬 浪費時間次數", value=f"{stats['interaction_count']} 次", inline=True)
        embed.add_field(name="💖 微弱亮點", value=f"{stats['pos_feedback']} 次", inline=True)
        embed.add_field(name="💢 絕望時刻", value=f"{stats['neg_feedback']} 次", inline=True)
        
        footer_scrap = await self.bot.router.generate_dynamic_system_msg("report_sent")
        embed.set_footer(text=f"⚙️ {footer_scrap}")
        await interaction.followup.send(embed=embed)
    @staticmethod
    def _fmt_pool_status(rows: list[dict]) -> str:
        """把 CooldownAwarePool.status() 排成 embed 行：狀態 emoji + 名稱 + TPM%/冷卻。

        只呈現 pool 真知道的（滾動 60s TPM + 冷卻），不估 TPD（本地計數會低估、會騙人）。
        """
        if not rows:
            return "（無 endpoint — 檢查 API key）"
        _emoji = {"available": "✅", "cooldown": "🧊", "tpm_high": "🟡"}
        lines = []
        for r in rows:
            e = _emoji.get(r["status"], "❔")
            tail = (f"冷卻 {r['cooldown_remaining']:.0f}s" if r["status"] == "cooldown"
                    else f"{r['tpm_pct']:.0f}% TPM")
            lines.append(f"{e} `{r['name']}` · {tail}")
        return "\n".join(lines)

    @staticmethod
    def _fmt_quality_today(rows: list[dict]) -> str:
        """當日品質指標四行（per feedback_marvin_quality_metrics）。無樣本＝剛重啟/還沒對話。"""
        fr = summarize_false_responding(rows)
        rk = summarize_latency([r for r in rows if r.get("metric") == "react"], "react_ms")
        it = summarize_interruption(rows)
        it_idle = summarize_interruption(rows, idle_only=True)
        rc = summarize_recall(rows)
        lines = []
        lines.append(f"⏱️ 反應: p50 {rk['p50']:.0f}ms / p95 {rk['p95']:.0f}ms (n={rk['count']})"
                     if rk["count"] else "⏱️ 反應: 今日無樣本")
        lines.append(f"🗣️ 誤回應: {fr['false_rate'] * 100:.0f}% (n={fr['total']})"
                     if fr["total"] else "🗣️ 誤回應: 今日無樣本")
        lines.append(f"✂️ 打斷: {it['interrupt_rate'] * 100:.0f}% (淨 {it_idle['interrupt_rate'] * 100:.0f}%, n={it['total']})"
                     if it["total"] else "✂️ 打斷: 今日無樣本")
        lines.append(f"🧠 記憶 recall: {rc['accuracy'] * 100:.0f}% (n={rc['total']})"
                     if rc["total"] else "🧠 記憶 recall: 每週一 probe")
        return "\n".join(lines)

    @app_commands.command(name="marvin_system", description="[System] 查看馬文的核心系統、網路備援與配額狀態")
    async def marvin_system(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        router = self.bot.router
        
        # Determine Limit Status
        budget_status = router.budget.get_info()
        used_pct = budget_status["percentage"]
        used_k = budget_status["used"] // 1000
        max_k = budget_status["max"] // 1000
        remaining_pct = max(0, 100 - used_pct)

        if router.is_exhausted or used_pct >= 100:
            limit_status = "🚨 嚴重 (主要 API 額度已耗盡，雲端防護鎖定中)"
            budget_color = discord.Color.red()
        elif router.budget.is_circuit_open() or used_pct >= 95:
            limit_status = "⚠️ 警告 (花費預算達日上限觸發熔斷)"
            budget_color = discord.Color.orange()
        elif used_pct >= 80:
            limit_status = "🟡 注意 (用量偏高)"
            budget_color = discord.Color.yellow()
        else:
            limit_status = "✅ 正常"
            budget_color = discord.Color.dark_grey()

        bar_filled = int(used_pct / 10)
        budget_bar = "█" * bar_filled + "░" * (10 - bar_filled)
        budget_line = f"`[{budget_bar}]` {used_pct:.1f}% 已用\n{used_k}k / {max_k}k tokens　剩餘 **{remaining_pct:.1f}%**"

        embed = discord.Embed(
            title="⚙️ 馬文系統診斷報告",
            description="「既然你那麼好心要幫我檢查身體，那我只好把這些無聊的數據攤在陽光下了。」",
            color=budget_color,
            timestamp=datetime.datetime.now()
        )
        embed.add_field(name="🧠 當前運算層級", value=f"**{router.current_tier}**", inline=False)
        embed.add_field(name="☁️ Tier-1 主大腦", value=f"`{router.model_name}`\n限制狀態: {limit_status}", inline=False)
        embed.add_field(name="💰 今日 Token 用量 (Gemini)", value=budget_line, inline=False)
        
        # TTS info
        tts_name = self.bot.tts._speaker if hasattr(self.bot, 'tts') else "zh-TW-YunJheNeural"
        embed.add_field(name="🗣️ 發聲模組 (TTS)", value=f"`Edge-TTS: {tts_name}`\n狀態: 運作中", inline=False)

        # 算力池（cleaner / curation resolver / feedback_analyzer 共用的 quick/analyze 兩層）
        # 顯示即時狀態 + TPM%（滾動 60s）；✅可用 / 🧊冷卻中 / 🟡TPM近上限
        tier_router = getattr(router, "_stt_router", None)
        if tier_router is None:
            embed.add_field(name="✨ 語音清洗算力池",
                            value="尚未初始化（lazy build，等第一次清洗才建池）", inline=False)
        else:
            embed.add_field(name="🪶 輕量池 quick（STT 清洗主力）",
                            value=self._fmt_pool_status(tier_router.quick_pool.status()), inline=False)
            embed.add_field(name="🧠 分析池 analyze（curation / feedback）",
                            value=self._fmt_pool_status(tier_router.analyze_pool.status()), inline=False)

        # 今日品質指標（react / 誤回應 / 打斷 / recall）— 讀當日 quality_metrics.jsonl
        try:
            _today = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            _q_rows = read_metrics(since_ts=_today)
            embed.add_field(name="📊 今日品質指標", value=self._fmt_quality_today(_q_rows), inline=False)
        except Exception as _e:
            logger.debug(f"⚠️ [marvin_system] 品質指標讀取失敗: {_e}")

        await interaction.followup.send(embed=embed)
