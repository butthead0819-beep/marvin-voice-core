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

from game.busted99.engine import Busted99Engine, parse_number
from game.busted99.llm_engine import Busted99LLMEngine
from game.busted99.voice_parse import extract_guess_via_llm
from game.busted99.session import Busted99Session, Busted99State
from game.busted99.marvin99 import Marvin99, FALLBACK_QUIPS

logger = logging.getLogger(__name__)

C_JOINING  = 0x5865F2
C_PICKING  = 0x9B59B6
C_GUESSING = 0xFF8C00
C_CORRECT  = 0x57F287
C_WRONG    = 0xED4245
C_TIMEOUT  = 0xFFA500
C_GAME_OVER = 0xFFD700

# (channel_message, tts_text)
_MARVIN99_SETTER_QUIPS = [
    ("嗯…好，我想好了！", "我已經設定好秘密數字了。"),
    ("數字選好了。你們有機會的，雖然很小。", "數字選好了，祝你們好運。"),
    ("我挑了一個很有趣的數字。反正你們猜不到。", "我選好了，猜猜看吧。"),
    ("好了，出題完成。這個數字蘊含了宇宙的某種規律。", "出題完成。"),
    ("數字選好了。我對這局充滿…悲觀的期待。", "數字設定完成，遊戲開始。"),
    ("嗯，選好了。別問我選了什麼，問也不說。", "我選好數字了。"),
    ("出題完畢。我選的這個數字，很孤獨。", "出題完畢，請開始猜吧。"),
]


# ── Modals ─────────────────────────────────────────────────────────────────────

