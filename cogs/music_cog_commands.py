"""
MusicCommandsMixin — MusicCog 的音樂相關 slash 指令。

從 music_cog.py 抽出（減肥，比照 voice_controller.py 拆解出
voice_controller_commands.py 的先例），以 mixin 形式併入 MusicCog：
    class MusicCog(MusicCommandsMixin, ..., commands.Cog): ...
因此 self 仍是 MusicCog 實例，_vc / _resolve_yt_query / _queue_user_song /
_ensure_stream_loop / start_radio / stop_radio / _safe_music_command /
_check_song_duplicate / _active_control_view / stream_mode / radio_mode /
_last_search / bot.music_memory / bot.router 等全部沿用原本的 self 存取，
行為零改動。

這是純 class（不繼承 commands.Cog）——discord.py 的 CogMeta.__new__ 會走過
reversed(mro) 蒐集每個 base 的 app_commands.Command，掛進
MusicCog(MusicCommandsMixin, ..., commands.Cog) 一樣能正常註冊 slash command。
"""
from __future__ import annotations

import asyncio
import datetime
import io
import logging
import time
from typing import Optional

import discord
from discord import app_commands

from playlist_utils import (
    extract_youtube_playlist_flat,
    format_playlist_export,
    is_youtube_playlist_url,
    parse_playlist_content,
)

logger = logging.getLogger(__name__)


