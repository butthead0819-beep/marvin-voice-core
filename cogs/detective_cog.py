from __future__ import annotations

import asyncio
import logging
import random
import uuid
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from game.detective.engine import DetectiveEngine
from game.detective.session import DetectiveSession, DetectiveState
from game.detective.marvin_detective import MarvinDetective

logger = logging.getLogger(__name__)

# ── 顏色常數 ────────────────────────────────────────────────────────────────────

C_JOINING   = 0x5865F2
C_DECLARING = 0x9B59B6
C_VOTING    = 0xFF8C00
C_REVEALING = 0x57F287
C_GAME_OVER = 0xFFD700


# ── Modals ──────────────────────────────────────────────────────────────────────

class DeclareModal(discord.ui.Modal, title="謊言偵探 — 輸入你的三句話"):
    stmt_a = discord.ui.TextInput(
        label="陳述 A（說一件真實的事）",
        placeholder="例如：我曾經去日本旅遊",
        max_length=80,
    )
    stmt_b = discord.ui.TextInput(
        label="陳述 B（說一件真實的事）",
        placeholder="例如：我不會游泳",
        max_length=80,
    )
    stmt_c = discord.ui.TextInput(
        label="陳述 C（說一件真實或虛假的事）",
        placeholder="例如：我養了一隻貓",
        max_length=80,
    )
    lie_choice = discord.ui.TextInput(
        label="哪句是謊言？（輸入 A、B 或 C）",
        placeholder="輸入 A、B 或 C",
        min_length=1,
        max_length=1,
    )

    def __init__(self, cog: "DetectiveCog", declarer_id: str):
        super().__init__()
        self._cog = cog
        self._declarer_id = declarer_id

    async def on_submit(self, interaction: discord.Interaction):
        choice = self.lie_choice.value.strip().upper()
        if choice not in ("A", "B", "C"):
            await interaction.response.send_message(
                "謊言選項只能是 A、B 或 C！請重新點擊按鈕輸入。", ephemeral=True
            )
            return

        lie_index = {"A": 0, "B": 1, "C": 2}[choice]
        await interaction.response.defer(ephemeral=True)

        engine = self._cog._engine
        if engine is None:
            await interaction.followup.send("遊戲已結束。", ephemeral=True)
            return

        ok = await engine.submit_statements(
            self._declarer_id,
            self.stmt_a.value.strip(),
            self.stmt_b.value.strip(),
            self.stmt_c.value.strip(),
            lie_index,
        )
        if ok:
            await interaction.followup.send("✅ 陳述已提交，等待大家投票！", ephemeral=True)
        else:
            await interaction.followup.send("❌ 提交失敗（不是你的回合或遊戲狀態不對）", ephemeral=True)


# ── Views ───────────────────────────────────────────────────────────────────────

class JoinDetectiveView(discord.ui.View):
    def __init__(self, cog: "DetectiveCog"):
        super().__init__(timeout=None)
        self._cog = cog

    @discord.ui.button(label="加入遊戲 🕵️", style=discord.ButtonStyle.primary)
    async def join(self, interaction: discord.Interaction, _button: discord.ui.Button):
        user = interaction.user
        engine = self._cog._engine
        if engine is None:
            await interaction.response.send_message("遊戲已結束。", ephemeral=True)
            return

        ok = await engine.add_player(str(user.id), user.display_name)
        if ok:
            await interaction.response.send_message(
                f"✅ {user.display_name} 加入謊言偵探！", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "遊戲已在進行中或你已加入。", ephemeral=True
            )

    @discord.ui.button(label="開始遊戲 ▶️", style=discord.ButtonStyle.success)
    async def start_now(self, interaction: discord.Interaction, _button: discord.ui.Button):
        session = self._cog._session
        if session is None:
            await interaction.response.send_message("目前沒有進行中的遊戲。", ephemeral=True)
            return

        humans = [p for p in session.players if p.user_id != "marvin"]
        if len(humans) < 3:
            await interaction.response.send_message(
                "至少需要 3 位人類玩家才能開始！", ephemeral=True
            )
            return

        await interaction.response.defer()
        engine = self._cog._engine
        if engine is not None:
            await engine.start_game()