class SetNumber99Modal(discord.ui.Modal, title="Busted99 — 設定秘密數字"):
    number_input = discord.ui.TextInput(
        label="輸入 1-99 的整數",
        placeholder="例如：42",
        min_length=1,
        max_length=2,
    )

    def __init__(self, cog: Busted99Cog):
        super().__init__()
        self._cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        text = self.number_input.value.strip()
        n = parse_number(text)
        if n is None:
            await interaction.response.send_message("請輸入 1-99 的整數！", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        ok = await self._cog._engine.set_answer(str(interaction.user.id), n)
        if ok:
            await interaction.followup.send(f"✅ 秘密數字已設定！遊戲即將開始…", ephemeral=True)
        else:
            await interaction.followup.send("❌ 設定失敗（不是你的回合或數字不合法）", ephemeral=True)


# ── Views ──────────────────────────────────────────────────────────────────────

class Join99View(discord.ui.View):
    def __init__(self, cog: Busted99Cog):
        super().__init__(timeout=35)
        self._cog = cog

    @discord.ui.button(label="Join Game 🎮", style=discord.ButtonStyle.primary)
    async def join(self, interaction: discord.Interaction, _button: discord.ui.Button):
        user = interaction.user
        ok = await self._cog._engine.add_player(str(user.id), user.display_name)
        if ok:
            self._cog._name_to_id[user.display_name] = user.id
            await interaction.response.send_message(f"✅ {user.display_name} 加入 Busted99！", ephemeral=True)
            if self._cog._channel and self._cog._session:
                # 用 _post_game_message 刪舊送新，讓 join embed 始終在底部
                await self._cog._post_game_message(
                    self._cog._build_joining_embed(self._cog._session),
                    Join99View(self._cog),
                )
        else:
            await interaction.response.send_message("遊戲已在進行中或你已加入", ephemeral=True)

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

    async def on_timeout(self):
        session = self._cog._session
        if session and session.state == Busted99State.JOINING and self._cog._engine:
            await self._cog._engine.start_game()


class SetterButton99View(discord.ui.View):
    """出題人按鈕：打開 SetNumber99Modal。"""

    def __init__(self, cog: Busted99Cog, setter_id: str):
        super().__init__(timeout=60)
        self._cog = cog
        self._setter_id = setter_id

    @discord.ui.button(label="設定秘密數字 🔢", style=discord.ButtonStyle.primary)
    async def set_number(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if str(interaction.user.id) != self._setter_id:
            await interaction.response.send_message("只有出題人才能設定數字", ephemeral=True)
            return
        await interaction.response.send_modal(SetNumber99Modal(self._cog))

    async def on_timeout(self):
        """出題人 60s 沒設定 → Marvin 代為出題（如果出題人是人類）。"""
        session = self._cog._session
        engine = self._cog._engine
        if (
            session
            and session.state == Busted99State.SETTER_PICKING
            and engine
            and session.setter_id == self._setter_id
        ):
            # 出題人超時：隨機選一個數字代替
            fallback = random.randint(1, 99)
            if self._cog._channel:
                setter = next(
                    (p for p in session.players if p.user_id == self._setter_id), None
                )
                name = setter.display_name if setter else "出題人"
                await self._cog._channel.send(
                    f"⏰ **{name}** 出題時間到！隨機設定秘密數字，遊戲繼續。"
                )
            await engine.set_answer(self._setter_id, fallback)


# ── Play Again View ────────────────────────────────────────────────────────────

class PlayAgainView(discord.ui.View):
    def __init__(self, cog: "Busted99Cog"):
        super().__init__(timeout=120)
        self._cog = cog

    @discord.ui.button(label="再來一局 🔄", style=discord.ButtonStyle.primary)
    async def play_again(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.defer()
        await self._cog._handle_start_game(interaction.channel)

    @discord.ui.button(label="查看分數", style=discord.ButtonStyle.secondary)
    async def view_scores(self, interaction: discord.Interaction, _button: discord.ui.Button):
        session = self._cog._last_session
        if session is None:
            await interaction.response.send_message("沒有記錄", ephemeral=True)
            return
        ranked = sorted(session.players, key=lambda p: p.score, reverse=True)
        lines = [f"**{p.display_name}**: {p.score} 分" for p in ranked]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)


# ── Skip Vote View ─────────────────────────────────────────────────────────────

class SkipVote99View(discord.ui.View):
    """猜題者 AFK 時，其他玩家可投票跳過。"""
    def __init__(self, cog: "Busted99Cog"):
        super().__init__(timeout=None)
        self._cog = cog

    @discord.ui.button(label="⏭ 跳過此輪", style=discord.ButtonStyle.secondary)
    async def skip_vote(self, interaction: discord.Interaction, _button: discord.ui.Button):
        voter_id = str(interaction.user.id)
        session = self._cog._session
        if session is None:
            await interaction.response.send_message("遊戲已結束", ephemeral=True)
            return
        player_ids = {p.user_id for p in session.players}
        if voter_id not in player_ids:
            await interaction.response.send_message("你不是這場遊戲的玩家", ephemeral=True)
            return
        guesser_id = session.current_guesser_id
        if voter_id == guesser_id:
            await interaction.response.send_message("猜題者不能投票跳過自己", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        triggered = await self._cog.record_skip_vote99(voter_id)
        session = self._cog._session  # re-fetch; may change
        non_guesser = [
            p for p in (session.players if session else [])
            if p.user_id != guesser_id and p.user_id != "marvin"
        ]
        total = len(non_guesser)
        current = len(self._cog._skip_votes)
        if triggered:
            await interaction.followup.send("✅ 已達投票門檻，跳過此輪！", ephemeral=True)
        else:
            await interaction.followup.send(
                f"已記錄你的跳過投票（{current}/{total} 票）", ephemeral=True
            )


# ── Cog ────────────────────────────────────────────────────────────────────────

class Busted99Cog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._engine: Optional[Busted99Engine] = None
        self._session: Optional[Busted99Session] = None
        self._channel: Optional[discord.TextChannel] = None
        self._tasks: set[asyncio.Task] = set()
        self._name_to_id: dict[str, int] = {}       # display_name → Discord user_id (int)
        self._guesser_timeout_task: Optional[asyncio.Task] = None
        self._guessing_deadline: float = 0.0
        self._last_session: Optional[Busted99Session] = None
        self._marvin = Marvin99()
        self._skip_votes: set[str] = set()
        self._skip_triggered: bool = False
        self._ws_hub = None  # injected by main_discord after hub starts
        self._player_tokens: dict[str, str] = {}  # token → user_id
        self._prev_state: Optional[Busted99State] = None

    # ── Task helpers ───────────────────────────────────────────────────────────

    def _cancel_tasks(self):
        for t in list(self._tasks):
            if not t.done():
                t.cancel()
        self._tasks.clear()
        self._cancel_guesser_timeout()

    def _spawn(self, coro):
        t = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return t

    async def _fire_tts(self, vc, text: str) -> None:
        """fire-and-forget TTS（遊戲 narration）：
        - force_macos=True 走本機 say（100-300ms 響應，bypass game_mode drop）
        - _tts_protected=True 繞過 silence gate（遊戲主持是 game-critical，
          玩家還在說話時也要播，否則 narration 永遠被吃掉）
        """
        vc._tts_protected = True
        try:
            await vc.play_tts(text, already_in_channel=False, force_macos=True)
        except Exception as e:
            logger.warning(f"[Busted99] TTS failed: {e}")
        finally:
            vc._tts_protected = False

    def _exit_game_mode(self) -> None:
        """VoiceController game_mode=False + 恢復 VAD 溫度上限與 RMS bump。"""
        vc = self.bot.cogs.get("VoiceController") if hasattr(self.bot, "cogs") else None
        if vc is not None:
            vc.game_mode = False
        engine = getattr(self.bot, "engine", None)
        if engine and hasattr(engine, "conv_buffer"):
            engine.conv_buffer.game_mode_cap = None
        if engine and hasattr(engine, "sink") and engine.sink is not None:
            engine.sink.game_mode_rms_bump = 0

    def _cancel_guesser_timeout(self):
        if self._guesser_timeout_task and not self._guesser_timeout_task.done():
            self._guesser_timeout_task.cancel()
        self._guesser_timeout_task = None

    # ── Sound effects ──────────────────────────────────────────────────────────

    async def _play_sfx(self, name: str) -> None:
        sfx_path = os.path.join("assets", "sfx", f"{name}.wav")
        if not os.path.exists(sfx_path):
            return
        vc = next((v for v in self.bot.voice_clients if v.is_connected()), None)
        if vc is None:
            return
        if vc.is_playing():
            return
        try:
            vc.play(discord.FFmpegPCMAudio(sfx_path))
        except Exception as e:
            logger.debug(f"[SFX99] {name}: {e}")

    # ── Embed builders ─────────────────────────────────────────────────────────

    def _scores_line(self, session: Busted99Session) -> str:
        return " | ".join(f"**{p.display_name}**: {p.score}" for p in session.players)

    def _build_joining_embed(self, session: Busted99Session) -> discord.Embed:
        names = [p.display_name for p in session.players] or ["（無）"]
        rules_text = (
            "• 出題人選 1-99 的秘密數字\n"
            "• 其他人輪流用語音猜數字，範圍會縮小\n"
            "• **猜對 = 爆掉（0 分）**，其他人依範圍大小得分\n"
            "• **最後 2 選 1：猜錯得 100 分！** 猜對只會爆掉\n"
            "• 超時扣分，Marvin 也是玩家"
        )
        e = discord.Embed(title="🔢 Busted99 — 等待玩家加入", color=C_JOINING)
        e.add_field(name="📜 規則", value=rules_text, inline=False)
        e.add_field(name="玩家", value=" | ".join(names), inline=False)
        e.set_footer(text="按 Join Game 加入，或 Start Game Now 立即開始（35秒自動開始）")
        return e

    def _build_setter_picking_embed(self, session: Busted99Session) -> discord.Embed:
        setter = next((p for p in session.players if p.user_id == session.setter_id), None)
        name = setter.display_name if setter else "?"
        e = discord.Embed(
            title="🔢 Busted99 — 出題中",
            description=f"🎭 **{name}** 正在設定秘密數字…\n⏱ 60 秒內請輸入",
            color=C_PICKING,
        )
        return e

    def _build_guessing_embed(
        self, session: Busted99Session, remaining: int | None = None
    ) -> discord.Embed:
        guesser = next(
            (p for p in session.players if p.user_id == session.current_guesser_id), None
        )
        name = guesser.display_name if guesser else "?"
        space = session.high_bound - session.low_bound + 1
        is_last = space <= 2

        title = f"🔢 {name} 的回合" + ("  🔐 終極密碼！" if is_last else "")
        e = discord.Embed(title=title, color=C_GUESSING)
        e.add_field(
            name="範圍",
            value=f"**{session.low_bound}** ～ **{session.high_bound}**（剩 {space} 個）",
            inline=False,
        )
        timer_label = f"{remaining} 秒" if remaining is not None else "10 分"
        e.add_field(name="⏱ 限時", value=timer_label, inline=True)
        e.add_field(name="📊 積分板", value=self._scores_line(session), inline=False)
        return e

    def _build_guess_result_embed(
        self,
        session: Busted99Session,
        result: dict,
    ) -> discord.Embed:
        res = result.get("result", "")
        # 優先用 result 內的「剛猜的人」— _advance_guesser 後 session.current_guesser_id 已變
        name = result.get("guesser_name")
        if not name:
            guesser_id = result.get("guesser_id") or session.current_guesser_id
            guesser = next(
                (p for p in session.players if p.user_id == guesser_id), None
            )
            name = guesser.display_name if guesser else "?"

        if res == "bust":
            pts = result.get("score_change", 0)
            e = discord.Embed(
                title=f"💥 {name} 爆了！秘密數字：{session.answer}",
                description=f"{name} 猜中爆掉（**0 分**），其他人各得 **{pts} 分**！",
                color=C_CORRECT,
            )
        elif res == "last_bust":
            setter = next((p for p in session.players if p.user_id == session.setter_id), None)
            setter_name = setter.display_name if setter else "出題人"
            e = discord.Embed(
                title=f"💥 {name} 終極爆了！秘密數字：{session.answer}",
                description=f"{name} 猜中爆掉（**0 分**），**{setter_name}** 及其他人各得 **100 分**！",
                color=C_CORRECT,
            )
        elif res == "last_wrong":
            e = discord.Embed(
                title=f"🎉 {name} 猜錯得分！秘密數字：{session.answer}",
                description=f"2 選 1 猜錯反而安全，{name} 得 **100 分**！",
                color=C_CORRECT,
            )
        elif res == "wrong_low":
            e = discord.Embed(
                title=f"📉 {name} 猜太低",
                description=f"新範圍：**{result['new_low']}** ～ **{result['new_high']}**",
                color=C_WRONG,
            )
        elif res == "wrong_high":
            e = discord.Embed(
                title=f"📈 {name} 猜太高",
                description=f"新範圍：**{result['new_low']}** ～ **{result['new_high']}**",
                color=C_WRONG,
            )
        elif res == "timeout":
            deducted = result.get("deducted", 0)
            # Use timed_out_name from engine result — session.current_guesser_id has already
            # advanced to the next guesser by the time this embed is built.
            timeout_name = result.get("timed_out_name") or name
            e = discord.Embed(
                title=f"⏰ {timeout_name} 超時！",
                description=f"扣 **{deducted}** 分",
                color=C_TIMEOUT,
            )
        else:
            e = discord.Embed(title="🔢 結果", description=str(res), color=C_GUESSING)

        e.add_field(name="📊 積分板", value=self._scores_line(session), inline=False)
        return e

    def _build_game_over_embed(self, session: Busted99Session) -> discord.Embed:
        ranked = sorted(session.players, key=lambda p: p.score, reverse=True)
        medals = ["🥇", "🥈", "🥉"]
        lines = [
            f"{medals[i] if i < 3 else f'{i+1}.'} **{p.display_name}**: {p.score} 分"
            for i, p in enumerate(ranked)
        ]
        e = discord.Embed(
            title="🔢 Busted99 — 遊戲結束！",
            description="\n".join(lines),
            color=C_GAME_OVER,
        )
        if session.answer is not None:
            e.add_field(name="🔑 秘密答案", value=f"**{session.answer}**", inline=True)
        e.set_footer(text="感謝遊玩！用 /busted99_start 再來一局")
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
    ) -> None:
        if not (self._channel and self._session and self._session.game_message_id):
            return
        try:
            msg = await self._channel.fetch_message(self._session.game_message_id)
            await msg.edit(embed=embed, view=view)
        except Exception:
            pass

    async def _upsert_game_message(
        self,
        embed: discord.Embed,
        view: Optional[discord.ui.View] = None,
    ) -> None:
        """Edit the existing game message in place; fall back to post if not found.

        Keeps the game status message at a fixed position in the channel.
        """
        if self._session and self._session.game_message_id and self._channel:
            try:
                msg = await self._channel.fetch_message(self._session.game_message_id)
                await msg.edit(embed=embed, view=view)
                return
            except Exception:
                pass
        await self._post_game_message(embed, view)

    async def record_skip_vote99(self, voter_id: str) -> bool:
        """Record a skip vote from voter_id.

        Returns True if the vote threshold is reached and force_skip_round was triggered.
        Guesser's own ID and 'marvin' are not counted as eligible voters.
        Threshold: all non-guesser human players must vote.
        """
        session = self._session
        if session is None:
            return False
        guesser_id = session.current_guesser_id
        if voter_id == guesser_id or voter_id == "marvin":
            return False
        self._skip_votes.add(voter_id)
        non_guesser_humans = [
            p for p in session.players
            if p.user_id != guesser_id and p.user_id != "marvin"
        ]
        if len(non_guesser_humans) > 0 and len(self._skip_votes) >= len(non_guesser_humans):
            if self._skip_triggered:
                return False  # 已有另一個 concurrent call 正在處理，防止雙觸發
            self._skip_triggered = True
            await self.force_skip_round()
            return True
        return False

    # ── Companion bridge hooks (Lane F2) ───────────────────────────────────

    def _scoreboard_payload(self, session: Busted99Session) -> list[dict]:
        ranked = sorted(session.players, key=lambda p: p.score, reverse=True)
        return [{"user": p.display_name, "score": p.score} for p in ranked]

    def _phase_for_state(self, state: Busted99State) -> Optional[str]:
        return {
            Busted99State.JOINING:         "joining",
            Busted99State.SETTER_PICKING:  "setter_picking",
            Busted99State.GUESSING:        "guessing",
            Busted99State.GAME_OVER:       "ended",
        }.get(state)

    def _timer_for_state(self, state: Busted99State) -> Optional[int]:
        return {
            Busted99State.SETTER_PICKING: 60,
            Busted99State.GUESSING:       600,
        }.get(state)

    def _build_last_event(self, session: Busted99Session) -> str:
        state = session.state
        setter = next((p for p in session.players if p.user_id == session.setter_id), None)
        guesser = next(
            (p for p in session.players if p.user_id == session.current_guesser_id), None
        )
        sname = setter.display_name if setter else "?"
        gname = guesser.display_name if guesser else "?"
        if state == Busted99State.JOINING:
            return f"等待玩家加入（已 {len(session.players)} 人）"
        if state == Busted99State.SETTER_PICKING:
            return f"{sname} 正在設定秘密數字"
        if state == Busted99State.GUESSING:
            return f"{gname} 猜題中，範圍 {session.low_bound}~{session.high_bound}"
        if state == Busted99State.GAME_OVER:
            ranked = sorted(session.players, key=lambda p: p.score, reverse=True)
            top = ranked[0] if ranked else None
            return f"遊戲結束 — 冠軍：{top.display_name} {top.score} 分" if top else "遊戲結束"
        return ""

    async def _emit_phase(self, session: Busted99Session) -> None:
        """若 bridge 在運行，廣播 game_phase_changed。"""
        bridge = getattr(self.bot, "companion_bridge", None)
        if bridge is None or not getattr(bridge, "is_running", False):
            return
        phase = self._phase_for_state(session.state)
        if phase is None:
            return
        # current_player 在 setter_picking 是 setter；guessing 是 guesser；其餘為 None
        cur = None
        if session.state == Busted99State.SETTER_PICKING:
            p = next((p for p in session.players if p.user_id == session.setter_id), None)
            cur = p.display_name if p else None
        elif session.state == Busted99State.GUESSING:
            p = next(
                (p for p in session.players if p.user_id == session.current_guesser_id), None
            )
            cur = p.display_name if p else None
        payload = {
            "round": session.round_num,
            "round_total": max(len(session.players), session.round_num),
            "scoreboard": self._scoreboard_payload(session),
            "current_player": cur,
            "timer_seconds": self._timer_for_state(session.state),
            "last_event": self._build_last_event(session),
        }
        try:
            await bridge.emit_game_phase_changed(
                game_name="busted99",
                phase=phase,
                payload=payload,
            )
        except Exception as e:
            logger.warning(f"[Busted99] emit_game_phase_changed failed: {e}")

    # ── Web UI broadcast ────────────────────────────────────────────────────

    def _build_ws_state(self, session: Busted99Session) -> dict:
        """穩定的 view model — bot 內部怎麼改，只需更新這一層。"""
        guesser = next(
            (p for p in session.players if p.user_id == session.current_guesser_id), None
        )
        setter = next(
            (p for p in session.players if p.user_id == session.setter_id), None
        )
        non_guesser_humans = [
            p for p in session.players
            if p.user_id != session.current_guesser_id and p.user_id != "marvin"
        ]
        remaining = max(0, int(self._guessing_deadline - time.time())) if self._guessing_deadline else 0
        is_over = session.state == Busted99State.GAME_OVER

        # 遊戲結束時要顯示的補充資訊：答案、結果、最後一猜
        last_guesser = None
        if session.last_guess is not None:
            # 用 last_guess_result 找誰是上一個猜的（在 advance_guesser 之前是 current_guesser）
            # 但 advance 後 current 已變，所以結束時 current_guesser_id 是真正的「最後猜題人」
            last_guesser = guesser.display_name if guesser else None

        return {
            "type": "game_state",
            "phase": {
                Busted99State.JOINING:        "joining",
                Busted99State.SETTER_PICKING: "setter_picking",
                Busted99State.GUESSING:       "guessing",
                Busted99State.GAME_OVER:      "game_over",
            }.get(session.state, "unknown"),
            "round": session.round_num,
            "guesser": guesser.display_name if guesser else None,
            "setter": setter.display_name if setter else None,
            "range_low": session.low_bound,
            "range_high": session.high_bound,
            "remaining_sec": remaining,
            "scores": [
                {"name": p.display_name, "score": p.score}
                for p in sorted(session.players, key=lambda p: p.score, reverse=True)
            ],
            "players": [p.display_name for p in session.players],
            "skip_votes": len(self._skip_votes),
            "skip_votes_needed": len(non_guesser_humans),
            # 結束畫面用（其他 phase 為 None / 0）
            "answer": session.answer if is_over else None,
            "last_guess": session.last_guess if is_over else None,
            "last_guesser": last_guesser if is_over else None,
            "last_outcome": session.last_guess_result if is_over else None,
            # 猜題歷史（最多取最近 50 筆，避免 payload 過大）
            "guess_log": list(getattr(session, "guess_log", [])[-50:]),
        }

    async def _emit_ws_state(self, session: Busted99Session) -> None:
        hub = self._ws_hub
        if hub is None or not hub.is_running:
            return
        try:
            await hub.broadcast(self._build_ws_state(session))
        except Exception as e:
            logger.warning(f"[Busted99] ws broadcast failed: {e}")

    # ── Bridge-callable controls (Lane F2) ─────────────────────────────────

    async def force_skip_round(self) -> None:
        """Companion bridge 呼叫：強制跳過當前回合。"""
        engine = self._engine
        session = self._session
        if engine is None or session is None:
            logger.info("[Busted99] force_skip_round 無 active engine，drop")
            return
        try:
            state = session.state
            if state == Busted99State.GUESSING:
                self._cancel_guesser_timeout()
                await engine.timeout_guesser()
            else:
                logger.info(f"[Busted99] force_skip_round in state={state}，drop")
        except Exception as e:
            logger.warning(f"[Busted99] force_skip_round 失敗: {e}")

    async def end_session(self) -> None:
        """Companion bridge 呼叫：結束目前遊戲。"""
        if self._engine is None:
            logger.info("[Busted99] end_session 無 active engine，drop")
            return
        self._cancel_tasks()
        self._engine = None
        self._session = None
        self._name_to_id.clear()
        self._exit_game_mode()
        if self._channel is not None:
            try:
                await self._channel.send("🛑 Busted99 已被 companion 端結束。")
            except Exception:
                pass

    # ── Player token system ────────────────────────────────────────────────

    def _generate_player_token(self, user_id: str) -> str:
        """產生並儲存 user_id 的唯一 token。"""
        token = uuid.uuid4().hex
        self._player_tokens[token] = user_id
        return token

    def resolve_token(self, token: str) -> str | None:
        return self._player_tokens.get(token)

    def _build_player_link(self, user_id: str) -> str:
        token = self._generate_player_token(user_id)
        base = os.getenv("GAME_PUBLIC_URL", "http://localhost:8767")
        # 帶上 display_name 供 client UI 判斷「是不是我」（server 不信這個值，仍以 token 為準）
        name = ""
        if self._session:
            p = next((p for p in self._session.players if p.user_id == user_id), None)
            if p:
                from urllib.parse import quote
                name = f"&name={quote(p.display_name)}"
        return f"{base}?token={token}{name}"

    async def _send_player_links(self, session: Busted99Session) -> None:
        """在 Discord 頻道用 ephemeral 訊息送每位玩家個人連結。"""
        if self._channel is None:
            return
        for player in session.players:
            if player.user_id == "marvin":
                continue
            discord_id = self._name_to_id.get(player.display_name)
            if discord_id is None:
                continue
            link = self._build_player_link(player.user_id)
            try:
                user = self.bot.get_user(discord_id)
                if user:
                    # Discord 普通訊息不渲染 markdown link，URL-encoded query 也常 auto-link 失敗。
                    # 用 Link Button — 100% 可點，不依賴 URL parser
                    view = discord.ui.View()
                    view.add_item(discord.ui.Button(
                        label="🎮 點此進入 Busted99",
                        style=discord.ButtonStyle.link,
                        url=link,
                    ))
                    await user.send(content="你的 Busted99 個人連結：", view=view)
            except Exception as e:
                logger.debug(f"[Busted99] 無法 DM {player.display_name}: {e}")

    async def _handle_web_action(self, action: dict) -> None:
        """瀏覽器 → Bot：處理玩家從 web UI 送來的動作。Bot 是唯一仲裁者。
        Hub 在 dispatch 時已將 token 解析為 resolved_user_id，直接使用。
        """
        t = action.get("type", "")
        user_id = action.get("resolved_user_id")  # token 解析後的 Discord user_id
        if not user_id:
            logger.debug("[Busted99] web action without valid token, drop: type=%s", t)
            return

        if t == "b99_guess":
            engine = self._engine
            session = self._session
            if engine is None or session is None:
                return
            if user_id != session.current_guesser_id:
                # 非當前猜題人 → 靜默丟棄，不打擾 Discord 頻道
                tried = next((p.display_name for p in session.players if p.user_id == user_id), "?")
                logger.debug("[Busted99] web b99_guess from non-guesser %s (not turn), drop", tried)
                return
            number = action.get("number")
            if number is None:
                return
            try:
                guess_int = int(number)
            except (ValueError, TypeError):
                return
            result = await engine.submit_guess(user_id, guess_int)
            if result and self._channel:
                await self._channel.send(
                    embed=self._build_guess_result_embed(session, result)
                )
            # 播 LLM 生的 Marvin 主持台詞（fire-and-forget）
            narration = (result or {}).get("narration", "")
            if narration:
                vc = self.bot.cogs.get("VoiceController")
                if vc is not None:
                    self._spawn(self._fire_tts(vc, narration))

        elif t == "b99_skip_vote":
            if not user_id or self._session is None:
                return
            await self.record_skip_vote99(user_id)

        else:
            logger.debug(f"[Busted99] unknown/unresolved web action: {t}")

    # ── Central state dispatcher ───────────────────────────────────────────────

    async def on_state_change(self, session: Busted99Session):
        self._session = session
        state = session.state
        prev_state = self._prev_state
        self._prev_state = state

        # Lane F2：先廣播 phase 給 companion bridge（失敗不影響本機 UI）
        await self._emit_phase(session)
        # Web UI broadcast（失敗不影響遊戲）
        await self._emit_ws_state(session)

        if state == Busted99State.JOINING:
            self._player_tokens = {}  # 新一局，舊連結失效
            await self._post_game_message(
                self._build_joining_embed(session), Join99View(self)
            )

        elif state == Busted99State.SETTER_PICKING:
            embed = self._build_setter_picking_embed(session)

            if session.setter_id == "marvin":
                await self._upsert_game_message(embed)
                self._spawn(self._marvin_setter_task())
            else:
                view = SetterButton99View(self, session.setter_id or "")
                await self._upsert_game_message(embed, view)

        elif state == Busted99State.GUESSING:
            self._cancel_guesser_timeout()
            self._skip_votes = set()
            self._skip_triggered = False
            # 注入 resolver 到 hub（hub 不直接持有 cog）
            # 第一輪 GUESSING 時送 DM 個人連結（後續輪次連結不變）
            if session.round_num == 1:
                self._spawn(self._send_player_links(session))
            if prev_state == Busted99State.SETTER_PICKING:
                # 出題人剛完成 → 開場音
                await self._play_sfx("air_horn")
            # 其他 GUESSING re-entry：不在此處播 SFX
            # SFX 由 _process_guess (ba_dum_tss/sad_horn) 或 timeout_task (sad_horn) 負責
            self._guessing_deadline = time.time() + 600.0
            # deadline 是在 on_state_change 開頭的 _emit_ws_state 之後才設的，
            # 重 broadcast 一次讓 web UI 看到正確的 remaining_sec
            await self._emit_ws_state(session)
            await self._upsert_game_message(
                self._build_guessing_embed(session, 600),
                SkipVote99View(self),
            )
            # 無論誰猜題，都啟動 timeout 與 countdown（Marvin auto-guess 失敗時有保底）
            self._guesser_timeout_task = self._spawn(self._guesser_timeout_task_coro())
            self._spawn(self._guessing_countdown_loop())
            if session.current_guesser_id == "marvin":
                self._spawn(self._marvin_guesser_task())

        elif state == Busted99State.GAME_OVER:
            self._cancel_tasks()
            # SFX 改由觸發端（_process_guess bust 播 sad_horn / timeout_task 播 sad_horn）負責
            # 在清空前儲存 session，供「查看分數」按鈕使用
            self._last_session = session
            await self._upsert_game_message(
                self._build_game_over_embed(session), PlayAgainView(self)
            )
            self._engine = None
            self._session = None
            self._name_to_id.clear()
            self._exit_game_mode()

    # ── Background tasks ───────────────────────────────────────────────────────

    async def _guesser_timeout_task_coro(self):
        """600 秒後若仍在 GUESSING 狀態，呼叫 timeout_guesser。"""
        try:
            await asyncio.sleep(600.0)
            session = self._session
            engine = self._engine
            if session is None or engine is None:
                return
            if session.state != Busted99State.GUESSING:
                return
            result = await engine.timeout_guesser()
            if self._channel and session:
                await self._channel.send(
                    embed=self._build_guess_result_embed(session, {"result": "timeout", **result})
                )
            await self._play_sfx("sad_horn")
        except asyncio.CancelledError:
            pass

    async def _guessing_countdown_loop(self) -> None:
        """每隔固定時間刷新 guessing embed，顯示剩餘倒數秒數。"""
        try:
            for remaining in (300, 120, 60, 30, 10, 5):
                wait = self._guessing_deadline - remaining - time.time()
                if wait > 0:
                    await asyncio.sleep(wait)
                if (
                    self._session is None
                    or self._session.state != Busted99State.GUESSING
                ):
                    return
                await self._edit_game_message(
                    self._build_guessing_embed(self._session, remaining),
                    SkipVote99View(self),
                )
        except asyncio.CancelledError:
            pass

    async def _marvin_setter_task(self):
        """Marvin 隨機選一個 1-99 的數字並設定。"""
        try:
            await asyncio.sleep(random.uniform(1.0, 2.0))
            session = self._session
            engine = self._engine
            if session is None or engine is None:
                return
            if session.state != Busted99State.SETTER_PICKING:
                return
            number = random.randint(1, 99)
            channel_quip, tts_quip = random.choice(_MARVIN99_SETTER_QUIPS)
            if self._channel:
                await self._channel.send(f"**Marvin**: {channel_quip}")
            # 用 TTS 說出台詞（play_tts 失敗不能卡住遊戲）
            vc = self.bot.cogs.get("VoiceController")
            if vc is not None:
                vc._tts_protected = True
                try:
                    await vc.play_tts(tts_quip, already_in_channel=False, force_macos=True)
                except Exception as e:
                    logger.warning(f"[Busted99] Marvin TTS failed (continuing): {e}")
                finally:
                    vc._tts_protected = False
            await engine.set_answer("marvin", number)
        except asyncio.CancelledError:
            pass

    async def _marvin_guesser_task(self):
        """Marvin 當猜題人時，並行預生成垃圾話並自動猜一個數字。"""
        try:
            session = self._session
            if session is None:
                return

            # 立刻開始生成垃圾話（在 delay 期間並行）
            scores = {p.display_name: p.score for p in session.players}
            leader = max(scores, key=lambda k: scores[k]) if scores else "未知"
            space = session.high_bound - session.low_bound + 1
            context = {
                "scores": scores,
                "leader": leader,
                "current_guesser": "Marvin",
                "low_bound": session.low_bound,
                "high_bound": session.high_bound,
                "space": space,
                "is_last_chance": space <= 2,
                "round_num": session.round_num,
            }
            trash_talk_task = asyncio.create_task(
                self._marvin.generate_trash_talk(context)
            )

            # 同時等待 delay（縮短：narration 已經替代了「Marvin 想想中」的氣氛）
            delay = random.uniform(0.5, 1.5)
            await asyncio.sleep(delay)

            # 再次確認遊戲狀態沒有改變
            if (
                self._session is None
                or self._session.state != Busted99State.GUESSING
                or self._session.current_guesser_id != "marvin"
            ):
                trash_talk_task.cancel()
                return

            engine = self._engine
            if engine is None:
                trash_talk_task.cancel()
                return

            session = self._session

            # 決定要猜的數字（在發訊息前確定，確保 channel 訊息和實際猜測一致）
            # 終極密碼規則：space > 2 時不猜邊界
            space = session.high_bound - session.low_bound + 1
            if space > 2:
                number = random.randint(session.low_bound + 1, session.high_bound - 1)
            else:
                number = random.randint(session.low_bound, session.high_bound)

            # 取預先生成的垃圾話（最多再等 2 秒）
            try:
                trash_talk = await asyncio.wait_for(
                    asyncio.shield(trash_talk_task), timeout=2.0
                )
            except (asyncio.TimeoutError, Exception):
                trash_talk = random.choice(FALLBACK_QUIPS)
                trash_talk_task.cancel()

            # 播垃圾話 TTS（失敗不卡遊戲）
            vc = self.bot.cogs.get("VoiceController")
            if vc is not None:
                try:
                    await vc.play_tts(trash_talk, already_in_channel=False, force_macos=True)
                except Exception as e:
                    logger.warning(f"[Busted99] Marvin guesser TTS failed (continuing): {e}")

            if self._channel:
                await self._channel.send(f"**Marvin** 🤖：{trash_talk}")

            result = await engine.submit_guess("marvin", number)
            # 播 LLM 生的 Marvin 主持台詞（fire-and-forget，不阻塞下一輪）
            narration = (result or {}).get("narration", "")
            if narration and vc is not None:
                self._spawn(self._fire_tts(vc, narration))
            if result and self._channel and session:
                await self._channel.send(
                    embed=self._build_guess_result_embed(session, result)
                )
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[Marvin guesser] {e}")

    async def _auto_start_timer(self):
        """35 秒後若仍在 JOINING，自動開始。"""
        try:
            await asyncio.sleep(35)
            session = self._session
            if session and session.state == Busted99State.JOINING and self._engine:
                await self._engine.start_game()
        except asyncio.CancelledError:
            pass

    # ── STT hook ───────────────────────────────────────────────────────────────

    def should_suppress_for_game(self, speaker: str) -> bool:
        """
        GUESSING 狀態時，只允許 current_guesser 說話；其他回 False。
        """
        if self._session is None or self._session.state != Busted99State.GUESSING:
            return False
        guesser_id = self._session.current_guesser_id
        if guesser_id is None:
            return False
        guesser = next(
            (p for p in self._session.players if p.user_id == guesser_id), None
        )
        if guesser is None:
            return False
        return guesser.display_name != speaker

    def should_suppress_for_game_by_id(self, user_id: int) -> bool:
        """
        同 should_suppress_for_game，但以 Discord user_id（int）查詢。
        供 STT dispatch 層在取得 display_name 之前做早期過濾，節省 STT inflight 容量。
        """
        if self._session is None or self._session.state != Busted99State.GUESSING:
            return False
        guesser_id = self._session.current_guesser_id
        if guesser_id is None:
            return False
        return str(user_id) != str(guesser_id)

    async def receive_voice_answer_by_speaker(self, speaker: str, text: str) -> bool:
        """
        只處理當前猜題人的語音。非猜題人靜默忽略，不送 LLM、不給 channel feedback。

        流程：
          1. STT 塞車檢查：inflight >= MAX → 通知 channel + 回 False
          2. speaker 驗證：非猜題人靜默回 False
          3. parse_number：成功 → 立刻 TTS echo "X猜N"
          4. 若 regex 失敗 → extract_guess_via_llm（2s 超時）
          5. 仍失敗 → TTS/channel 提示用鍵盤，回 False
          6. 成功 → _process_guess
        """
        if self._engine is None or self._session is None:
            return False
        if self._session.state != Busted99State.GUESSING:
            return False

        # ── 1. STT 塞車檢查 ────────────────────────────────────────────────
        engine_stt = getattr(self.bot, "engine", None)
        if engine_stt is not None:
            inflight = getattr(engine_stt, "_full_stt_inflight", 0)
            max_inflight = getattr(engine_stt, "_MAX_FULL_STT_INFLIGHT", 3)
            if inflight >= max_inflight:
                logger.warning("[Busted99] STT 塞車 inflight=%d, 跳過語音猜題", inflight)
                if self._channel:
                    await self._channel.send(
                        f"⚠️ **{speaker}**：語音系統排隊中，請直接打字輸入 "
                        f"{self._session.low_bound}～{self._session.high_bound} 的數字"
                    )
                return False

        # ── 2. 驗 speaker，不是猜題人直接放行給其他系統 ──────────────────
        cur_id = self._session.current_guesser_id
        cur = next((p for p in self._session.players if p.user_id == cur_id), None)
        if cur is None or cur.display_name != speaker:
            return False

        low, high = self._session.low_bound, self._session.high_bound

        # ── 3. 快速 regex parse ────────────────────────────────────────────
        number = parse_number(text)

        if number is not None:
            # 立刻 TTS echo（fire-and-forget，不等 LLM）
            vc = self.bot.cogs.get("VoiceController")
            if vc is not None:
                self._spawn(self._fire_tts(vc, f"{speaker}猜{number}"))
        else:
            # ── 4. LLM fallback（2s 超時）────────────────────────────────
            try:
                number = await asyncio.wait_for(
                    extract_guess_via_llm(text, low, high),
                    timeout=2.0,
                )
            except asyncio.TimeoutError:
                number = None

            # ── 5. 仍失敗 → 提示鍵盤 ─────────────────────────────────────
            if number is None:
                logger.info("[Busted99] 語音無法解析數字: %r → 提示打字", text)
                vc = self.bot.cogs.get("VoiceController")
                if vc is not None:
                    self._spawn(self._fire_tts(vc, f"{speaker}，請打字輸入數字"))
                if self._channel:
                    await self._channel.send(
                        f"⌨️ **{speaker}**：語音沒聽到數字，"
                        f"請在此輸入 {low}～{high} 的整數"
                    )
                return False

        # ── 6. 送出猜測 ────────────────────────────────────────────────────
        logger.info("[Busted99] voice guess: %r → %d (text=%r)", speaker, number, text)
        await self._process_guess(cur.display_name, cur_id, number)
        return True

    async def _process_guess(
        self,
        guesser_display_name: str,
        guesser_id: str,
        number: int,
    ) -> tuple[bool, str]:
        """
        共用猜數字處理核心：驗證狀態、呼叫 submit_guess、送出回饋。
        回傳 (ok: bool, result_code: str)。
        result_code 是引擎原始結果碼（wrong_low / out_of_range / boundary…）。
        """
        if self._engine is None or self._session is None:
            return False, "error_no_engine"
        if self._session.state != Busted99State.GUESSING:
            return False, "error_not_guessing"

        cur_id = self._session.current_guesser_id
        if cur_id != guesser_id:
            expected = next(
                (p.display_name for p in self._session.players if p.user_id == cur_id),
                "某位玩家",
            )
            if self._channel:
                await self._channel.send(f"現在輪到 **{expected}** 猜題，不是你喔！")
            return False, "error_wrong_guesser"

        result = await self._engine.submit_guess(guesser_id, number)
        if result is None:
            return False, "error_system"

        res = result.get("result")
        s = self._session
        if res == "out_of_range":
            if self._channel:
                await self._channel.send(
                    f"超出範圍！請猜 {s.low_bound}～{s.high_bound} 之間的數字。"
                )
            return False, "out_of_range"
        if res == "boundary":
            if self._channel:
                await self._channel.send(
                    f"不可以猜邊界（{s.low_bound} 或 {s.high_bound}）！請重新猜。"
                )
            return False, "boundary"

        if self._channel and self._session:
            await self._channel.send(
                embed=self._build_guess_result_embed(self._session, result)
            )
            if self._session.state == Busted99State.GUESSING:
                remaining = max(1, int(self._guessing_deadline - time.time()))
                await self._upsert_game_message(
                    self._build_guessing_embed(self._session, remaining),
                    SkipVote99View(self),
                )
        narration = result.get("narration", "")
        vc = self.bot.cogs.get("VoiceController")
        if vc is not None:
            if res in ("wrong_low", "wrong_high"):
                # ba_dum_tss → 範圍 TTS → narration，同一個 task 序列播放
                low, high = self._session.low_bound, self._session.high_bound
                range_text = random.choice([
                    f"範圍縮小，{low} 到 {high}",
                    f"現在可猜 {low} 到 {high}",
                    f"{low} 到 {high}",
                    f"縮小到 {low} 到 {high}",
                ])

                async def _sfx_range_narration(vc=vc, rt=range_text, nt=narration):
                    await self._play_sfx("ba_dum_tss")
                    await self._fire_tts(vc, rt)
                    if nt:
                        await self._fire_tts(vc, nt)

                self._spawn(_sfx_range_narration())
            elif res in ("bust", "last_bust", "last_wrong"):
                # sad_horn → narration（遊戲結果）
                async def _sfx_bust_narration(vc=vc, nt=narration):
                    await self._play_sfx("sad_horn")
                    if nt:
                        await self._fire_tts(vc, nt)

                self._spawn(_sfx_bust_narration())
            elif narration:
                self._spawn(self._fire_tts(vc, narration))

        logger.info("[Busted99] guess: %r guessed %d → %s", guesser_display_name, number, res)
        return True, res

    # ── Internal start helper ──────────────────────────────────────────────────

    async def _handle_start_game(self, channel: Optional[discord.TextChannel]) -> None:
        """建立新 session 並開始 Joining 階段。供 /busted99_start 和「再來一局」按鈕共用。"""
        if self._engine is not None:
            # 已有遊戲進行中，不重複開始
            return

        self._channel = channel

        session = Busted99Session(
            session_id=str(uuid.uuid4()),
            guild_id=channel.guild.id if channel and hasattr(channel, "guild") else 0,
            channel_id=channel.id if channel else 0,
        )
        self._session = session

        engine_cls = (
            Busted99LLMEngine
            if os.environ.get("BUSTED99_LLM", "").lower() == "true"
            else Busted99Engine
        )
        self._engine = engine_cls(
            session=session,
            on_state_change=self.on_state_change,
            db_path="marvin.db",
        )

        # Marvin 自動加入
        await self._engine.add_player("marvin", "Marvin")

        # 進入 game_mode，同時壓低 VAD 靜默門檻避免短數字音訊被高溫對話丟棄
        # 並拉高 RMS floor 過濾雜訊（cough、鍵盤、遠端閒聊）
        vc = self.bot.cogs.get("VoiceController")
        if vc is not None:
            vc.game_mode = True
        engine = getattr(self.bot, "engine", None)
        if engine and hasattr(engine, "conv_buffer"):
            engine.conv_buffer.game_mode_cap = 0.8
        if engine and hasattr(engine, "sink") and engine.sink is not None:
            engine.sink.game_mode_rms_bump = 250

        # 顯示 join embed + 35s 自動開始
        await self._post_game_message(self._build_joining_embed(session), Join99View(self))
        self._spawn(self._auto_start_timer())

    # ── Slash commands ─────────────────────────────────────────────────────────

    @app_commands.command(name="busted99_start", description="開始一場 Busted99 猜數字遊戲")
    async def busted99_start(self, interaction: discord.Interaction):
        if self._engine is not None:
            await interaction.response.send_message("Busted99 遊戲已在進行中！", ephemeral=True)
            return

        await interaction.response.send_message(
            "🔢 **Busted99** 遊戲啟動！猜 1-99 的秘密數字，Marvin 已加入。",
            ephemeral=True,
        )

        await self._handle_start_game(interaction.channel)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """
        當猜題中，攔截當前猜題玩家在聊天室輸入的數字，當作猜題送出。
        非數字訊息或非猜題玩家的訊息一律忽略，不影響正常對話。
        """
        if message.author.bot:
            return
        if self._engine is None or self._session is None:
            return
        if self._session.state != Busted99State.GUESSING:
            return

        guesser_id = self._session.current_guesser_id
        guesser = next(
            (p for p in self._session.players if p.user_id == guesser_id), None
        )
        if guesser is None or guesser_id != str(message.author.id):
            return

        number = parse_number(message.content)
        if number is None:
            return  # 非數字訊息，靜默忽略

        logger.debug(
            "[Busted99] on_message: %r typed %r → number=%d",
            message.author.display_name, message.content, number,
        )
        await self._process_guess(guesser.display_name, guesser_id, number)

    @app_commands.command(name="busted99_stop", description="強制中止目前的 Busted99 遊戲")
    async def busted99_stop(self, interaction: discord.Interaction):
        if self._engine is None:
            await interaction.response.send_message("目前沒有進行中的 Busted99 遊戲。", ephemeral=True)
            return

        self._cancel_tasks()
        self._engine = None
        self._session = None
        self._name_to_id.clear()

        self._exit_game_mode()

        await interaction.response.send_message(
            "🛑 Busted99 已強制中止，可以用 `/busted99_start` 重新開始。",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Busted99Cog(bot))