class MusicCommandsMixin:
    # ── 🎵 Slash commands ─────────────────────────────────────────────────────

    @app_commands.command(name="marvin_radio", description="[Radio] 啟動/停止 Marvin 電台，隨機播放 assets/songs 中的歌曲")
    @app_commands.describe(action="start=強制啟動, stop=強制停止, 不填=切換狀態")
    @app_commands.choices(action=[
        app_commands.Choice(name="start — 啟動電台", value="start"),
        app_commands.Choice(name="stop — 停止電台", value="stop"),
    ])
    async def marvin_radio(self, interaction: discord.Interaction, action: str = "toggle"):
        await interaction.response.defer(ephemeral=False)
        vc = self._vc()
        if not vc:
            await interaction.followup.send("❌ 語音系統尚未就緒。", ephemeral=True)
            return

        if action == "toggle":
            action = "stop" if self.radio_mode else "start"

        if action == "start":
            if self.radio_mode:
                await interaction.followup.send("📻 電台已經在播放了。就算宇宙正在崩塌，至少還有音樂。")
                return
            guild_vc = interaction.guild.voice_client
            if not guild_vc:
                if interaction.user.voice:
                    await interaction.followup.send("❌ 馬文不在目前的語音頻道中。請先使用 `/summon` 召喚我，我才能為你播放這無助的旋律。", ephemeral=True)
                else:
                    await interaction.followup.send("❌ 馬文不在頻道中，且你似乎也還沒加入任何頻道。這世界果然一片荒蕪。", ephemeral=True)
                return
            await interaction.followup.send("📻 **【馬文電台：啟動】**\n好吧，既然你們都不說話，我就讓音樂來填補這令人窒息的寂靜。")
            await self.start_radio(trigger="手動指令")

        elif action == "stop":
            if not self.radio_mode:
                await interaction.followup.send("📻 電台沒有在播放。沉默本來就是這個宇宙的預設狀態。", ephemeral=True)
                return
            await self.stop_radio(reason="手動指令停止")
            await interaction.followup.send("📻 **【馬文電台：停止】**\n好了，音樂停了。你們滿意了嗎。")

    @app_commands.command(name="marvin_play", description="[Stream] 播放 YouTube 音樂，輸入歌名或貼上連結")
    @app_commands.describe(query="歌名（例如：周杰倫 稻香）或 YouTube 連結")
    async def marvin_play(self, interaction: discord.Interaction, query: str):
        from cogs.voice_views import PlayControlView
        await interaction.response.defer(ephemeral=False)
        vc = self._vc()
        if not vc:
            await interaction.followup.send("❌ 語音系統尚未就緒。", ephemeral=True)
            return
        guild_vc = interaction.guild.voice_client
        if not guild_vc:
            await interaction.followup.send("❌ 馬文不在語音頻道中。請先使用 `/summon` 召喚我。", ephemeral=True)
            return

        username = interaction.user.display_name

        _history_kws = ["喜歡的歌", "我的歌單", "曾點過的歌", "曾經點過", "愛歌", "常聽的歌"]
        if hasattr(self.bot, 'music_memory') and not any(kw in query for kw in _history_kws):
            last = self._last_search.get(username)
            if last and time.time() - last['ts'] < 300 and last.get('source') == 'voice':
                old_q = last.get('query', '')
                if old_q and old_q != query and len(old_q) > 1:
                    is_version_spec = old_q in query and len(query) > len(old_q) + 1
                    is_correction = False
                    if not is_version_spec:
                        try:
                            from rapidfuzz import fuzz
                            is_correction = fuzz.ratio(old_q, query) >= 60
                        except ImportError:
                            pass
                    if is_version_spec or is_correction:
                        note = (
                            f"搜尋「{old_q}」→ 自動指定版本「{query}」"
                            if is_version_spec
                            else f"語音辨識「{old_q}」→ 修正為「{query}」"
                        )
                        self.bot.music_memory.record_stt_correction(username, old_q, query)
                        self._last_search.pop(username, None)
                        asyncio.create_task(
                            interaction.followup.send(
                                f"📝 **【搜尋偏好學習】** 已記住：{note}",
                                ephemeral=False,
                            )
                        )

        history_keywords = ["喜歡的歌", "我的歌單", "曾點過的歌", "曾經點過", "愛歌", "常聽的歌"]
        is_random_history = False
        if any(kw in query for kw in history_keywords):
            history = self.bot.router.memory.get_song_history(username)
            if not history:
                await interaction.followup.send("❌ 你的大腦裡一片空白，我的記憶庫裡也沒有你點過任何歌的紀錄。")
                return
            import random
            query = random.choice(history)
            is_random_history = True
            msg = await interaction.followup.send(f"🔍 **正在從你那可悲的歌單中隨機挑選：** `{query}`...")
        else:
            msg = await interaction.followup.send(f"🔍 **正在搜尋：** `{query}`...")

        info = await self._resolve_yt_query(query)
        if not info:
            await msg.edit(content=f"❌ 找不到結果：`{query}`。就跟在宇宙虛空中尋找意義一樣徒勞。")
            return

        if not is_random_history and hasattr(self.bot.router.memory, 'add_song_history'):
            self.bot.router.memory.add_song_history(username, info['title'])

        vc.stt_logger.info(
            f"[點歌-手動] 使用者={username} | 搜尋={query} | 結果={info['title']} / {info.get('uploader', '?')}"
        )

        if not is_random_history:
            self._last_search[username] = {'query': query, 'ts': time.time(), 'source': 'manual'}

        if self.radio_mode:
            await self.stop_radio(reason="Stream 模式接管")

        info['requested_by'] = username
        if self._check_song_duplicate(url=info['url'], title=info['title'], username=username, webpage_url=info.get('webpage_url', ''), check_history=False):
            # 已在佇列 → 仍要確保 loop 活著：使用者重點同一首，多半正是因為它沒在播。
            revived = self._ensure_stream_loop()
            await msg.edit(content=f"⏭️ 「{info['title']}」已在佇列待播了。"
                                   + ("（播放已恢復）" if revived else ""))
            return
        self._queue_user_song(info)

        self._ensure_stream_loop()

        existing_view = self._active_control_view
        if existing_view and getattr(existing_view, 'message', None):
            try:
                await existing_view.message.edit(embed=existing_view._build_embed(), view=existing_view)
                await msg.delete()
                return
            except Exception:
                pass

        view = PlayControlView(vc)
        self._active_control_view = view
        await msg.edit(content=None, embed=view._build_embed(), view=view)
        view.message = msg

    @app_commands.command(name="marvin_skip", description="[Stream] 跳過當前播放的歌曲")
    async def marvin_skip(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not self.stream_mode:
            await interaction.followup.send("沒有歌曲在播放。虛無是這個宇宙的預設狀態。", ephemeral=True)
            return
        user_name = interaction.user.display_name if interaction.user else "Discord"
        await self._safe_music_command(user_name, "", "skip")
        await interaction.followup.send("⏭️ 已跳過。", ephemeral=True)

    @app_commands.command(name="marvin_play_control", description="[Stream] 播放控制台：音量、暫停、上下首、佇列管理")
    async def marvin_play_control(self, interaction: discord.Interaction):
        from cogs.voice_views import PlayControlView
        vc = self._vc()
        if not vc:
            await interaction.response.send_message("❌ 語音系統尚未就緒。", ephemeral=True)
            return
        view = PlayControlView(vc)
        self._active_control_view = view
        await interaction.response.send_message(embed=view._build_embed(), view=view)
        view.message = await interaction.original_response()

    @app_commands.command(name="marvin_playlist_export", description="[Playlist] 匯出個人點播歌單（支援 TXT/JSON/CSV 檔）")
    @app_commands.describe(
        format="匯出格式：txt（純文字清單）、json（完整結構化資料）、csv（表格）",
        target_user="[選填] 指定要匯出的成員名稱（預設為自己）",
    )
    @app_commands.choices(format=[
        app_commands.Choice(name="txt — 純文字清單（含歌名與網址）", value="txt"),
        app_commands.Choice(name="json — 結構化 JSON 備份檔", value="json"),
        app_commands.Choice(name="csv — CSV 表格檔案", value="csv"),
    ])
    async def marvin_playlist_export(
        self,
        interaction: discord.Interaction,
        format: str = "txt",
        target_user: Optional[str] = None,
    ):
        await interaction.response.defer(ephemeral=False)
        mm = getattr(self.bot, "music_memory", None)
        if not mm:
            await interaction.followup.send("❌ 音樂記憶系統尚未就緒。", ephemeral=True)
            return

        username = target_user or interaction.user.display_name
        songs = mm.export_user_playlist(username)
        if not songs:
            await interaction.followup.send(f"❌ 找不到 `{username}` 的點播歌單紀錄（可能尚未在頻道中點播過歌曲）。")
            return

        summary, file_bytes, ext = format_playlist_export(songs, format, username)
        date_str = datetime.datetime.now().strftime("%Y%m%d")
        filename = f"playlist_{username}_{date_str}.{ext}"

        file = discord.File(io.BytesIO(file_bytes), filename=filename)
        await interaction.followup.send(summary, file=file)

    @app_commands.command(name="marvin_playlist_import", description="[Playlist] 匯入歌曲至個人歌單（支援 YouTube 播放清單連結、附檔或文字）")
    @app_commands.describe(
        query_or_url="YouTube 播放清單連結、單曲網址或文字清單",
        file="[選填] 上傳 JSON / TXT / CSV 歌單檔案",
        target_user="[選填] 指定要匯入的成員名稱（預設為自己）",
    )
    async def marvin_playlist_import(
        self,
        interaction: discord.Interaction,
        query_or_url: Optional[str] = None,
        file: Optional[discord.Attachment] = None,
        target_user: Optional[str] = None,
    ):
        await interaction.response.defer(ephemeral=False)
        mm = getattr(self.bot, "music_memory", None)
        if not mm:
            await interaction.followup.send("❌ 音樂記憶系統尚未就緒。", ephemeral=True)
            return

        username = target_user or interaction.user.display_name

        if not query_or_url and not file:
            await interaction.followup.send("❌ 請提供 YouTube 歌單連結、文字清單或上傳歌單檔案（.json, .txt, .csv）。", ephemeral=True)
            return

        songs_to_import: list[dict] = []

        if file:
            try:
                content_bytes = await file.read()
                ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else "txt"
                parsed = parse_playlist_content(content_bytes, ext)
                songs_to_import.extend(parsed)
            except Exception as e:
                logger.error(f"❌ 讀取附檔失敗: {e}")
                await interaction.followup.send(f"❌ 讀取檔案 `{file.filename}` 失敗: {e}", ephemeral=True)
                return

        if query_or_url:
            cleaned_query = query_or_url.strip()
            if is_youtube_playlist_url(cleaned_query):
                yt_songs = await extract_youtube_playlist_flat(cleaned_query)
                songs_to_import.extend(yt_songs)
            else:
                parsed = parse_playlist_content(cleaned_query, "txt")
                songs_to_import.extend(parsed)

        if not songs_to_import:
            await interaction.followup.send("❌ 無法從提供之內容中解析出有效歌曲。", ephemeral=True)
            return

        imported_cnt, skipped_cnt = mm.import_user_playlist(username, songs_to_import)
        total_user_songs = len(mm.export_user_playlist(username))

        msg = (
            f"✅ **【歌單匯入完成】** 成功為 `{username}` 匯入 **{imported_cnt}** 首歌！\n"
            f"（略過無效或重複項：{skipped_cnt} 首，目前個人歌單共有 **{total_user_songs}** 首歌）\n"
            f"💡 現在你可以直接在語音頻道說「**播我的歌單**」開始連續播放！"
        )
        await interaction.followup.send(msg)
