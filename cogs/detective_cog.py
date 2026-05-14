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
from game.detective.session import DetectiveSession, DetectiveState, PlayerDState, StatementSet
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
        label="陳述 A",
        placeholder="輸入第一句話（最多 80 字）",
        max_length=80,
    )
    stmt_b = discord.ui.TextInput(
        label="陳述 B",
        placeholder="輸入第二句話（最多 80 字）",
        max_length=80,
    )
    stmt_c = discord.ui.TextInput(
        label="陳述 C",
        placeholder="輸入第三句話（最多 80 字）",
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
                "謊言選項只能是 A、B 或 C！", ephemeral=True
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
            # 更新 joining embed 顯示新玩家
            session = self._cog._session
            if session and self._cog._channel:
                await self._cog._channel.send(
                    embed=self._cog._build_joining_embed(session)
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
    """陳述者專用 View — 只有 current_declarer 可以按按鈕。"""

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
    """投票 View — 三個選項按鈕。"""

    def __init__(self, cog: "DetectiveCog", declarer_id: str):
        super().__init__(timeout=None)
        self._cog = cog
        self._declarer_id = declarer_id

    async def _handle_vote(
        self, interaction: discord.Interaction, vote_index: int, label: str
    ):
        voter_id = str(interaction.user.id)

        # 陳述者不能投票
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
                "你已經投過票了！", ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"✅ 你投了「{label} 是謊言」，等待其他人投票…", ephemeral=True
        )

        # 若所有人都投完，直接關閉投票
        if result.get("all_voted"):
            self._cog._cancel_vote_timeout()
            close_result = await engine.close_voting()
            self._cog._last_close_result = close_result

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
        self._last_close_result: Optional[dict] = None
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

    def _scores_line(self, session: DetectiveSession) -> str:
        return " | ".join(
            f"**{p.display_name}**: {p.score}" for p in session.players
        )

    def _build_joining_embed(self, session: DetectiveSession) -> discord.Embed:
        names = [p.display_name for p in session.players] or ["（無）"]
        rules_text = (
            "• 每輪一位陳述者說三句話，其中**兩真一假**\n"
            "• 其他人猜哪句是謊言\n"
            "• 猜中得 **+2 分**，騙到別人得 **+1 分**\n"
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
        # 已宣告人數 = total - (剩餘隊列 + 1)
        declared_count = total - len(session.declarer_queue)

        e = discord.Embed(
            title=f"📝 {name} 正在想三句話…",
            color=C_DECLARING,
        )
        e.add_field(
            name="目前陳述者",
            value=f"**{name}**（第 {declared_count}/{total} 位）",
            inline=True,
        )
        e.add_field(name="⏱ 限時", value="60 秒", inline=True)
        e.add_field(
            name="其他玩家",
            value="等待陳述者輸入…",
            inline=False,
        )
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

        e = discord.Embed(
            title="🗳️ 猜哪句是謊言？",
            color=C_VOTING,
        )
        e.add_field(name="陳述者", value=f"**{declarer_name}**", inline=False)
        if stmts:
            e.add_field(name="A", value=stmts.a, inline=False)
            e.add_field(name="B", value=stmts.b, inline=False)
            e.add_field(name="C", value=stmts.c, inline=False)
        # 計算可投票人數（扣除陳述者）
        voter_count = len([p for p in session.players if p.user_id != session.current_declarer_id])
        e.add_field(
            name="投票進度",
            value=f"{voted_count} / {voter_count} 人已投票",
            inline=True,
        )
        e.add_field(name="⏱ 剩餘時間", value="40 秒", inline=True)
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

        correct_voters = result.get("correct_voters", [])
        fooled_voters = result.get("fooled_voters", [])
        score_changes = result.get("score_changes", {})

        e = discord.Embed(
            title="💡 揭曉！",
            color=C_REVEALING,
        )
        e.add_field(
            name=f"🎭 謊言是 **{lie_label}**！",
            value=f"**{lie_text}**",
            inline=False,
        )

        correct_names = (
            " | ".join(correct_voters) if correct_voters else "（無人猜中）"
        )
        fooled_names = (
            " | ".join(fooled_voters) if fooled_voters else "（無人被騙）"
        )
        e.add_field(name="✅ 猜中的人", value=correct_names, inline=False)
        e.add_field(name="🎪 被騙的人", value=fooled_names, inline=False)

        if score_changes:
            changes_text = "\n".join(
                f"**{uid}**: {'+' if delta >= 0 else ''}{delta}"
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

    # ── Central state dispatcher ───────────────────────────────────────────────

    async def on_state_change(self, session: DetectiveSession):
        self._session = session
        state = session.state

        if state == DetectiveState.JOINING:
            await self._post_game_message(
                self._build_joining_embed(session), JoinDetectiveView(self)
            )

        elif state == DetectiveState.DECLARING:
            # 取消前一個宣告計時器
            self._cancel_declare_timeout()

            declarer_id = session.current_declarer_id or ""
            declarer = next(
                (p for p in session.players if p.user_id == declarer_id), None
            )
            declarer_name = declarer.display_name if declarer else "?"

            if declarer_id == "marvin":
                await self._post_game_message(
                    self._build_declaring_embed(session)
                )
                self._spawn(self._marvin_declare_task())
            else:
                await self._post_game_message(
                    self._build_declaring_embed(session),
                    DeclareView(self, declarer_id),
                )
                self._declare_timeout_task = self._spawn(
                    self._declare_timeout_task_coro(declarer_id, declarer_name)
                )

        elif state == DetectiveState.VOTING:
            # 取消宣告計時器
            self._cancel_declare_timeout()

            declarer_id = session.current_declarer_id or ""
            await self._post_game_message(
                self._build_voting_embed(session, voted_count=0),
                VoteView(self, declarer_id),
            )
            self._vote_timeout_task_handle = self._spawn(
                self._vote_timeout_task_coro()
            )

            # 若 Marvin 在玩家中且不是陳述者，讓 Marvin 投票
            marvin_player = next(
                (p for p in session.players if p.user_id == "marvin"), None
            )
            if marvin_player and declarer_id != "marvin":
                self._spawn(self._marvin_vote_task())

        elif state == DetectiveState.REVEALING:
            self._cancel_vote_timeout()
            result = self._last_close_result or {}
            await self._post_game_message(
                self._build_revealing_embed(session, result)
            )
            self._spawn(self._reveal_then_advance())

        elif state == DetectiveState.GAME_OVER:
            self._cancel_tasks()
            await self._post_game_message(
                self._build_game_over_embed(session), PlayAgainView(self)
            )
            self._engine = None
            self._session = None
            # 恢復 VoiceController
            vc = self.bot.cogs.get("VoiceController")
            if vc is not None:
                vc.game_mode = False

    # ── Background tasks ───────────────────────────────────────────────────────

    async def _declare_timeout_task_coro(
        self, declarer_id: str, declarer_name: str
    ):
        """60 秒後若仍在 DECLARING 且同一陳述者，跳過這輪。"""
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
                    f"⏰ **{declarer_name}** 超時，跳過這輪！"
                )
            await engine.skip_declaring()
        except asyncio.CancelledError:
            pass

    async def _vote_timeout_task_coro(self):
        """40 秒後若仍在 VOTING，強制關閉投票。"""
        try:
            await asyncio.sleep(40.0)
            session = self._session
            engine = self._engine
            if session is None or engine is None:
                return
            if session.state != DetectiveState.VOTING:
                return
            if self._channel:
                await self._channel.send("⏰ 投票時間到！")
            close_result = await engine.close_voting()
            self._last_close_result = close_result
        except asyncio.CancelledError:
            pass

    async def _marvin_declare_task(self):
        """Marvin 當陳述者時，自動生成三句話。"""
        try:
            session = self._session
            if session is None:
                return

            player_names = [p.display_name for p in session.players if p.user_id != "marvin"]

            # 並行生成陳述與 delay（假裝在思考）
            delay = random.uniform(2.0, 4.0)
            gen_task = asyncio.ensure_future(
                self._marvin.generate_statements(player_names)
            )
            await asyncio.sleep(delay)

            # 確認遊戲狀態未改變
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
                # fallback：生成假陳述
                stmts = {
                    "a": "我曾經在雨中淋濕過。",
                    "b": "我會說 17 種語言。",
                    "c": "我從未感到過悲傷。",
                    "lie_index": 1,
                }

            # TTS 宣告台詞
            vc = self.bot.cogs.get("VoiceController")
            tts_quip = "我想好了，三句話裡有一句是謊言。"
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

    async def _marvin_vote_task(self):
        """Marvin 當投票者時，分析陳述後投票。"""
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

            # 假裝在分析（3-6 秒）
            delay = random.uniform(3.0, 6.0)
            vote_task = asyncio.ensure_future(
                self._marvin.generate_vote(
                    {"a": stmts.a, "b": stmts.b, "c": stmts.c},
                    declarer_name,
                )
            )
            await asyncio.sleep(delay)

            # 確認遊戲狀態
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
                    self._last_close_result = close_result

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[Detective] _marvin_vote_task error: {e}")

    async def _reveal_then_advance(self):
        """揭曉後等 10 秒，播 TTS 評語，再推進到下一陳述者。"""
        try:
            await asyncio.sleep(10.0)

            session = self._session
            engine = self._engine
            if session is None or engine is None:
                return
            if session.state != DetectiveState.REVEALING:
                return

            result = self._last_close_result or {}

            # 若 Marvin 在遊戲中，播 TTS reveal quip
            marvin_player = next(
                (p for p in session.players if p.user_id == "marvin"), None
            )
            if marvin_player:
                vc = self.bot.cogs.get("VoiceController")
                if vc is not None:
                    try:
                        declarer = next(
                            (p for p in session.players
                             if p.user_id == session.current_declarer_id),
                            None,
                        )
                        declarer_name = declarer.display_name if declarer else "陳述者"
                        correct_voters = result.get("correct_voters", [])
                        fooled_count = len(result.get("fooled_voters", []))
                        marvin_correct = "Marvin" in correct_voters

                        quip = await self._marvin.generate_reveal_quip(
                            marvin_correct, fooled_count, declarer_name
                        )
                        await vc.play_tts(quip, already_in_channel=False)
                    except Exception as e:
                        logger.warning(f"[Detective] Marvin reveal TTS failed (continuing): {e}")

            await engine.advance_declaring()

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[Detective] _reveal_then_advance error: {e}")

    # ── STT hook ───────────────────────────────────────────────────────────────

    def should_suppress_for_game(self, speaker: str) -> bool:
        """謊言偵探不限制任何玩家說話。"""
        return False

    async def receive_voice_answer_by_speaker(self, speaker: str, text: str) -> bool:
        """謊言偵探不消費語音輸入。"""
        return False

    # ── Internal start helper ──────────────────────────────────────────────────

    async def _handle_start_game(self, channel: Optional[discord.TextChannel]) -> None:
        """建立新 session；供 /detective_start 和「再來一局」共用。"""
        if self._engine is not None:
            return

        self._channel = channel

        session = DetectiveSession(
            session_id=str(uuid.uuid4()),
            guild_id=channel.guild.id if channel and hasattr(channel, "guild") else 0,
            channel_id=channel.id if channel else 0,
        )
        self._session = session
        self._last_close_result = None

        self._engine = DetectiveEngine(
            session=session,
            on_state_change=self.on_state_change,
            db_path="marvin.db",
        )

        # Marvin 自動加入
        await self._engine.add_player("marvin", "Marvin")

        # 進入 game_mode
        vc = self.bot.cogs.get("VoiceController")
        if vc is not None:
            vc.game_mode = True

        # 顯示 joining embed
        await self._post_game_message(
            self._build_joining_embed(session), JoinDetectiveView(self)
        )

    # ── Slash commands ─────────────────────────────────────────────────────────

    @app_commands.command(name="detective_start", description="開始一場謊言偵探遊戲")
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

    @app_commands.command(name="detective_stop", description="強制中止目前的謊言偵探遊戲")
    async def detective_stop(self, interaction: discord.Interaction):
        if self._engine is None:
            await interaction.response.send_message(
                "目前沒有進行中的謊言偵探遊戲。", ephemeral=True
            )
            return

        self._cancel_tasks()
        self._engine = None
        self._session = None
        self._last_close_result = None

        vc = self.bot.cogs.get("VoiceController")
        if vc is not None:
            vc.game_mode = False

        await interaction.response.send_message(
            "🛑 謊言偵探已強制中止，可用 `/detective_start` 重新開始。",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(DetectiveCog(bot))
