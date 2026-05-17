"""海龜湯 Discord cog — v0 MVP。

延 Busted99 模式：
- engine 不知道 Discord，所有 UI / TTS / SFX 在這層
- on_state_change 是唯一的 cog ⇄ engine 事件管道
- STT hook 透過 receive_voice_question_by_speaker
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from game.turtle_soup.engine import TurtleSoupEngine
from game.turtle_soup.session import (
    EndReason,
    TurtleSoupSession,
    TurtleSoupState,
)
from game.turtle_soup.puzzles import get_default_puzzle
from game.turtle_soup.voice_parse import classify_intent

logger = logging.getLogger(__name__)

# Embed 顏色
C_JOINING = 0x5865F2
C_PRESENTING = 0x9B59B6
C_ASKING = 0xFF8C00
C_OVER_WIN = 0x57F287
C_OVER_LOSE = 0xED4245
C_OVER_CANCEL = 0x808080

VERDICT_SFX = {
    "yes": "correct",
    "no": "buzz",
    "irrelevant": "ba_dum_tss",
}

VERDICT_EMOJI = {
    "yes": "✅",
    "no": "❌",
    "irrelevant": "💨",
}


# ── Views ─────────────────────────────────────────────────────────────────────

class TurtleJoinView(discord.ui.View):
    def __init__(self, cog: "TurtleSoupCog"):
        super().__init__(timeout=35)
        self._cog = cog

    @discord.ui.button(label="Join 🐢", style=discord.ButtonStyle.primary)
    async def join(self, interaction: discord.Interaction, _b: discord.ui.Button):
        user = interaction.user
        ok = await self._cog._engine.add_player(str(user.id), user.display_name)
        if ok:
            self._cog._name_to_id[user.display_name] = user.id
            await interaction.response.send_message(
                f"✅ {user.display_name} 加入海龜湯！", ephemeral=True
            )
            await self._cog._post_or_edit(
                self._cog._build_joining_embed(self._cog._session), TurtleJoinView(self._cog),
            )
        else:
            await interaction.response.send_message("你已加入或遊戲已開始。", ephemeral=True)

    @discord.ui.button(label="Start Now ▶️", style=discord.ButtonStyle.success)
    async def start_now(self, interaction: discord.Interaction, _b: discord.ui.Button):
        if self._cog._engine is None:
            return
        if not self._cog._session.players:
            await interaction.response.send_message("至少需 1 位玩家", ephemeral=True)
            return
        await interaction.response.defer()
        await self._cog._engine.begin_presenting()

    async def on_timeout(self):
        # 35s 自動進 PRESENTING（若仍 JOINING 且有人）
        if self._cog._engine and self._cog._session and self._cog._session.state == TurtleSoupState.JOINING:
            if self._cog._session.players:
                await self._cog._engine.begin_presenting()
            else:
                await self._cog._engine.cancel()


# ── Cog ───────────────────────────────────────────────────────────────────────

class TurtleSoupCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._engine: Optional[TurtleSoupEngine] = None
        self._session: Optional[TurtleSoupSession] = None
        self._channel: Optional[discord.TextChannel] = None
        self._tasks: set[asyncio.Task] = set()
        self._name_to_id: dict[str, int] = {}
        # FIFO inflight cap：同時最多 N 個 judge 進行中
        self._asking_inflight = 0
        self._MAX_ASKING_INFLIGHT = 3

    # ── Task helpers ──────────────────────────────────────────────────────────

    def _spawn(self, coro):
        t = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return t

    def _cancel_tasks(self):
        for t in list(self._tasks):
            if not t.done():
                t.cancel()
        self._tasks.clear()

    # ── Game mode（沿 Busted99）───────────────────────────────────────────────

    def _enter_game_mode(self):
        vc = self.bot.cogs.get("VoiceController") if hasattr(self.bot, "cogs") else None
        if vc is not None:
            vc.game_mode = True
        engine = getattr(self.bot, "engine", None)
        if engine and hasattr(engine, "conv_buffer"):
            engine.conv_buffer.game_mode_cap = 0.8

    def _exit_game_mode(self):
        vc = self.bot.cogs.get("VoiceController") if hasattr(self.bot, "cogs") else None
        if vc is not None:
            vc.game_mode = False
        engine = getattr(self.bot, "engine", None)
        if engine and hasattr(engine, "conv_buffer"):
            engine.conv_buffer.game_mode_cap = None

    # ── SFX & TTS ─────────────────────────────────────────────────────────────

    async def _play_sfx(self, name: str) -> None:
        import os
        sfx_path = os.path.join("assets", "sfx", f"{name}.wav")
        if not os.path.exists(sfx_path):
            return
        vc = next((v for v in self.bot.voice_clients if v.is_connected()), None)
        if vc is None or vc.is_playing():
            return
        try:
            vc.play(discord.FFmpegPCMAudio(sfx_path))
        except Exception as e:
            logger.debug(f"[TurtleSoup SFX] {name}: {e}")

    async def _fire_tts(self, vc, text: str) -> None:
        vc._tts_protected = True
        try:
            await vc.play_tts(text, already_in_channel=False, force_macos=True)
        except Exception as e:
            logger.warning(f"[TurtleSoup TTS] failed: {e}")
        finally:
            vc._tts_protected = False

    # ── Embed builders ────────────────────────────────────────────────────────

    def _build_joining_embed(self, session: TurtleSoupSession) -> discord.Embed:
        names = [p.display_name for p in session.players] or ["（無）"]
        e = discord.Embed(
            title="🐢 海龜湯 — 等待玩家加入",
            description=(
                "**規則**\n"
                "• Marvin 知道完整故事（湯底），念給你聽謎題表面（湯面）\n"
                "• 玩家用語音問是非題，Marvin 只回 yes / no / 無關\n"
                "• 想到答案直接喊「答案是 XXX」，喊「我投降」結束\n"
                "• 上限 50 題"
            ),
            color=C_JOINING,
        )
        e.add_field(name="玩家", value=" | ".join(names), inline=False)
        e.set_footer(text="按 Join 加入，或 Start Now 立即開始（35s 自動開始）")
        return e

    def _build_presenting_embed(self, session: TurtleSoupSession) -> discord.Embed:
        puzzle = self._engine.puzzle
        e = discord.Embed(
            title="🐢 海龜湯 — 湯面",
            description=puzzle.surface,
            color=C_PRESENTING,
        )
        e.set_footer(text="Marvin 念完湯面後可開始發問")
        return e

    def _build_asking_embed(self, session: TurtleSoupSession) -> discord.Embed:
        puzzle = self._engine.puzzle
        e = discord.Embed(
            title=f"🐢 海龜湯 — 提問中（{session.questions_count}/{session.max_questions}）",
            description=puzzle.surface,
            color=C_ASKING,
        )
        # 最近 5 個問答
        recent = session.asked_questions[-5:]
        if recent:
            lines = []
            for q in recent:
                emoji = VERDICT_EMOJI.get(q.verdict, "?")
                lines.append(f"{emoji} **{q.asker_name}**: {q.question} → {q.narration}")
            e.add_field(name="最近問答", value="\n".join(lines), inline=False)
        e.set_footer(text="語音/文字皆可；喊「答案是 XXX」嘗試猜答；喊「我投降」結束")
        return e

    def _build_game_over_embed(self, session: TurtleSoupSession) -> discord.Embed:
        puzzle = self._engine.puzzle
        reason = session.end_reason
        if reason == EndReason.WIN:
            title = "🎉 海龜湯 — 你猜對了！"
            color = C_OVER_WIN
        elif reason == EndReason.SURRENDER:
            title = "🏳️ 海龜湯 — 玩家投降"
            color = C_OVER_LOSE
        elif reason == EndReason.EXHAUSTED:
            title = "💀 海龜湯 — 題數用完未猜中"
            color = C_OVER_LOSE
        else:
            title = "🛑 海龜湯 — 已取消"
            color = C_OVER_CANCEL
        e = discord.Embed(
            title=title,
            description=f"**湯底**\n{puzzle.truth}",
            color=color,
        )
        e.add_field(name="湯面", value=puzzle.surface, inline=False)
        e.add_field(name="總提問數", value=str(session.questions_count), inline=True)
        return e

    # ── Message management ────────────────────────────────────────────────────

    async def _post_or_edit(self, embed: discord.Embed, view: Optional[discord.ui.View] = None):
        """貼新訊息或編輯既有。海龜湯只有一條 game_message。"""
        if self._channel is None:
            return
        if self._session and self._session.game_message_id:
            try:
                msg = await self._channel.fetch_message(self._session.game_message_id)
                await msg.edit(embed=embed, view=view)
                return
            except Exception:
                self._session.game_message_id = None
        msg = await self._channel.send(embed=embed, view=view)
        if self._session:
            self._session.game_message_id = msg.id

    # ── State dispatcher ──────────────────────────────────────────────────────

    async def on_state_change(self, session: TurtleSoupSession):
        self._session = session
        state = session.state

        if state == TurtleSoupState.JOINING:
            await self._post_or_edit(self._build_joining_embed(session), TurtleJoinView(self))
            self._enter_game_mode()

        elif state == TurtleSoupState.PRESENTING:
            await self._post_or_edit(self._build_presenting_embed(session))
            # Marvin 念湯面（fire-and-forget，TTS 結束後自動進 ASKING）
            self._spawn(self._present_and_advance())

        elif state == TurtleSoupState.ASKING:
            await self._post_or_edit(self._build_asking_embed(session))

        elif state == TurtleSoupState.GAME_OVER:
            self._cancel_tasks()
            await self._post_or_edit(self._build_game_over_embed(session))
            self._spawn(self._announce_truth_and_cleanup())

    async def _present_and_advance(self):
        """念湯面 TTS → 自動進 ASKING。"""
        vc = self.bot.cogs.get("VoiceController")
        if vc is not None and self._engine and self._engine.puzzle:
            await self._fire_tts(vc, self._engine.puzzle.surface)
        if self._engine and self._session and self._session.state == TurtleSoupState.PRESENTING:
            await self._engine.begin_asking()

    async def _announce_truth_and_cleanup(self):
        """GAME_OVER 時念湯底，然後 cleanup。"""
        vc = self.bot.cogs.get("VoiceController")
        if vc is not None and self._engine and self._engine.puzzle:
            sfx = "fanfare" if self._session.end_reason == EndReason.WIN else "sad_horn"
            await self._play_sfx(sfx)
            # 簡短一句結束台詞，然後念湯底
            opener = {
                EndReason.WIN: "你猜到了！正確答案是——",
                EndReason.SURRENDER: "好吧，我來公布答案：",
                EndReason.EXHAUSTED: "題數用完，正確答案是——",
                EndReason.CANCELLED: "遊戲結束，告訴你答案：",
            }.get(self._session.end_reason, "答案是：")
            await self._fire_tts(vc, opener + self._engine.puzzle.truth)
        self._exit_game_mode()
        # 保留 _engine / _session 一陣子供查看，10 分鐘後自動清空
        await asyncio.sleep(600)
        self._engine = None
        self._session = None
        self._name_to_id.clear()

    # ── STT entry point ───────────────────────────────────────────────────────

    def is_active(self) -> bool:
        return (
            self._session is not None
            and self._session.state in (
                TurtleSoupState.JOINING,
                TurtleSoupState.PRESENTING,
                TurtleSoupState.ASKING,
            )
        )

    async def receive_voice_question_by_speaker(self, speaker: str, text: str) -> bool:
        """STT pipeline 入口。處理玩家語音意圖。回 True 表示已消化（cog 接管）。"""
        if not self._engine or not self._session:
            return False
        if self._session.state != TurtleSoupState.ASKING:
            return False  # 非 ASKING 階段忽略

        intent_result = classify_intent(text)
        intent = intent_result["intent"]
        payload = intent_result["payload"]

        if intent == "ignore":
            return False

        if intent == "surrender":
            await self._engine.surrender(self._resolve_user_id(speaker), speaker)
            return True

        if intent == "final_answer":
            self._spawn(self._handle_final_guess(speaker, payload))
            return True

        if intent == "question":
            # 過載保護
            if self._asking_inflight >= self._MAX_ASKING_INFLIGHT:
                if self._channel:
                    await self._channel.send(
                        f"⚠️ **{speaker}**：Marvin 還在想上一題，請稍等再問。"
                    )
                return False
            self._spawn(self._handle_question(speaker, payload))
            return True

        return False

    def _resolve_user_id(self, speaker: str) -> str:
        """display_name → discord user_id 字串。沒有時用 speaker 當 id。"""
        uid = self._name_to_id.get(speaker)
        return str(uid) if uid else speaker

    async def _handle_question(self, speaker: str, question: str):
        self._asking_inflight += 1
        try:
            result = await self._engine.submit_question(
                self._resolve_user_id(speaker), speaker, question,
            )
            if result is None:
                return
            await self._fire_verdict_sequence(result["verdict"], result["narration"])
            await self._post_or_edit(self._build_asking_embed(self._session))
        except Exception as e:
            logger.error(f"[TurtleSoup] handle_question 失敗: {e}")
        finally:
            self._asking_inflight -= 1

    async def _handle_final_guess(self, speaker: str, player_answer: str):
        try:
            if self._channel:
                await self._channel.send(f"🎯 **{speaker}** 嘗試最終猜答：「{player_answer}」")
            result = await self._engine.submit_final_guess(
                self._resolve_user_id(speaker), speaker, player_answer,
            )
            if result is None:
                return
            vc = self.bot.cogs.get("VoiceController")
            if result["accepted"]:
                # WIN 流程由 on_state_change 接手
                return
            # 駁回：留在 ASKING，播 buzz + Marvin narration
            await self._fire_verdict_sequence("no", result["narration"] or "差一點，繼續想。")
        except Exception as e:
            logger.error(f"[TurtleSoup] handle_final_guess 失敗: {e}")

    async def _fire_verdict_sequence(self, verdict: str, narration: str):
        """SFX → TTS 序列播放（沿 Busted99 SFX chain pattern）。"""
        vc = self.bot.cogs.get("VoiceController")
        if vc is None:
            return
        sfx_name = VERDICT_SFX.get(verdict, "ba_dum_tss")

        async def _chain():
            await self._play_sfx(sfx_name)
            if narration:
                await self._fire_tts(vc, narration)

        self._spawn(_chain())

    # ── Slash commands ────────────────────────────────────────────────────────

    @app_commands.command(name="turtle_soup_start", description="開始一場海龜湯")
    async def turtle_soup_start(self, interaction: discord.Interaction):
        if self._engine is not None and self._session and self._session.state != TurtleSoupState.GAME_OVER:
            await interaction.response.send_message("海龜湯已進行中。", ephemeral=True)
            return

        await interaction.response.send_message("🐢 海龜湯啟動！", ephemeral=True)
        await self._handle_start(interaction.channel)

    @app_commands.command(name="turtle_soup_stop", description="強制中止目前的海龜湯")
    async def turtle_soup_stop(self, interaction: discord.Interaction):
        if self._engine is None:
            await interaction.response.send_message("沒有進行中的海龜湯。", ephemeral=True)
            return
        await self._engine.cancel()
        await interaction.response.send_message("🛑 海龜湯已中止。", ephemeral=True)

    @app_commands.command(name="turtle_soup_show", description="重看當前題目的湯面")
    async def turtle_soup_show(self, interaction: discord.Interaction):
        if self._engine is None:
            await interaction.response.send_message("沒有進行中的海龜湯。", ephemeral=True)
            return
        await interaction.response.send_message(
            f"**湯面**\n{self._engine.puzzle.surface}", ephemeral=True,
        )

    # ── Start helper ──────────────────────────────────────────────────────────

    async def _handle_start(self, channel: Optional[discord.TextChannel]) -> None:
        self._channel = channel
        puzzle = get_default_puzzle()
        session = TurtleSoupSession(
            session_id=str(uuid.uuid4()),
            guild_id=channel.guild.id if channel and hasattr(channel, "guild") else 0,
            channel_id=channel.id if channel else 0,
        )
        self._session = session
        self._engine = TurtleSoupEngine(
            session=session,
            puzzle=puzzle,
            on_state_change=self.on_state_change,
        )
        await self._engine.start_game()


async def setup(bot: commands.Bot):
    await bot.add_cog(TurtleSoupCog(bot))