class DeclareView(discord.ui.View):
    def __init__(self, cog: "DetectiveCog", declarer_id: str):
        super().__init__(timeout=None)
        self._cog = cog
        self._declarer_id = declarer_id

    @discord.ui.button(label="輸入我的陳述 📝", style=discord.ButtonStyle.primary)
    async def declare(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if str(interaction.user.id) != self._declarer_id:
            await interaction.response.send_message(
                "⚠️ 現在不是你的回合，請等待輪到你當陳述者。", ephemeral=True
            )
            return
        await interaction.response.send_modal(
            DeclareModal(self._cog, self._declarer_id)
        )


class VoteView(discord.ui.View):
    def __init__(self, cog: "DetectiveCog", declarer_id: str):
        super().__init__(timeout=None)
        self._cog = cog
        self._declarer_id = declarer_id

    async def _handle_vote(
        self, interaction: discord.Interaction, vote_index: int, label: str
    ):
        voter_id = str(interaction.user.id)

        if voter_id == self._declarer_id:
            await interaction.response.send_message(
                "⚠️ 你是陳述者，不能投票！", ephemeral=True
            )
            return

        engine = self._cog._engine
        if engine is None:
            await interaction.response.send_message("遊戲已結束。", ephemeral=True)
            return

        result = await engine.submit_vote(voter_id, vote_index)

        if "error" in result:
            await interaction.response.send_message(
                f"❌ {result['error']}", ephemeral=True
            )
            return

        if result.get("already_voted"):
            await interaction.response.send_message(
                "你已經投過票了，票已登記！不能更改。", ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"✅ 你投了「{label} 是謊言」，等待其他人投票…", ephemeral=True
        )

        # 更新 embed 顯示投票進度
        session = self._cog._session
        if session and self._cog._channel and session.game_message_id:
            voted = sum(
                1 for p in session.players
                if p.user_id != session.current_declarer_id and p.vote is not None
            )
            try:
                msg = await self._cog._channel.fetch_message(session.game_message_id)
                await msg.edit(embed=self._cog._build_voting_embed(session, voted))
            except Exception:
                pass

        # 若所有人都投完，直接關閉投票
        if result.get("all_voted"):
            self._cog._cancel_vote_timeout()
            close_result = await engine.close_voting()
            # close_voting 失敗（例如重複呼叫）時不繼續
            if "error" in close_result:
                logger.warning(f"[Detective] close_voting returned error: {close_result}")

    @discord.ui.button(label="A 是謊言", style=discord.ButtonStyle.danger)
    async def vote_a(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._handle_vote(interaction, 0, "A")

    @discord.ui.button(label="B 是謊言", style=discord.ButtonStyle.danger)
    async def vote_b(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._handle_vote(interaction, 1, "B")

    @discord.ui.button(label="C 是謊言", style=discord.ButtonStyle.danger)
    async def vote_c(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._handle_vote(interaction, 2, "C")


class PlayAgainView(discord.ui.View):
    def __init__(self, cog: "DetectiveCog"):
        super().__init__(timeout=120)
        self._cog = cog

    @discord.ui.button(label="再來一局 🔄", style=discord.ButtonStyle.primary)
    async def play_again(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.defer()
        await self._cog._handle_start_game(interaction.channel)


# ── Cog ─────────────────────────────────────────────────────────────────────────

class DetectiveCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._engine: Optional[DetectiveEngine] = None
        self._session: Optional[DetectiveSession] = None
        self._channel: Optional[discord.TextChannel] = None
        self._tasks: set[asyncio.Task] = set()
        self._declare_timeout_task: Optional[asyncio.Task] = None
        self._vote_timeout_task_handle: Optional[asyncio.Task] = None
        self._marvin = MarvinDetective()

    # ── Task helpers ───────────────────────────────────────────────────────────

    def _cancel_tasks(self):
        for t in list(self._tasks):
            if not t.done():
                t.cancel()
        self._tasks.clear()
        self._cancel_declare_timeout()
        self._cancel_vote_timeout()

    def _spawn(self, coro) -> asyncio.Task:
        t = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return t

    def _cancel_declare_timeout(self):
        if self._declare_timeout_task and not self._declare_timeout_task.done():
            self._declare_timeout_task.cancel()
        self._declare_timeout_task = None

    def _cancel_vote_timeout(self):
        if self._vote_timeout_task_handle and not self._vote_timeout_task_handle.done():
            self._vote_timeout_task_handle.cancel()
        self._vote_timeout_task_handle = None

    # ── Embed builders ─────────────────────────────────────────────────────────

    def _id_to_name(self, session: DetectiveSession, user_id: str) -> str:
        p = next((p for p in session.players if p.user_id == user_id), None)
        return p.display_name if p else user_id

    def _scores_line(self, session: DetectiveSession) -> str:
        return " | ".join(
            f"**{p.display_name}**: {p.score}" for p in session.players
        )

    def _build_joining_embed(self, session: DetectiveSession) -> discord.Embed:
        names = [p.display_name for p in session.players] or ["（無）"]
        rules_text = (
            "• 每輪一位陳述者說三句話，其中**兩真一假**\n"
            "• 其他人猜哪句是謊言\n"
            "• 猜中得 **+50 分**，騙到別人每人 **+30 分**\n"
            "• 所有人輪流當陳述者後遊戲結束\n"
            "• Marvin 也是玩家！"
        )
        e = discord.Embed(title="🕵️ 謊言偵探", color=C_JOINING)
        e.add_field(name="📜 規則", value=rules_text, inline=False)
        e.add_field(name="玩家列表", value=" | ".join(names), inline=False)
        e.set_footer(text="按「加入遊戲」加入，集滿 3 人後可按「開始遊戲」")
        return e

    def _build_declaring_embed(self, session: DetectiveSession) -> discord.Embed:
        declarer = next(
            (p for p in session.players if p.user_id == session.current_declarer_id),
            None,
        )
        name = declarer.display_name if declarer else "?"
        total = len(session.players)

        e = discord.Embed(
            title=f"📝 {name} 正在想三句話…",
            color=C_DECLARING,
        )
        e.add_field(
            name="目前陳述者",
            value=f"**{name}**（第 {session.round_num}/{total} 輪）",
            inline=True,
        )
        e.add_field(name="⏱ 限時", value="60 秒", inline=True)
        e.add_field(name="提示", value="兩真一假，其他人要猜出謊言！", inline=False)
        return e

    def _build_voting_embed(
        self, session: DetectiveSession, voted_count: int = 0
    ) -> discord.Embed:
        declarer = next(
            (p for p in session.players if p.user_id == session.current_declarer_id),
            None,
        )
        declarer_name = declarer.display_name if declarer else "?"
        stmts = session.current_statements

        e = discord.Embed(title="🗳️ 猜哪句是謊言？", color=C_VOTING)
        e.add_field(name="陳述者", value=f"**{declarer_name}**", inline=False)
        if stmts:
            e.add_field(
                name="三句陳述",
                value=f"🅰️ {stmts.a}\n🅱️ {stmts.b}\n🆑 {stmts.c}",
                inline=False,
            )
        voter_count = len([p for p in session.players if p.user_id != session.current_declarer_id])
        e.add_field(name="投票進度", value=f"{voted_count} / {voter_count} 人已投票", inline=True)
        e.add_field(name="⏱ 限時", value="40 秒", inline=True)
        e.set_footer(text="請用下方按鈕投票，投完後不能更改！")
        return e

    def _build_revealing_embed(
        self, session: DetectiveSession, result: dict
    ) -> discord.Embed:
        stmts = session.current_statements
        lie_index = result.get("lie_index", 0)
        lie_label = ["A", "B", "C"][lie_index]

        if stmts:
            lie_text = [stmts.a, stmts.b, stmts.c][lie_index]
        else:
            lie_text = "（未知）"

        correct_voter_ids = result.get("correct_voters", [])
        fooled_voter_ids = result.get("fooled_voters", [])
        unvoted_ids = result.get("unvoted", [])
        score_changes = result.get("score_changes", {})

        # 轉換 user_id → display_name
        correct_names = (
            " | ".join(self._id_to_name(session, uid) for uid in correct_voter_ids)
            if correct_voter_ids else "（無人猜中）"
        )
        fooled_names = (
            " | ".join(self._id_to_name(session, uid) for uid in fooled_voter_ids)
            if fooled_voter_ids else "（無人被騙）"
        )

        e = discord.Embed(title="💡 揭曉！", color=C_REVEALING)
        e.add_field(
            name=f"🎭 謊言是 **{lie_label}**！",
            value=f"**{lie_text}**",
            inline=False,
        )
        e.add_field(name="✅ 猜中的人", value=correct_names, inline=False)
        e.add_field(name="🎪 被騙的人", value=fooled_names, inline=False)

        if unvoted_ids:
            e.add_field(
                name="⏭ 未投票",
                value=" | ".join(self._id_to_name(session, uid) for uid in unvoted_ids),
                inline=False,
            )

        if score_changes:
            changes_text = "\n".join(
                f"**{self._id_to_name(session, uid)}**: {'+' if delta >= 0 else ''}{delta}"
                for uid, delta in score_changes.items()
            )
            e.add_field(name="本輪得分變化", value=changes_text, inline=False)

        e.add_field(name="📊 積分板", value=self._scores_line(session), inline=False)
        e.set_footer(text="10 秒後進入下一輪…")
        return e

    def _build_game_over_embed(self, session: DetectiveSession) -> discord.Embed:
        ranked = sorted(session.players, key=lambda p: p.score, reverse=True)
        medals = ["🥇", "🥈", "🥉"]
        lines = [
            f"{medals[i] if i < 3 else f'{i+1}.'} **{p.display_name}**: {p.score} 分"
            for i, p in enumerate(ranked)
        ]
        e = discord.Embed(
            title="🎉 謊言偵探結束！",
            description="\n".join(lines),
            color=C_GAME_OVER,
        )
        e.set_footer(text="感謝遊玩！用 /detective_start 再來一局")
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

    # ── Companion bridge hooks (Lane F) ─────────────────────────────────────

    def _scoreboard_payload(self, session: DetectiveSession) -> list[dict]:
        """把 session.players 轉成 [{user, score}, ...]，依分數降序。"""
        ranked = sorted(session.players, key=lambda p: p.score, reverse=True)
        return [{"user": p.display_name, "score": p.score} for p in ranked]

    def _phase_for_state(self, state: DetectiveState) -> Optional[str]:
        return {
            DetectiveState.JOINING: "joining",
            DetectiveState.DECLARING: "declaring",
            DetectiveState.VOTING: "voting",
            DetectiveState.REVEALING: "revealing",
            DetectiveState.GAME_OVER: "ended",
        }.get(state)

    def _build_last_event(
        self, session: DetectiveSession, state: DetectiveState
    ) -> str:
        """根據當前 state 產生一行 events-log 字串。重用既有顯示邏輯的字串。"""
        declarer = next(
            (p for p in session.players if p.user_id == session.current_declarer_id),
            None,
        )
        name = declarer.display_name if declarer else "?"
        if state == DetectiveState.JOINING:
            return f"等待玩家加入（已 {len(session.players)} 人）"
        if state == DetectiveState.DECLARING:
            return f"輪到 {name} 宣告三句話"
        if state == DetectiveState.VOTING:
            return f"{name} 完成宣告，開放投票"
        if state == DetectiveState.REVEALING:
            result = session.last_round_result or {}
            correct = result.get("correct_voters", [])
            fooled = result.get("fooled_voters", [])
            return f"揭曉：{len(correct)} 人猜中，{len(fooled)} 人被騙"
        if state == DetectiveState.GAME_OVER:
            ranked = sorted(session.players, key=lambda p: p.score, reverse=True)
            top = ranked[0] if ranked else None
            return f"遊戲結束 — 冠軍：{top.display_name} {top.score} 分" if top else "遊戲結束"
        return ""

    def _timer_for_state(self, state: DetectiveState) -> Optional[int]:
        return {
            DetectiveState.DECLARING: 60,
            DetectiveState.VOTING: 40,
            DetectiveState.REVEALING: 10,
        }.get(state)

    async def _emit_phase(self, session: DetectiveSession) -> None:
        """若 bridge 在運行，廣播 game_phase_changed。"""
        bridge = getattr(self.bot, "companion_bridge", None)
        if bridge is None or not getattr(bridge, "is_running", False):
            return
        phase = self._phase_for_state(session.state)
        if phase is None:
            return
        declarer = next(
            (p for p in session.players if p.user_id == session.current_declarer_id),
            None,
        )
        payload = {
            "round": session.round_num,
            "round_total": max(len(session.players), session.round_num),
            "scoreboard": self._scoreboard_payload(session),
            "current_player": declarer.display_name if declarer else None,
            "timer_seconds": self._timer_for_state(session.state),
            "last_event": self._build_last_event(session, session.state),
        }
        try:
            await bridge.emit_game_phase_changed(
                game_name="detective",
                phase=phase,
                payload=payload,
            )
        except Exception as e:
            logger.warning(f"[Detective] emit_game_phase_changed failed: {e}")

    # ── Bridge-callable controls (Lane F) ──────────────────────────────────

    async def force_skip_round(self) -> None:
        """Companion bridge 呼叫：強制跳過當前回合。"""
        engine = self._engine
        session = self._session
        if engine is None or session is None:
            logger.info("[Detective] force_skip_round 無 active engine，drop")
            return
        try:
            if session.state == DetectiveState.DECLARING:
                self._cancel_declare_timeout()
                await engine.skip_declaring()
            elif session.state == DetectiveState.VOTING:
                self._cancel_vote_timeout()
                await engine.close_voting()
            else:
                logger.info(f"[Detective] force_skip_round in state={session.state}，drop")
        except Exception as e:
            logger.warning(f"[Detective] force_skip_round 失敗: {e}")

    async def end_session(self) -> None:
        """Companion bridge 呼叫：結束目前遊戲。"""
        if self._engine is None:
            logger.info("[Detective] end_session 無 active engine，drop")
            return
        self._cancel_tasks()
        self._engine = None
        self._session = None
        vc = self.bot.cogs.get("VoiceController") if hasattr(self.bot, "cogs") else None
        if vc is not None:
            vc.game_mode = False
        if self._channel is not None:
            try:
                await self._channel.send("🛑 謊言偵探已被 companion 端結束。")
            except Exception:
                pass

    # ── Central state dispatcher ───────────────────────────────────────────────

    async def on_state_change(self, session: DetectiveSession):
        self._session = session
        state = session.state

        # 廣播 phase 給 companion bridge（失敗不影響本機 UI）
        await self._emit_phase(session)

        if state == DetectiveState.JOINING:
            await self._post_game_message(
                self._build_joining_embed(session), JoinDetectiveView(self)
            )

        elif state == DetectiveState.DECLARING:
            self._cancel_declare_timeout()

            declarer_id = session.current_declarer_id or ""
            declarer = next(
                (p for p in session.players if p.user_id == declarer_id), None
            )
            declarer_name = declarer.display_name if declarer else "?"

            if declarer_id == "marvin":
                await self._post_game_message(self._build_declaring_embed(session))
                self._spawn(self._marvin_declare_task())
                # Marvin 也需要超時保護
                self._declare_timeout_task = self._spawn(
                    self._declare_timeout_task_coro(declarer_id, declarer_name)
                )
            else:
                await self._post_game_message(
                    self._build_declaring_embed(session),
                    DeclareView(self, declarer_id),
                )
                self._declare_timeout_task = self._spawn(
                    self._declare_timeout_task_coro(declarer_id, declarer_name)
                )

        elif state == DetectiveState.VOTING:
            self._cancel_declare_timeout()

            declarer_id = session.current_declarer_id or ""
            await self._post_game_message(
                self._build_voting_embed(session, voted_count=0),
                VoteView(self, declarer_id),
            )
            self._vote_timeout_task_handle = self._spawn(self._vote_timeout_task_coro())

            marvin_player = next(
                (p for p in session.players if p.user_id == "marvin"), None
            )
            if marvin_player and declarer_id != "marvin":
                self._spawn(self._marvin_vote_task())

        elif state == DetectiveState.REVEALING:
            self._cancel_vote_timeout()
            # 從 session 讀結果（engine 在呼叫 _notify 前已設好）
            result = session.last_round_result or {}
            await self._post_game_message(self._build_revealing_embed(session, result))
            self._spawn(self._reveal_then_advance())

        elif state == DetectiveState.GAME_OVER:
            self._cancel_tasks()
            await self._post_game_message(
                self._build_game_over_embed(session), PlayAgainView(self)
            )
            self._engine = None
            self._session = None
            vc = self.bot.cogs.get("VoiceController")
            if vc is not None:
                vc.game_mode = False

    # ── Background tasks ───────────────────────────────────────────────────────

    async def _declare_timeout_task_coro(
        self, declarer_id: str, declarer_name: str
    ):
        try:
            await asyncio.sleep(60.0)
            session = self._session
            engine = self._engine
            if session is None or engine is None:
                return
            if session.state != DetectiveState.DECLARING:
                return
            if session.current_declarer_id != declarer_id:
                return
            if self._channel:
                await self._channel.send(
                    f"⏰ **{declarer_name}** 超時，跳過這輪！此輪不計分，進入下一位陳述者。"
                )
            await engine.skip_declaring()
        except asyncio.CancelledError:
            pass

    async def _vote_timeout_task_coro(self):
        try:
            await asyncio.sleep(40.0)
            session = self._session
            engine = self._engine
            if session is None or engine is None:
                return
            if session.state != DetectiveState.VOTING:
                return
            # 統計已投票人數
            if session:
                voted = sum(
                    1 for p in session.players
                    if p.user_id != session.current_declarer_id and p.vote is not None
                )
                total = len([p for p in session.players if p.user_id != session.current_declarer_id])
                if self._channel:
                    await self._channel.send(
                        f"⏰ 投票時間到！{voted}/{total} 人已投票，以目前票數揭曉！"
                    )
            close_result = await engine.close_voting()
            if "error" in close_result:
                logger.warning(f"[Detective] vote timeout close_voting error: {close_result}")
        except asyncio.CancelledError:
            pass

    async def _marvin_declare_task(self):
        try:
            session = self._session
            if session is None:
                return

            player_names = [p.display_name for p in session.players if p.user_id != "marvin"]

            delay = random.uniform(2.0, 4.0)
            gen_task = asyncio.create_task(
                self._marvin.generate_statements(player_names)
            )
            await asyncio.sleep(delay)

            if (
                self._session is None
                or self._session.state != DetectiveState.DECLARING
                or self._session.current_declarer_id != "marvin"
            ):
                gen_task.cancel()
                return

            engine = self._engine
            if engine is None:
                gen_task.cancel()
                return

            try:
                stmts = await asyncio.wait_for(asyncio.shield(gen_task), timeout=5.0)
            except (asyncio.TimeoutError, Exception) as e:
                logger.warning(f"[Detective] Marvin generate_statements failed: {e}")
                gen_task.cancel()
                stmts = {
                    "a": "有人在這局說話最多。",
                    "b": "有人從不主動開始遊戲。",
                    "c": "有人贏過我。",
                    "lie_index": 2,
                }

            tts_quip = "我想好了，三句話裡有一句是謊言。"
            # 先發 channel 文字
            if self._channel:
                await self._channel.send(f"**Marvin** 🤖：{tts_quip}")
            # TTS（失敗不卡遊戲）
            vc = self.bot.cogs.get("VoiceController")
            if vc is not None:
                try:
                    await vc.play_tts(tts_quip, already_in_channel=False)
                except Exception as e:
                    logger.warning(f"[Detective] Marvin TTS failed (continuing): {e}")

            await engine.submit_statements(
                "marvin",
                stmts["a"],
                stmts["b"],
                stmts["c"],
                stmts["lie_index"],
            )
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[Detective] _marvin_declare_task error: {e}")
            # fallback：若任何錯誤，跳過這輪
            engine = self._engine
            if engine is not None:
                try:
                    await engine.skip_declaring()
                except Exception:
                    pass

    async def _marvin_vote_task(self):
        try:
            session = self._session
            if session is None:
                return

            stmts = session.current_statements
            if stmts is None:
                return

            declarer = next(
                (p for p in session.players if p.user_id == session.current_declarer_id),
                None,
            )
            declarer_name = declarer.display_name if declarer else "陳述者"

            delay = random.uniform(3.0, 6.0)
            vote_task = asyncio.create_task(
                self._marvin.generate_vote(
                    {"a": stmts.a, "b": stmts.b, "c": stmts.c},
                    declarer_name,
                )
            )
            await asyncio.sleep(delay)

            if (
                self._session is None
                or self._session.state != DetectiveState.VOTING
            ):
                vote_task.cancel()
                return

            engine = self._engine
            if engine is None:
                vote_task.cancel()
                return

            try:
                vote_index, comment = await asyncio.wait_for(
                    asyncio.shield(vote_task), timeout=5.0
                )
            except (asyncio.TimeoutError, Exception) as e:
                logger.warning(f"[Detective] Marvin generate_vote failed: {e}")
                vote_task.cancel()
                vote_index = random.randint(0, 2)
                comment = "嗯…我有一種感覺。"

            result = await engine.submit_vote("marvin", vote_index)

            if "error" not in result and not result.get("already_voted"):
                if self._channel:
                    vote_label = ["A", "B", "C"][vote_index]
                    await self._channel.send(
                        f"**Marvin** 🤖：{comment}（我投 **{vote_label}**）"
                    )

                if result.get("all_voted"):
                    self._cancel_vote_timeout()
                    close_result = await engine.close_voting()
                    if "error" in close_result:
                        logger.warning(f"[Detective] Marvin vote close_voting error: {close_result}")

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[Detective] _marvin_vote_task error: {e}")

    async def _reveal_then_advance(self):
        try:
            await asyncio.sleep(10.0)

            session = self._session
            engine = self._engine
            if session is None or engine is None:
                return
            if session.state != DetectiveState.REVEALING:
                return

            result = session.last_round_result or {}

            marvin_player = next(
                (p for p in session.players if p.user_id == "marvin"), None
            )
            if marvin_player:
                declarer = next(
                    (p for p in session.players if p.user_id == session.current_declarer_id),
                    None,
                )
                declarer_name = declarer.display_name if declarer else "陳述者"
                correct_voters = result.get("correct_voters", [])
                fooled_count = len(result.get("fooled_voters", []))
                # correct_voters 存的是 user_id，Marvin 的 user_id = "marvin"
                marvin_correct = "marvin" in correct_voters

                try:
                    quip = await self._marvin.generate_reveal_quip(
                        marvin_correct, fooled_count, declarer_name
                    )
                    # 先發 channel 文字，再播 TTS
                    if self._channel:
                        await self._channel.send(f"**Marvin** 🤖：{quip}")
                    vc = self.bot.cogs.get("VoiceController")
                    if vc is not None:
                        try:
                            await vc.play_tts(quip, already_in_channel=False)
                        except Exception as e:
                            logger.warning(f"[Detective] Marvin reveal TTS failed (continuing): {e}")
                except Exception as e:
                    logger.warning(f"[Detective] Marvin reveal quip failed (continuing): {e}")

            await engine.advance_declaring()

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[Detective] _reveal_then_advance error: {e}")

    # ── STT hook ───────────────────────────────────────────────────────────────

    def should_suppress_for_game(self, speaker: str) -> bool:
        return False

    async def receive_voice_answer_by_speaker(self, speaker: str, text: str) -> bool:
        return False

    # ── Internal start helper ──────────────────────────────────────────────────

    async def _handle_start_game(self, channel: Optional[discord.TextChannel]) -> None:
        if self._engine is not None:
            return

        self._channel = channel

        session = DetectiveSession(
            session_id=str(uuid.uuid4()),
            guild_id=channel.guild.id if channel and hasattr(channel, "guild") else 0,
            channel_id=channel.id if channel else 0,
        )
        self._session = session

        self._engine = DetectiveEngine(
            session=session,
            on_state_change=self.on_state_change,
            db_path="marvin.db",
        )

        # Marvin 自動加入（會觸發 on_state_change(JOINING) → _post_game_message）
        await self._engine.add_player("marvin", "Marvin")

        vc = self.bot.cogs.get("VoiceController")
        if vc is not None:
            vc.game_mode = True

    # ── Slash commands ─────────────────────────────────────────────────────────

    @app_commands.command(name="bustedlie_start", description="開始一場謊言偵探遊戲")
    async def detective_start(self, interaction: discord.Interaction):
        if self._engine is not None:
            await interaction.response.send_message(
                "謊言偵探遊戲已在進行中！", ephemeral=True
            )
            return

        await interaction.response.send_message(
            "🕵️ **謊言偵探** 遊戲啟動！Marvin 已加入，等待其他玩家…",
            ephemeral=True,
        )
        await self._handle_start_game(interaction.channel)

    @app_commands.command(name="bustedlie_stop", description="強制中止目前的謊言偵探遊戲")
    async def detective_stop(self, interaction: discord.Interaction):
        if self._engine is None:
            await interaction.response.send_message(
                "目前沒有進行中的謊言偵探遊戲。", ephemeral=True
            )
            return

        self._cancel_tasks()
        self._engine = None
        self._session = None

        vc = self.bot.cogs.get("VoiceController")
        if vc is not None:
            vc.game_mode = False

        if self._channel:
            await self._channel.send(
                f"🛑 謊言偵探已被 {interaction.user.display_name} 強制中止。"
            )
        await interaction.response.send_message(
            "已中止。可用 `/detective_start` 重新開始。", ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(DetectiveCog(bot))
