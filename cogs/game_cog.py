from __future__ import annotations

import asyncio
import os
import random
import time
import uuid
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from game.engine import GameEngine, ANSWER_MIN_LEN, ANSWER_MAX_LEN, BUZZ_LOCK_SECONDS
from game.session import GameSession, GameState
from game.clue_generator import generate_clue
from game.marvin_player import MarvinPlayer
from game.suki_topic_picker import pick, pick_theme_candidates
from suki_memory import MemoryManager

logger = logging.getLogger(__name__)

C_JOINING   = 0x5865F2
C_CLUE      = 0xFFA500
C_BUZZ      = 0xFF0000
C_CORRECT   = 0x57F287
C_NOBODY    = 0xED4245
C_GAME_OVER = 0xFFD700
C_SPINNER   = 0x9B59B6


# ── Modals ─────────────────────────────────────────────────────────────────────

class SetAnswerModal(discord.ui.Modal, title="Busted — 設定謎底"):
    answer_input = discord.ui.TextInput(
        label="你的答案",
        placeholder=f"請輸入謎底（{ANSWER_MIN_LEN}–{ANSWER_MAX_LEN} 個字）",
        min_length=1,           # 1 而非 ANSWER_MIN_LEN：避免中文 IME 組字中途被 Discord 拒絕
        max_length=ANSWER_MAX_LEN,
    )

    def __init__(self, cog: BustedCog):
        super().__init__()
        self._cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        answer = self.answer_input.value.strip()
        if not (ANSWER_MIN_LEN <= len(answer) <= ANSWER_MAX_LEN):
            await interaction.response.send_message(f"答案必須是 {ANSWER_MIN_LEN}–{ANSWER_MAX_LEN} 個字！", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await self._cog._engine.set_answer(answer)
        await interaction.followup.send("✅ 謎底已設定！線索即將出現…", ephemeral=True)


class Round5AnswerModal(discord.ui.Modal, title="Busted — 第5輪最終答案"):
    answer_input = discord.ui.TextInput(
        label="你的最終猜測",
        placeholder="盡力猜！按字比對得分",
        min_length=1,
        max_length=10,
    )

    def __init__(self, cog: BustedCog):
        super().__init__()
        self._cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user_id = str(interaction.user.id)
        result = await self._cog._engine.submit_round5_answer(user_id, self.answer_input.value.strip())
        pts = result["pts"]
        matched = result["matched"]
        answer_len = result["answer_len"]
        if pts > 0:
            self._cog._round5_display_scores[interaction.user.display_name] = pts
            await interaction.followup.send(
                f"📊 猜對了 **{matched}/{answer_len}** 個字，得 **{pts}** 分！", ephemeral=True
            )
        else:
            await interaction.followup.send(
                f"❌ 猜對了 **{matched}/{answer_len}** 個字，未得分。感謝參與！", ephemeral=True
            )


# ── Views ──────────────────────────────────────────────────────────────────────

class JoinView(discord.ui.View):
    def __init__(self, cog: BustedCog):
        super().__init__(timeout=35)
        self._cog = cog

    @discord.ui.button(label="Join Game 🎮", style=discord.ButtonStyle.primary)
    async def join(self, interaction: discord.Interaction, _button: discord.ui.Button):
        user = interaction.user
        ok = await self._cog._engine.add_player(str(user.id), user.display_name)
        if ok:
            self._cog._name_to_id[user.display_name] = user.id
            await interaction.response.send_message(f"✅ {user.display_name} 加入遊戲！", ephemeral=True)
        else:
            await interaction.response.send_message("遊戲已滿或你已加入", ephemeral=True)

    @discord.ui.button(label="Start Game Now ▶️", style=discord.ButtonStyle.success)
    async def start_now(self, interaction: discord.Interaction, _button: discord.ui.Button):
        session = self._cog._session
        if session is None:
            return
        humans = [p for p in session.players if p.user_id != "marvin"]
        if len(humans) < 1:
            await interaction.response.send_message("至少需要 1 位人類玩家", ephemeral=True)
            return
        await interaction.response.defer()
        await self._cog._engine.start_game()


class SetterInputView(discord.ui.View):
    def __init__(self, cog: BustedCog, setter_id: str):
        super().__init__(timeout=120)
        self._cog = cog
        self._setter_id = setter_id

    @discord.ui.button(label="輸入答案 ✏️", style=discord.ButtonStyle.primary)
    async def input_answer(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if str(interaction.user.id) != self._setter_id:
            await interaction.response.send_message("只有出題人才能設定答案", ephemeral=True)
            return
        await interaction.response.send_modal(SetAnswerModal(self._cog))


class BuzzView(discord.ui.View):
    def __init__(self, cog: BustedCog, disabled: bool = False):
        super().__init__(timeout=None)
        self._cog = cog
        buzz_btn = discord.ui.Button(
            label="BUZZ IN! 🔔",
            style=discord.ButtonStyle.danger,
            disabled=disabled,
            custom_id="busted_buzz",
        )
        buzz_btn.callback = self._on_buzz
        self.add_item(buzz_btn)

        skip_btn = discord.ui.Button(
            label="⏩ 跳過這條線索",
            style=discord.ButtonStyle.secondary,
            custom_id="busted_skip_vote",
        )
        skip_btn.callback = self._on_skip_vote
        self.add_item(skip_btn)

    async def _on_buzz(self, interaction: discord.Interaction):
        engine = self._cog._engine
        if engine is None:
            await interaction.response.defer()
            return
        ok = await engine.buzz_in(str(interaction.user.id))
        if not ok:
            await interaction.response.send_message(
                "❌ 無法搶答（冷卻中、已有人搶答、或你是出題人）", ephemeral=True
            )
        else:
            await interaction.response.defer()

    async def _on_skip_vote(self, interaction: discord.Interaction):
        user_id = str(interaction.user.id)
        await interaction.response.defer(ephemeral=True)
        await self._cog.record_skip_vote(user_id)


class Round5View(discord.ui.View):
    def __init__(self, cog: BustedCog):
        super().__init__(timeout=None)
        self._cog = cog

    @discord.ui.button(
        label="Submit Final Answer 📝",
        style=discord.ButtonStyle.success,
        custom_id="busted_r5",
    )
    async def submit_final(self, interaction: discord.Interaction, _button: discord.ui.Button):
        session = self._cog._session
        if session is None:
            return
        if str(interaction.user.id) == session.current_setter_id:
            await interaction.response.send_message("出題人不能作答", ephemeral=True)
            return
        await interaction.response.send_modal(Round5AnswerModal(self._cog))


class ResultView(discord.ui.View):
    def __init__(self, cog: BustedCog):
        super().__init__(timeout=60)
        self._cog = cog

    @discord.ui.button(label="下一輪 ▶️", style=discord.ButtonStyle.primary)
    async def next_round(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.defer()
        await self._cog._engine.next_round()


class MidGameJoinView(discord.ui.View):
    """Offer a latecomer the chance to join an in-progress game."""

    def __init__(self, cog: BustedCog, target: discord.Member):
        super().__init__(timeout=30)
        self._cog = cog
        self._target = target

    @discord.ui.button(label="加入遊戲 🎮", style=discord.ButtonStyle.primary)
    async def join_mid(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if interaction.user.id != self._target.id:
            await interaction.response.send_message("這個邀請不是給你的", ephemeral=True)
            return
        engine = self._cog._engine
        if engine is None:
            await interaction.response.send_message("遊戲已結束", ephemeral=True)
            return
        ok = await engine.add_player_midgame(str(interaction.user.id), interaction.user.display_name)
        if ok:
            self._cog._name_to_id[interaction.user.display_name] = interaction.user.id
            await interaction.response.send_message(
                f"✅ {interaction.user.display_name} 已加入！下輪開始時輪到你出題。", ephemeral=True
            )
            await self._cog._refresh_current_embed()
            self.stop()
        else:
            await interaction.response.send_message(
                "❌ 無法加入（遊戲滿員或現在不能加入）", ephemeral=True
            )


class ThemeSelectView(discord.ui.View):
    """Three buttons — one per candidate theme. Setter picks the round's topic."""

    def __init__(self, cog: BustedCog, themes: list[str], setter_id: str):
        super().__init__(timeout=150)
        self._cog = cog
        self._setter_id = setter_id
        for theme in themes:
            btn = discord.ui.Button(label=theme, style=discord.ButtonStyle.secondary)
            btn.callback = self._make_callback(theme)
            self.add_item(btn)

    def _make_callback(self, theme: str):
        async def callback(interaction: discord.Interaction):
            if str(interaction.user.id) != self._setter_id:
                await interaction.response.send_message("只有出題人可以選主題", ephemeral=True)
                return
            await interaction.response.defer()
            await self._cog._engine.select_theme(theme)
            self.stop()
        return callback

    async def on_timeout(self):
        # Auto-select the first candidate so the game never stalls
        engine = self._cog._engine
        if engine and engine.session.candidate_themes:
            await engine.select_theme(engine.session.candidate_themes[0])


# ── Cog ────────────────────────────────────────────────────────────────────────

class BustedCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._engine: Optional[GameEngine] = None
        self._session: Optional[GameSession] = None
        self._channel: Optional[discord.TextChannel] = None
        self._tasks: set[asyncio.Task] = set()
        self._marvin: Optional[MarvinPlayer] = None
        self._memory_manager = MemoryManager()
        self._last_result: dict = {}
        self._clue_deadline: float = 0.0
        self._name_to_id: dict[str, int] = {}  # display_name → Discord user_id (int)
        self._marvin_guess_task_ref: Optional[asyncio.Task] = None
        self._game_state: Optional[GameState] = None  # previous state for SFX routing
        self._grace_timers: dict[str, asyncio.Task] = {}  # user_id → pending leave task
        self._round5_display_scores: dict[str, int] = {}  # display_name → pts, for result embed
        self._skip_votes: set[str] = set()               # user_ids who voted to skip current clue

    # ── Task helpers ───────────────────────────────────────────────────────────

    def _cancel_tasks(self):
        for t in list(self._tasks):
            if not t.done():
                t.cancel()
        self._tasks.clear()
        self._marvin_guess_task_ref = None

    def _spawn(self, coro):
        t = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return t

    # ── Sound effects ──────────────────────────────────────────────────────────

    async def _play_sfx(self, name: str) -> None:
        """Play a short WAV sound effect through the active voice client (fire-and-forget)."""
        sfx_path = os.path.join("assets", "sfx", f"{name}.wav")
        if not os.path.exists(sfx_path):
            return
        vc = next((v for v in self.bot.voice_clients if v.is_connected()), None)
        if vc is None:
            return
        if vc.is_playing():
            return  # skip rather than interrupt ongoing audio
        try:
            vc.play(discord.FFmpegPCMAudio(sfx_path))
        except Exception as e:
            logger.debug(f"[SFX] {name}: {e}")

    # ── Embed builders ─────────────────────────────────────────────────────────

    def _scores_line(self, session: GameSession) -> str:
        return " | ".join(f"**{p.display_name}**: {p.score}" for p in session.players)

    def _build_joining_embed(self, session: GameSession) -> discord.Embed:
        names = [p.display_name for p in session.players] or ["（無）"]
        e = discord.Embed(title="🎮 BUSTED — 等待玩家加入", color=C_JOINING)
        e.add_field(name="玩家", value=" | ".join(names), inline=False)
        e.set_footer(text="按 Join Game 加入，或 Start Game Now 立即開始（30秒自動開始）")
        return e

    def _build_clue_embed(self, session: GameSession, countdown: int = 75) -> discord.Embed:
        is_r5 = session.current_round >= 5
        round_label = f"第 {session.current_round}/5 輪線索" + ("（最終輪！）" if is_r5 else "")
        e = discord.Embed(title=f"🎮 BUSTED — {round_label}", color=C_CLUE)

        if session.current_theme:
            e.add_field(name="🎯 本輪主題", value=f"**{session.current_theme}**", inline=True)
        setter = next((p for p in session.players if p.user_id == session.current_setter_id), None)
        e.add_field(name="🎭 出題人", value=setter.display_name if setter else "?", inline=True)
        e.add_field(name="🔐 答案字數", value=f"{len(session.current_answer or '')} 個字", inline=True)
        e.add_field(name="​", value="​", inline=False)

        for i, clue in enumerate(session.current_clues, 1):
            e.add_field(name=f"💡 線索 {i}", value=clue, inline=False)

        if session.wrong_guesses:
            e.add_field(name="❌ 已猜過", value="　".join(session.wrong_guesses), inline=False)

        guesser_pts = {1: 100, 2: 80, 3: 60, 4: 40}.get(session.current_round, "比例")
        setter_pts  = {1: 20,  2: 40, 3: 60, 4: 80}.get(session.current_round, 100)

        if is_r5:
            e.add_field(name="⏱ 最終輪", value=f"所有玩家請提交最終答案！剩餘 **{countdown}s**", inline=False)
        else:
            e.add_field(
                name="⏱ 計時",
                value=f"下一條線索：**{countdown}s** 後 | 猜中得 **{guesser_pts}** 分 | 出題人得 **{setter_pts}** 分",
                inline=False,
            )

        e.add_field(name="📊 積分板", value=self._scores_line(session), inline=False)
        return e

    def _build_buzz_locked_embed(self, session: GameSession) -> discord.Embed:
        holder = next((p for p in session.players if p.user_id == session.buzz_holder_id), None)
        name = holder.display_name if holder else "?"
        e = discord.Embed(
            title=f"⚡ {name} 搶答中！",
            description=f"**{name}** 有 {int(BUZZ_LOCK_SECONDS)} 秒回答（語音或打字）",
            color=C_BUZZ,
        )
        e.add_field(name="📊 積分板", value=self._scores_line(session), inline=False)
        return e

    def _build_setter_input_embed(self, session: GameSession) -> discord.Embed:
        setter = next((p for p in session.players if p.user_id == session.current_setter_id), None)
        name = setter.display_name if setter else "?"
        e = discord.Embed(
            title="🎮 BUSTED — 出題中",
            description=f"🎭 **{name}** 正在設定謎底…\n⏱ 120 秒內請輸入答案",
            color=C_JOINING,
        )
        if session.current_theme:
            e.add_field(name="🎯 本輪主題", value=f"**{session.current_theme}**", inline=False)
        return e

    def _build_result_embed(self, session: GameSession) -> discord.Embed:
        info = self._last_result
        answer      = session.current_answer or "?"
        winner_name = info.get("winner_name")
        setter_pts  = info.get("setter_score", 0)
        guesser_pts = info.get("guesser_score", 0)
        r5_scores   = info.get("round5_scores", {})

        if winner_name:
            color = C_CORRECT
            desc  = f"✅ 答案是：**{answer}**\n🏆 猜中：**{winner_name}** (+{guesser_pts} 分)"
        elif r5_scores:
            lines = [f"**{n}**: +{s} 分" for n, s in r5_scores.items() if s > 0]
            color = C_CORRECT if lines else C_NOBODY
            desc  = f"📊 答案是：**{answer}**\n" + ("\n".join(lines) if lines else "沒有人猜到正確的字…")
        else:
            color = C_NOBODY
            desc  = f"❌ 答案是：**{answer}**\n無人猜中"

        setter = next((p for p in session.players if p.user_id == session.current_setter_id), None)
        setter_name = setter.display_name if setter else "?"
        sign = "+" if setter_pts >= 0 else ""
        desc += f"\n出題人 **{setter_name}**: {sign}{setter_pts} 分"

        e = discord.Embed(title="🎮 BUSTED — 本輪結果", description=desc, color=color)
        e.add_field(name="📊 積分板", value=self._scores_line(session), inline=False)
        return e

    def _build_game_over_embed(self, session: GameSession) -> discord.Embed:
        ranked = sorted(session.players, key=lambda p: p.score, reverse=True)
        medals = ["🥇", "🥈", "🥉"]
        lines  = [
            f"{medals[i] if i < 3 else f'{i+1}.'} **{p.display_name}**: {p.score} 分"
            for i, p in enumerate(ranked)
        ]
        e = discord.Embed(title="🎮 BUSTED — 遊戲結束！", description="\n".join(lines), color=C_GAME_OVER)
        e.set_footer(text="感謝遊玩！用 /busted_start 再來一局")
        return e

    # ── Message management ─────────────────────────────────────────────────────

    async def _post_game_message(
        self,
        embed: discord.Embed,
        view: Optional[discord.ui.View] = None,
    ) -> Optional[discord.Message]:
        if self._channel is None:
            return None
        if self._session and self._session.game_message_id:
            try:
                old = await self._channel.fetch_message(self._session.game_message_id)
                await old.delete()
            except Exception:
                pass
            self._session.game_message_id = None
        msg = await self._channel.send(embed=embed, view=view)
        if self._session:
            self._session.game_message_id = msg.id
        return msg

    async def _edit_game_message(
        self,
        embed: discord.Embed,
        view: Optional[discord.ui.View] = None,
    ):
        if not (self._channel and self._session and self._session.game_message_id):
            return
        try:
            msg = await self._channel.fetch_message(self._session.game_message_id)
            await msg.edit(embed=embed, view=view)
        except Exception:
            pass

    def _get_game_player(self, user_id: str):
        """Return the PlayerState for user_id if they're in the current game, else None."""
        if self._session is None:
            return None
        return next((p for p in self._session.players if p.user_id == user_id), None)

    async def _refresh_current_embed(self) -> None:
        """Re-render the current game embed in-place without triggering state transitions."""
        s = self._session
        if s is None:
            return
        state = s.state
        if state == GameState.CLUE_ACTIVE:
            remaining = max(int(self._clue_deadline - time.time()), 0)
            is_r5 = s.current_round >= 5
            view = Round5View(self) if is_r5 else BuzzView(self, disabled=False)
            await self._edit_game_message(self._build_clue_embed(s, remaining), view)
        elif state == GameState.ROUND_RESULT:
            await self._edit_game_message(self._build_result_embed(s), ResultView(self))
        elif state == GameState.SETTER_INPUT:
            setter_id = s.current_setter_id or ""
            view = SetterInputView(self, setter_id) if setter_id != "marvin" else None
            await self._edit_game_message(self._build_setter_input_embed(s), view)

    # ── Companion bridge hooks (Lane F2) ───────────────────────────────────

    def _scoreboard_payload(self, session: GameSession) -> list[dict]:
        ranked = sorted(session.players, key=lambda p: p.score, reverse=True)
        return [{"user": p.display_name, "score": p.score} for p in ranked]

    def _phase_for_state(self, state: GameState) -> Optional[str]:
        return {
            GameState.JOINING:      "joining",
            GameState.SPINNING:     "spinning",
            GameState.THEME_SELECT: "theme_select",
            GameState.SETTER_INPUT: "setter_input",
            GameState.CLUE_ACTIVE:  "clue_active",
            GameState.BUZZ_LOCKED:  "buzz_locked",
            GameState.ROUND_RESULT: "round_result",
            GameState.GAME_OVER:    "ended",
        }.get(state)

    def _timer_for_state(self, state: GameState) -> Optional[int]:
        return {
            GameState.SETTER_INPUT: 120,
            GameState.CLUE_ACTIVE:  50,
            GameState.BUZZ_LOCKED:  50,
            GameState.ROUND_RESULT: 50,
        }.get(state)

    def _build_last_event(self, session: GameSession) -> str:
        setter = next(
            (p for p in session.players if p.user_id == session.current_setter_id),
            None,
        )
        sname = setter.display_name if setter else "?"
        state = session.state
        if state == GameState.JOINING:
            return f"等待玩家加入（已 {len(session.players)} 人）"
        if state == GameState.SPINNING:
            return "正在抽出本輪出題人"
        if state == GameState.THEME_SELECT:
            return f"{sname} 正在選主題"
        if state == GameState.SETTER_INPUT:
            return f"{sname} 正在設定謎底"
        if state == GameState.CLUE_ACTIVE:
            return f"第 {session.current_round}/5 條線索，出題人 {sname}"
        if state == GameState.BUZZ_LOCKED:
            holder = next(
                (p for p in session.players if p.user_id == session.buzz_holder_id),
                None,
            )
            hname = holder.display_name if holder else "?"
            return f"{hname} 搶答中"
        if state == GameState.ROUND_RESULT:
            info = self._last_result or {}
            winner = info.get("winner_name")
            if winner:
                return f"{winner} 猜中，本輪結束"
            return "本輪無人猜中"
        if state == GameState.GAME_OVER:
            ranked = sorted(session.players, key=lambda p: p.score, reverse=True)
            top = ranked[0] if ranked else None
            return f"遊戲結束 — 冠軍：{top.display_name} {top.score} 分" if top else "遊戲結束"
        return ""

    async def _emit_phase(self, session: GameSession) -> None:
        """若 bridge 在運行，廣播 game_phase_changed。"""
        bridge = getattr(self.bot, "companion_bridge", None)
        if bridge is None or not getattr(bridge, "is_running", False):
            return
        phase = self._phase_for_state(session.state)
        if phase is None:
            return
        setter = next(
            (p for p in session.players if p.user_id == session.current_setter_id),
            None,
        )
        payload = {
            "round": session.round_num,
            "round_total": max(len(session.players), session.round_num),
            "scoreboard": self._scoreboard_payload(session),
            "current_player": setter.display_name if setter else None,
            "timer_seconds": self._timer_for_state(session.state),
            "last_event": self._build_last_event(session),
        }
        try:
            await bridge.emit_game_phase_changed(
                game_name="busted",
                phase=phase,
                payload=payload,
            )
        except Exception as e:
            logger.warning(f"[Busted] emit_game_phase_changed failed: {e}")

    # ── Bridge-callable controls (Lane F2) ─────────────────────────────────

    async def force_skip_round(self) -> None:
        """Companion bridge 呼叫：強制跳過當前回合。"""
        engine = self._engine
        session = self._session
        if engine is None or session is None:
            logger.info("[Busted] force_skip_round 無 active engine，drop")
            return
        try:
            state = session.state
            if state == GameState.SETTER_INPUT:
                await engine.skip_setter_timeout()
            elif state == GameState.CLUE_ACTIVE:
                # 立刻進入結果階段：呼叫 next_round 太激進；用 expire_buzz 不適用。
                # 用 advance_clue 直到結果（最多 5 次）；保守起見呼叫一次足夠 hint。
                if hasattr(engine, "advance_clue"):
                    await engine.advance_clue()
            elif state == GameState.BUZZ_LOCKED:
                await engine.expire_buzz()
            elif state == GameState.ROUND_RESULT:
                await engine.next_round()
            else:
                logger.info(f"[Busted] force_skip_round in state={state}，drop")
        except Exception as e:
            logger.warning(f"[Busted] force_skip_round 失敗: {e}")

    async def end_session(self) -> None:
        """Companion bridge 呼叫：結束目前遊戲。"""
        if self._engine is None:
            logger.info("[Busted] end_session 無 active engine，drop")
            return
        self._cancel_tasks()
        for t in list(self._grace_timers.values()):
            t.cancel()
        self._grace_timers.clear()
        self._engine = None
        self._session = None
        self._game_state = None
        self._name_to_id.clear()
        vc = self.bot.cogs.get("VoiceController") if hasattr(self.bot, "cogs") else None
        if vc is not None:
            vc.game_mode = False
        if self._channel is not None:
            try:
                await self._channel.send("🛑 Busted 已被 companion 端結束。")
            except Exception:
                pass

    # ── Central state dispatcher ───────────────────────────────────────────────

    async def on_state_change(self, session: GameSession):
        prev_state = self._game_state
        self._session = session
        state = session.state

        # Lane F2：先廣播 phase 給 companion bridge（失敗不影響本機 UI）
        await self._emit_phase(session)

        if state == GameState.JOINING:
            await self._play_sfx("fanfare")
            await self._post_game_message(self._build_joining_embed(session), JoinView(self))

        elif state == GameState.SPINNING:
            self._cancel_tasks()
            self._spawn(self._run_spinner(session))

        elif state == GameState.THEME_SELECT:
            setter = next((p for p in session.players if p.user_id == session.current_setter_id), None)
            setter_name = setter.display_name if setter else "出題人"
            embed = discord.Embed(
                title="🎯 選擇本輪主題",
                description=(
                    f"**{setter_name}** 請從以下主題中選一個，\n"
                    "你的謎底必須與這個主題相關！\n\n"
                    "150 秒內未選擇將自動抽選。"
                ),
                color=0x9B59B6,
            )
            if session.current_setter_id == "marvin":
                await self._post_game_message(embed)
                self._spawn(self._marvin_theme_select_task())
            else:
                view = ThemeSelectView(self, session.candidate_themes, session.current_setter_id or "")
                await self._post_game_message(embed, view)

        elif state == GameState.SETTER_INPUT:
            self._round5_display_scores.clear()
            embed = self._build_setter_input_embed(session)
            if session.current_setter_id == "marvin":
                await self._post_game_message(embed)
                self._spawn(self._marvin_setter_task())
            else:
                view = SetterInputView(self, session.current_setter_id or "")
                await self._post_game_message(embed, view)
                self._spawn(self._setter_timeout_task())

        elif state == GameState.CLUE_ACTIVE:
            is_r5 = session.current_round >= 5
            view  = Round5View(self) if is_r5 else BuzzView(self, disabled=False)
            await self._post_game_message(self._build_clue_embed(session), view)

            self._skip_votes.clear()

            if prev_state == GameState.SETTER_INPUT:
                # Fresh setter turn — start a new timer loop
                self._cancel_tasks()
                self._clue_deadline = time.time() + 50.0
                self._spawn(self._clue_loop())
            else:
                # New clue, returning from buzz, or buzz-holder left — reset deadline
                self._clue_deadline = time.time() + 50.0

            # Round 5 early exit: if all guessers have submitted, force the loop to fire now
            if is_r5 and self._engine and self._engine.round5_all_submitted():
                self._clue_deadline = time.time()

            # Trigger Marvin guess — cancel previous think before spawning a new one
            if session.current_setter_id != "marvin" and session.current_clues:
                if self._marvin_guess_task_ref and not self._marvin_guess_task_ref.done():
                    self._marvin_guess_task_ref.cancel()
                self._marvin_guess_task_ref = self._spawn(self._marvin_guess_task())

        elif state == GameState.BUZZ_LOCKED:
            await self._play_sfx("buzz")
            holder = next((p for p in session.players if p.user_id == session.buzz_holder_id), None)
            await self._edit_game_message(self._build_buzz_locked_embed(session), BuzzView(self, disabled=True))
            if holder and self._channel:
                await self._channel.send(
                    f"⚡ **{holder.display_name}** 搶答！請在 **{int(BUZZ_LOCK_SECONDS)} 秒**內回答（語音或文字）"
                )
                self._spawn(self._watch_for_text_answer(holder.user_id, timeout=float(BUZZ_LOCK_SECONDS)))

        elif state == GameState.ROUND_RESULT:
            if prev_state == GameState.BUZZ_LOCKED:
                await self._play_sfx("correct")
            elif prev_state == GameState.CLUE_ACTIVE:
                await self._play_sfx("sad_horn")
            await self._post_game_message(self._build_result_embed(session), ResultView(self))
            self._spawn(self._auto_next_round())

        elif state == GameState.GAME_OVER:
            await self._play_sfx("game_over")
            self._cancel_tasks()
            for t in list(self._grace_timers.values()):
                t.cancel()
            self._grace_timers.clear()
            await self._post_game_message(self._build_game_over_embed(session))
            self._engine = None
            self._session = None
            self._name_to_id.clear()
            self._game_state = None
            # 🎮 離開 game_mode：恢復 Marvin 所有服務
            vc = self.bot.cogs.get("VoiceController")
            if vc is not None:
                vc.game_mode = False
            return  # skip the _game_state update below (already cleared)

        self._game_state = state

    # ── Background tasks ───────────────────────────────────────────────────────

    async def _clue_loop(self):
        """Tick every second; advance clue when deadline passes. Handles buzz windows."""
        last_refresh_bucket = -1  # tracks which 5s bucket we last refreshed
        try:
            while True:
                await asyncio.sleep(1.0)
                session = self._session
                if session is None or session.state == GameState.ROUND_RESULT:
                    return

                # Wait out any active buzz — with a failsafe timeout
                buzz_wait_start = time.time()
                while session.state == GameState.BUZZ_LOCKED:
                    await asyncio.sleep(0.3)
                    # If buzz window somehow never resolves, force-expire after window + 5s margin
                    if time.time() - buzz_wait_start > BUZZ_LOCK_SECONDS + 5.0 and self._engine:
                        await self._engine.expire_buzz()
                        break

                if session.state != GameState.CLUE_ACTIVE:
                    return

                remaining = int(self._clue_deadline - time.time())
                # Refresh embed at each 5s bucket crossing (10s and 5s marks)
                refresh_bucket = remaining // 5
                if refresh_bucket != last_refresh_bucket and remaining in range(1, 73):
                    last_refresh_bucket = refresh_bucket
                    is_r5 = session.current_round >= 5
                    view  = Round5View(self) if is_r5 else BuzzView(self, disabled=False)
                    await self._edit_game_message(self._build_clue_embed(session, max(remaining, 0)), view)

                if time.time() >= self._clue_deadline:
                    if session.current_round >= 5:
                        # Pre-set result so on_state_change(ROUND_RESULT) renders correctly
                        self._last_result = {
                            "round5_scores": dict(self._round5_display_scores)
                        }
                    await self._engine.advance_clue()
                    last_refresh_bucket = -1  # reset for next round
                    await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            pass

    async def _auto_next_round(self):
        """Auto-advance from ROUND_RESULT to the next round after 50 s."""
        try:
            await asyncio.sleep(50)
            if self._session and self._session.state == GameState.ROUND_RESULT and self._engine:
                await self._engine.next_round()
        except asyncio.CancelledError:
            pass

    async def _setter_timeout_task(self):
        """Penalise and skip setter if they don't submit an answer within 120 s."""
        try:
            await asyncio.sleep(120)
            session = self._session
            if session and session.state == GameState.SETTER_INPUT:
                setter = next(
                    (p for p in session.players if p.user_id == session.current_setter_id),
                    None,
                )
                name = setter.display_name if setter else "出題人"
                if self._channel:
                    await self._channel.send(
                        f"⏰ **{name}** 出題時間到！扣 50 分，換下一位出題。"
                    )
                await self._engine.skip_setter_timeout()
        except asyncio.CancelledError:
            pass

    async def record_skip_vote(self, user_id: str) -> None:
        """Record a skip-clue vote. Advances clue immediately when all eligible guessers vote."""
        session = self._session
        if session is None or session.state != GameState.CLUE_ACTIVE:
            return
        setter_id = session.current_setter_id
        # Only human non-setter non-Marvin players are eligible voters
        eligible = [
            p.user_id for p in session.players
            if p.user_id != setter_id and p.user_id != "marvin"
        ]
        if user_id not in eligible:
            return
        self._skip_votes.add(user_id)
        voted = len(self._skip_votes & set(eligible))
        total = len(eligible)
        if self._channel:
            await self._channel.send(
                f"⏩ 跳過票：{voted}/{total}（需全員同意）", delete_after=10
            )
        if voted >= total and self._engine:
            self._skip_votes.clear()
            await self._engine.advance_clue()

    async def _marvin_theme_select_task(self):
        """Marvin 快速自動選主題（1-2.5 秒假裝思考），避免掛在 ThemeSelectView 150 秒。"""
        try:
            await asyncio.sleep(random.uniform(1.0, 2.5))
            session = self._session
            if session is None or session.state != GameState.THEME_SELECT:
                return
            if not session.candidate_themes:
                return
            theme = random.choice(session.candidate_themes)
            if self._channel:
                await self._channel.send(f"**Marvin**: 我選「{theme}」！")
            await self._engine.select_theme(theme)
        except asyncio.CancelledError:
            pass

    async def _marvin_setter_task(self):
        """Marvin uses LLM to generate a theme-related answer."""
        try:
            await asyncio.sleep(random.uniform(1.5, 3.0))
            session = self._session
            if session is None or session.state != GameState.SETTER_INPUT:
                return
            theme = session.current_theme or "宇宙"
            if self._marvin:
                answer = await self._marvin.generate_setter_answer(
                    theme, min_len=ANSWER_MIN_LEN, max_len=ANSWER_MAX_LEN
                )
            else:
                _, answer = pick(self._memory_manager)
                if len(answer) > ANSWER_MAX_LEN:
                    answer = answer[:ANSWER_MAX_LEN]
                if len(answer) < ANSWER_MIN_LEN:
                    answer = "黑洞"
            quip = self._marvin.setter_quip() if self._marvin else "我來出題。"
            if self._channel:
                await self._channel.send(f"**Marvin**: {quip}")
            await self._engine.set_answer(answer)
        except asyncio.CancelledError:
            pass

    async def _marvin_guess_task(self):
        """Marvin considers buzzing (rounds 1-4) or submitting final answer (round 5)."""
        try:
            session = self._session
            if session is None or self._marvin is None:
                return
            if session.current_setter_id == "marvin":
                return

            clues         = list(session.current_clues)
            char_count    = len(session.current_answer or "")
            clue_round    = session.current_round
            wrong_guesses = list(session.wrong_guesses) if clue_round >= 4 else []

            if clue_round >= 5:
                # Round 5: no buzzing — submit via modal path so humans aren't blocked
                self._spawn(self._marvin_round5_submit(clues, char_count, wrong_guesses))
                return

            async def on_buzz_ready(guess: str):
                s = self._session
                if s is None or s.state != GameState.CLUE_ACTIVE:
                    return
                ok = await self._engine.buzz_in("marvin")
                if not ok:
                    return
                if self._channel:
                    await self._channel.send(f"**Marvin**: {guess}")
                result = await self._engine.submit_answer("marvin", guess)
                if result.get("correct") and self._session:
                    self._last_result = {
                        "winner_name":   "Marvin",
                        "guesser_score": result["score"],
                        "setter_score":  result["setter_score"],
                    }
                    await self._edit_game_message(
                        self._build_result_embed(self._session), ResultView(self)
                    )

            await self._marvin.think_then_buzz(clue_round, clues, char_count, wrong_guesses, on_buzz_ready)
        except asyncio.CancelledError:
            pass

    async def _marvin_round5_submit(self, clues: list, char_count: int, wrong_guesses: list):
        """Marvin submits a final answer in round 5 via the partial-score path (no buzz)."""
        try:
            await asyncio.sleep(random.uniform(2.0, 6.0))
            s = self._session
            if s is None or s.current_round < 5 or s.state != GameState.CLUE_ACTIVE:
                return
            guess = await self._marvin.generate_guess(5, clues, char_count, wrong_guesses)
            result = await self._engine.submit_round5_answer("marvin", guess)
            pts = result["pts"]
            matched = result["matched"]
            answer_len = result["answer_len"]
            if self._channel:
                score_text = f"猜對 {matched}/{answer_len} 個字，+{pts} 分" if pts > 0 else f"猜對 {matched}/{answer_len} 個字，0 分"
                await self._channel.send(f"**Marvin** 最終答案：{guess}（{score_text}）")
            if pts > 0:
                self._round5_display_scores["Marvin"] = pts
        except asyncio.CancelledError:
            pass

    async def _marvin_correct_react(self, winner_name: str) -> None:
        """Marvin 對人類猜中答案發表評論（TTS + channel 訊息）。"""
        try:
            if self._marvin is None:
                return
            quip = self._marvin.correct_quip(winner_name)
            if self._channel:
                await self._channel.send(f"**Marvin**: {quip}")
            vc = self.bot.cogs.get("VoiceController")
            if vc is not None:
                try:
                    await vc.play_tts(quip, already_in_channel=True)
                except Exception as e:
                    logger.debug(f"[BustedCog] _marvin_correct_react TTS skipped: {e}")
        except asyncio.CancelledError:
            pass

    async def _watch_for_text_answer(self, buzz_holder_id: str, timeout: float = 3.0):
        """Wait for the buzzer to type an answer within `timeout` seconds."""
        try:
            def check(msg: discord.Message) -> bool:
                return (
                    msg.channel == self._channel
                    and str(msg.author.id) == buzz_holder_id
                    and not msg.author.bot
                )

            msg    = await self.bot.wait_for("message", check=check, timeout=timeout)
            session = self._session
            if session and session.state == GameState.BUZZ_LOCKED:
                result = await self._engine.submit_answer(buzz_holder_id, msg.content.strip())
                if result.get("correct") and self._session:
                    winner = next((p for p in session.players if p.user_id == buzz_holder_id), None)
                    self._last_result = {
                        "winner_name":   winner.display_name if winner else buzz_holder_id,
                        "guesser_score": result["score"],
                        "setter_score":  result["setter_score"],
                    }
                    # on_state_change(ROUND_RESULT) already ran with empty _last_result;
                    # re-render now that we have the correct info
                    await self._edit_game_message(
                        self._build_result_embed(self._session), ResultView(self)
                    )
                else:
                    self._last_result = {}
                    asyncio.get_running_loop().create_task(self._play_sfx("wrong"))
                    matched = result.get("matched_chars", 0)
                    answer_len = result.get("answer_len", 0)
                    if self._channel and answer_len:
                        await self._channel.send(
                            f"❌ 猜錯！猜對了 **{matched}/{answer_len}** 個字", delete_after=8
                        )

        except asyncio.TimeoutError:
            # Answer window expired — release buzz via engine
            asyncio.get_running_loop().create_task(self._play_sfx("wrong"))
            if self._engine:
                await self._engine.expire_buzz()
        except asyncio.CancelledError:
            pass

    # ── Spinner animation ──────────────────────────────────────────────────────

    async def _run_spinner(self, session: GameSession):
        """8-frame animated spinner that lands on the pre-selected setter."""
        try:
            players     = list(session.players)
            names       = [p.display_name for p in players]
            winner_name = next(
                (p.display_name for p in players if p.user_id == session.current_setter_id),
                names[0],
            )

            embed = discord.Embed(title="🎰 BUSTED — 選擇出題人！", color=C_SPINNER)
            msg   = await self._post_game_message(embed)
            if msg is None:
                return

            # Frames 1–8: rotate highlight across names
            for frame in range(8):
                highlighted = names[frame % len(names)]
                lines = [
                    f"▶ **{n}**" if n == highlighted else f"　{n}"
                    for n in names
                ]
                await msg.edit(embed=discord.Embed(
                    title="🎰 BUSTED — 選擇出題人！",
                    description="\n".join(lines),
                    color=C_SPINNER,
                ))
                await asyncio.sleep(0.8)

            # Flash the winner 3 times
            for _ in range(3):
                await msg.edit(embed=discord.Embed(
                    title=f"🎉 {winner_name} 被選中出題！",
                    description=f"🎭 **{winner_name}** 請設定謎底！",
                    color=C_SPINNER,
                ))
                await asyncio.sleep(0.5)
                await msg.edit(embed=discord.Embed(title="🎰 …", color=C_SPINNER))
                await asyncio.sleep(0.4)

            themes = pick_theme_candidates(self._memory_manager, n=3)
            await self._engine.begin_theme_select(themes)

        except asyncio.CancelledError:
            pass

    # ── STT hook ───────────────────────────────────────────────────────────────

    @property
    def active_buzz_holder_id(self) -> int | None:
        """
        Returns the Discord user ID (int) of the current buzz holder during BUZZ_LOCKED,
        or None if no buzz window is open. voice_controller uses this to suppress
        non-holder speech from Marvin's pipeline during the answer window.
        """
        if self._session and self._session.buzz_holder_id:
            try:
                return int(self._session.buzz_holder_id)
            except (TypeError, ValueError):
                return None
        return None

    def should_suppress_for_game(self, speaker: str) -> bool:
        """
        Returns True when a buzz window is open and `speaker` is NOT the holder.
        voice_controller uses this to silently drop non-holder speech so it never
        reaches Marvin's pipeline — no actual muting, just routing suppression.
        """
        if self._session is None or self._session.buzz_holder_id is None:
            return False
        buzz_id = self._session.buzz_holder_id
        holder = next((p for p in self._session.players if p.user_id == buzz_id), None)
        if holder is None:
            return False
        return holder.display_name != speaker

    async def receive_voice_answer(self, user_id: int, text: str, _guild_id: int = 0) -> bool:
        """Called by voice_controller.py after STT transcription."""
        if self._engine is None:
            return False
        return await self._engine.receive_voice_answer(user_id, text)

    async def receive_voice_answer_by_speaker(self, speaker: str, text: str) -> bool:
        """
        Called by voice_controller.py using display_name (not user_id).
        Resolves name → id via the join-time mapping, then delegates to the engine.

        If the speaker is the current buzz holder, echoes their transcribed answer to
        the text channel as "**Name** 搶答：<text>" so all players (and Marvin) can see it.
        Other voice during game (clue phase chatter, non-holder speech) is silently dropped.

        Returns True if the text was consumed as a game answer.
        """
        user_id = self._name_to_id.get(speaker)
        if user_id is None:
            return False

        # Echo to channel only when this person is the active buzz holder
        if (
            self._session is not None
            and self._session.state == GameState.BUZZ_LOCKED
            and self._session.buzz_holder_id == str(user_id)
            and self._channel is not None
        ):
            await self._channel.send(f"**{speaker}** 搶答：{text}")

        result = await self._engine.receive_voice_answer(user_id, text) if self._engine else False
        if isinstance(result, dict) and result.get("correct") and self._session:
            session = self._session
            winner = next((p for p in session.players if p.user_id == str(user_id)), None)
            winner_name = winner.display_name if winner else speaker
            self._last_result = {
                "winner_name":   winner_name,
                "guesser_score": result["score"],
                "setter_score":  result["setter_score"],
            }
            await self._edit_game_message(self._build_result_embed(session), ResultView(self))
            # Marvin 評論猜中（只有人類猜中時）
            if winner_name != "Marvin" and self._marvin:
                self._spawn(self._marvin_correct_react(winner_name))
        return bool(result)

    # ── Clue request hook ──────────────────────────────────────────────────────

    async def _on_clue_request(self, session: GameSession):
        """Invoked by engine when a new clue should be generated and appended."""
        if session.current_answer is None:
            return

        # Show a loading placeholder while LLM generates
        if self._session and self._channel:
            loading_embed = self._build_clue_embed(session)
            loading_embed.add_field(name="💭", value="線索生成中…", inline=False)
            await self._edit_game_message(loading_embed)

        router = getattr(self.bot, "router", None)
        if router is None:
            session.current_clues.append("（線索生成器未連接）")
            await self.on_state_change(session)
            return
        clue = await generate_clue(
            session.current_answer,
            session.current_round,
            list(session.current_clues),
            router,
            theme=session.current_theme,
        )
        session.current_clues.append(clue)
        await self.on_state_change(session)

        # 用不可中斷 TTS 唸出線索
        vc = self.bot.cogs.get("VoiceController")
        if vc is not None:
            announcement = f"線索{session.current_round}：{clue}"
            vc._tts_protected = True
            try:
                await vc.play_tts(announcement, already_in_channel=False)
            finally:
                vc._tts_protected = False

    # ── Mid-game join / leave ──────────────────────────────────────────────────

    def _start_grace_timer(self, member: discord.Member) -> None:
        """Start a 3-second grace window before removing a player who left the voice channel."""
        user_id = str(member.id)
        if user_id in self._grace_timers:
            return  # already counting

        async def _grace():
            try:
                if self._channel:
                    await self._channel.send(
                        f"⏳ **{member.display_name}** 離開頻道，3 秒後移出遊戲…"
                    )
                await asyncio.sleep(3.0)
                if self._engine is None:
                    return
                result = await self._engine.remove_player(user_id)
                action = result.get("action")
                if action == "not_found":
                    return
                if self._channel:
                    await self._channel.send(f"👋 **{member.display_name}** 已離開遊戲。")
                if action == "setter_skipped" and self._channel:
                    await self._channel.send("⏩ 出題人離場，自動進入下一輪。")
                elif action == "removed":
                    # No notify was emitted — manually refresh the embed
                    await self._refresh_current_embed()
            except asyncio.CancelledError:
                pass
            finally:
                self._grace_timers.pop(user_id, None)

        task = asyncio.get_running_loop().create_task(_grace())
        self._grace_timers[user_id] = task

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if self._engine is None or self._session is None:
            return
        if member.bot:
            return
        if member.guild.id != self._session.guild_id:
            return

        bot_vc = next(
            (v for v in self.bot.voice_clients
             if v.is_connected() and v.channel.guild.id == member.guild.id),
            None,
        )
        if bot_vc is None:
            return

        bot_channel = bot_vc.channel
        left  = before.channel == bot_channel and after.channel != bot_channel
        joined = after.channel == bot_channel and before.channel != bot_channel

        user_id = str(member.id)

        if left and self._get_game_player(user_id):
            self._start_grace_timer(member)

        if joined:
            if user_id in self._grace_timers:
                # Player came back within the grace window — cancel removal
                self._grace_timers.pop(user_id).cancel()
                if self._channel:
                    await self._channel.send(f"✅ **{member.display_name}** 回來了！繼續遊戲。")
            elif self._get_game_player(user_id) is None:
                # New person — offer mid-game join
                if (
                    self._channel
                    and self._session.state
                    not in (GameState.GAME_OVER, GameState.JOINING, GameState.SPINNING)
                ):
                    await self._channel.send(
                        f"👋 {member.mention} 遊戲進行中！想加入嗎？",
                        view=MidGameJoinView(self, member),
                    )

    # ── Slash command ──────────────────────────────────────────────────────────

    @app_commands.command(name="busted_start", description="開始一場 Busted 猜謎遊戲")
    async def busted_start(self, interaction: discord.Interaction):
        if self._engine is not None:
            await interaction.response.send_message("遊戲已在進行中！", ephemeral=True)
            return

        self._channel     = interaction.channel
        self._last_result = {}

        session = GameSession(
            session_id=str(uuid.uuid4()),
            guild_id=interaction.guild_id or 0,
            channel_id=interaction.channel_id or 0,
        )
        self._session = session

        router       = getattr(self.bot, "router", None)
        self._marvin = MarvinPlayer(router) if router else None

        self._engine = GameEngine(
            session,
            on_state_change=self.on_state_change,
            clue_fn=self._on_clue_request,
        )

        # Marvin always auto-joins
        await self._engine.add_player("marvin", "Marvin")

        # 🎮 進入 game_mode：暫停 Marvin 所有服務，停止音樂
        vc = self.bot.cogs.get("VoiceController")
        if vc is not None:
            vc.game_mode = True
            if vc.stream_mode:
                await vc.stop_stream(reason="Busted 遊戲開始")
            if vc.radio_mode:
                await vc.stop_radio(reason="Busted 遊戲開始")

        await interaction.response.send_message("🎮 **Busted** 遊戲啟動！Marvin 已進入遊戲模式，暫停所有服務。", ephemeral=True)

        # Auto-start timer: if no human joins in 150 s, start anyway (Marvin vs nobody edge case)
        self._spawn(self._auto_start_timer())

    @app_commands.command(name="busted_stop", description="強制中止目前的 Busted 遊戲（卡住時使用）")
    async def busted_stop(self, interaction: discord.Interaction):
        if self._engine is None:
            await interaction.response.send_message("目前沒有進行中的遊戲。", ephemeral=True)
            return

        self._cancel_tasks()
        for t in list(self._grace_timers.values()):
            t.cancel()
        self._grace_timers.clear()
        self._engine = None
        self._session = None
        self._game_state = None
        self._name_to_id.clear()

        vc = self.bot.cogs.get("VoiceController")
        if vc is not None:
            vc.game_mode = False

        await interaction.response.send_message("🛑 遊戲已強制中止，可以用 `/busted_start` 重新開始。", ephemeral=True)

    async def _auto_start_timer(self):
        """Start the game automatically after 150 s even if no one pressed Start."""
        try:
            await asyncio.sleep(150)
            session = self._session
            if session and session.state == GameState.JOINING:
                await self._engine.start_game()
        except asyncio.CancelledError:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(BustedCog(bot))
