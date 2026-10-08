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

_ALBUM_TOUR_POLL_S = 1.0   # 巡禮 runner 等佇列消化的間隔（真 sleep 讓出，別 busy-spin）


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
        if getattr(self, '_album_tour', None):
            await interaction.followup.send("📀 專輯巡禮進行中，點歌先暫停；要結束巡禮就說「停」。", ephemeral=True)
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

    @app_commands.command(name="guide_song", description="[DJ] 深度導聆：先查證講 20 秒該聽什麼，再從頭播這首")
    @app_commands.describe(artist="歌手（例如：周杰倫）", song="歌名（例如：雙截棍）")
    async def guide_song(self, interaction: discord.Interaction, artist: str, song: str):
        await interaction.response.defer(ephemeral=False)
        vc = self._vc()
        if not vc:
            await interaction.followup.send("❌ 語音系統尚未就緒。", ephemeral=True)
            return
        if not interaction.guild.voice_client:
            await interaction.followup.send("❌ 馬文不在語音頻道中。請先使用 `/summon` 召喚我。", ephemeral=True)
            return
        if getattr(self, '_album_tour', None):
            await interaction.followup.send("📀 專輯巡禮進行中，點歌先暫停；要結束巡禮就說「停」。", ephemeral=True)
            return

        msg = await interaction.followup.send(f"🎧 **正在查證導聆資料：** {artist}〈{song}〉...")

        info = await self._resolve_yt_query(f"{artist} {song}")
        if not info:
            await msg.edit(content=f"❌ 找不到結果：{artist}〈{song}〉。")
            return

        from audiophile_fetcher import resolved_matches_track
        if not resolved_matches_track(info, song):
            await msg.edit(content=f"❌ YouTube 搜到的跟〈{song}〉對不上（搜到的是：{info.get('title', '?')}），不播錯歌。")
            return
        # 乾淨歌手/歌名寫進 info：導聆 key 與 /tour 同一套（audiophile::歌手 - 歌名）共用快取，
        # 後續 DJ/歌詞（_dj_clean_name 優先讀 track）也跟著用乾淨名字
        info['track'] = song
        info['artist'] = artist

        # 先渲染完才入隊：佇列空時歌會立刻開播，背景渲染會來不及
        await self._prepare_audiophile_guide(info)

        if self.radio_mode:
            await self.stop_radio(reason="Stream 模式接管")

        info['requested_by'] = interaction.user.display_name
        self._queue_user_song(info)
        self._ensure_stream_loop()

        await msg.edit(content=f"🎧 **【深度導聆】** 已排入：{info['title']}\n> {info.get('_audiophile_guide_text', '')}")

    def _audiophile_deps(self) -> tuple:
        """導聆/巡禮共用接線：(store, guard, router)。store 跟 DJ 賞析共用同一實例（多實例整份寫檔會互蓋）。"""
        from llm_paid import PaidUsageGuard
        from song_knowledge_store import SongKnowledgeStore
        store = getattr(self, '_song_knowledge_store', None)
        if store is None:
            store = SongKnowledgeStore()
            self._song_knowledge_store = store
        guard = getattr(self, '_audiophile_guard', None)
        if guard is None:
            guard = PaidUsageGuard()
            self._audiophile_guard = guard
        return store, guard, getattr(self.bot, 'router', None)

    async def _prepare_audiophile_guide(self, info: dict) -> None:
        """接線——共用 _song_knowledge_store（跟 DJ 賞析同一實例，多實例整份寫檔會互蓋）、
        router 的 free/paid Gemini client、PaidUsageGuard 記帳、bot.tts_engine、ffprobe 量秒。
        """
        from audiophile_fetcher import render_audiophile_guide

        store, guard, router = self._audiophile_deps()
        title, artist = self._dj_clean_name(info)
        await render_audiophile_guide(
            info, title=title or info.get('title', ''), artist=artist,
            free_client=getattr(router, 'google_client', None),
            paid_client=getattr(router, 'google_paid_client', None),
            guard=guard, store=store,
            tts_engine=self.bot.tts_engine,
            probe_duration=self._probe_audio_duration,
        )

    async def speak_now_playing_guide(self, info: dict) -> None:
        """「馬文這是什麼歌」報完歌名後接導聆：疊在 TTS 層講、歌不中斷（2026-10-01 使用者定）。
        稿子跟 DJ 預取同一把 label（大多快取命中零 API）；問的是真人 → human=True 可走付費鏈。
        查不到稿就不講（不用保底台詞——說錯不如沒說）；查稿/TTS 期間換歌了就作廢。"""
        vc = self._vc()
        if vc is None:
            return

        from audiophile_fetcher import _song_label, strip_entry_cue

        canon = info.get('_canon')
        if canon:
            label = _song_label(canon['title'], canon['artist'])
        else:
            title, artist = self._dj_clean_name(info)
            label = _song_label(title, artist)

        store, guard, router = self._audiophile_deps()
        free_client = getattr(router, 'google_client', None)
        paid_client = getattr(router, 'google_paid_client', None)
        if free_client is None and paid_client is None:
            return

        budget = getattr(self, '_auto_guide_budget', None)
        if budget is None:
            from audiophile_fetcher import AutoGuideBudget
            budget = AutoGuideBudget()
            self._auto_guide_budget = budget
        inflight = getattr(self, '_guide_inflight', None)
        if inflight is None:
            inflight = {}
            self._guide_inflight = inflight

        from audiophile_fetcher import song_guide_for_dj, GUIDE_TIMEOUT_S

        guide = await song_guide_for_dj(
            label, human=True,
            free_client=free_client, paid_client=paid_client,
            guard=guard, store=store,
            budget=budget, inflight=inflight,
            wait_s=GUIDE_TIMEOUT_S,
        )
        if not guide:
            logger.info(f"[NowPlayingGuide] {label} 查不到導聆稿，只報歌名")
            return

        if self._current_stream_info is not info:
            logger.info(f"[NowPlayingGuide] {label} 查稿期間換歌了，作廢")
            return

        text = strip_entry_cue(guide)
        try:
            audio = await self.bot.tts_engine.generate_audio(text)
        except Exception as e:
            logger.warning(f"[NowPlayingGuide] TTS 渲染失敗: {e}")
            return
        if not audio:
            logger.warning(f"[NowPlayingGuide] {label} TTS 沒產出音檔")
            return

        if self._current_stream_info is not info:
            logger.info(f"[NowPlayingGuide] {label} TTS 渲染期間換歌了，作廢")
            return

        with vc._protected_tts_window():
            ok = await vc.play_dj_on_tts_layer(audio, text=text)

        ch = getattr(vc, 'active_text_channel', None)
        if ok and ch is not None:
            try:
                await ch.send(f"🎧 **【馬文·導聆】** {text}")
            except Exception:
                logger.warning("[NowPlayingGuide] 貼頻道失敗", exc_info=True)

        logger.info(f"🎧 [NowPlayingGuide] {label} 導聆已上 TTS 層 ok={ok}")

    async def _album_tour_reject(self, speaker: str) -> bool:
        """巡禮中擋語音點歌（play/play_next）。只鎖點歌——停/跳照常（使用者 2026-09-28 定）。
        不換 IntentContext.mode：mode 一換所有沒宣告的 agent 都不出價，聊天/查詢也會死。"""
        tour = getattr(self, '_album_tour', None)
        if not tour:
            return False
        vc = self._vc()
        ch = vc.active_text_channel if vc else None
        if ch:
            await ch.send(f"📀 專輯巡禮《{tour.get('album', '')}》進行中，點歌先暫停；要結束巡禮就說「停」。")
        logger.info(f"📀 [AlbumTour] 巡禮中擋下 {speaker} 的點歌")
        return True

    @app_commands.command(name="tour", description="[DJ] 專輯巡禮：照曲序播整張專輯，每首先講 20 秒導聆")
    @app_commands.describe(artist="歌手（例如：周杰倫）", album="專輯名（例如：范特西）")
    async def tour(self, interaction: discord.Interaction, artist: str, album: str):
        await interaction.response.defer(ephemeral=False)
        vc = self._vc()
        if not vc:
            await interaction.followup.send("❌ 語音系統尚未就緒。", ephemeral=True)
            return
        if not interaction.guild.voice_client:
            await interaction.followup.send("❌ 馬文不在語音頻道中。請先使用 `/summon` 召喚我。", ephemeral=True)
            return
        if getattr(self, '_album_tour', None):
            await interaction.followup.send("📀 已經有專輯巡禮在進行了，要換就先說「停」。", ephemeral=True)
            return

        msg = await interaction.followup.send(f"📀 **正在查證曲目：** {artist}《{album}》...")

        tracks = await self._fetch_album_tracks(artist, album)
        if not tracks:
            await msg.edit(content=f"❌ 查不到 {artist}《{album}》可靠的曲目，巡禮取消。")
            return

        if self.radio_mode:
            await self.stop_radio(reason="Stream 模式接管")

        user = interaction.user.display_name
        self._album_tour = {"artist": artist, "album": album, "requested_by": user, "total": len(tracks)}
        self._album_tour_task = asyncio.create_task(self._run_album_tour(artist, tracks, user))

        listing = "\n".join(f"{i}. {t}" for i, t in enumerate(tracks, 1))
        await msg.edit(content=f"📀 **【專輯巡禮】** {artist}《{album}》共 {len(tracks)} 首，每首先導聆再從頭播（巡禮中點歌暫停，說「停」結束）：\n{listing}")

    async def _fetch_album_tracks(self, artist: str, album: str) -> list[str]:
        from audiophile_fetcher import fetch_album_tracklist   # 函式內 import（測試 patch 模組屬性）
        store, guard, router = self._audiophile_deps()
        return await fetch_album_tracklist(
            artist, album,
            free_client=getattr(router, 'google_client', None),
            paid_client=getattr(router, 'google_paid_client', None),
            guard=guard, store=store,
        )

    async def queue_current_album(self, speaker: str) -> None:
        """「播放這張專輯」=正在播的這首歌的專輯，挑幾首排進佇列（不打斷別人點歌，
        跟 /tour 整張巡禮不同）。CurrentAlbumAgent 的 handler 本體（10/8）。"""
        from audiophile_fetcher import pick_album_followups, resolved_matches_track

        vc = self._vc()

        async def _say(text: str) -> None:
            if vc:
                await vc.play_tts(text, already_in_channel=True)
            else:
                logger.info(f"📀 [CurrentAlbum] 無 vc，只 log：{text}")

        if await self._album_tour_reject(speaker):
            return

        info = self._current_stream_info
        if not info:
            await _say("現在沒在放歌，你說的是哪張專輯？")
            return

        album = info.get('album')
        artist = info.get('artist') or info.get('uploader') or ''
        if not album:
            await _say("這首我查不到是哪張專輯。")
            return

        ch = vc.active_text_channel if vc else None
        if ch:
            await ch.send(f"📀 查《{album}》的曲目中…")

        tracks = await self._fetch_album_tracks(artist, album)
        if not tracks:
            await _say(f"查不到《{album}》可靠的曲目。")
            return

        picks = pick_album_followups(
            tracks, f"{info.get('track') or ''} {info.get('title') or ''}")

        queued: list[str] = []
        for track in picks:
            try:
                res = await self._resolve_yt_query(f"{artist} {track}")
                if not res or not resolved_matches_track(res, track):
                    logger.info(f"📀 [CurrentAlbum] 「{artist} {track}」找不到對得上的音源，跳過")
                    continue
                res['requested_by'] = speaker
                res['track'] = track
                res['artist'] = artist
                self._queue_user_song(res)
                queued.append(track)
            except Exception as e:
                logger.warning(f"📀 [CurrentAlbum] 「{track}」準備失敗，跳過：{e}")
                continue

        if not queued:
            await _say(f"《{album}》的歌在 YouTube 都對不上，沒排進去。")
            return

        self._ensure_stream_loop()
        tail = "⋯" if len(queued) > 2 else ""
        await _say(f"好，《{album}》再排 {len(queued)} 首：{'、'.join(queued[:2])}{tail}")
        if ch:
            listing = "\n".join(f"{i}. {t}" for i, t in enumerate(queued, 1))
            await ch.send(f"📀 **【專輯接著聽】** 《{album}》：\n{listing}")

    def _album_tour_pending(self) -> bool:
        return any(i.get('_lane') == 'album_tour' for i in self.stream_queue)

    async def _run_album_tour(self, artist: str, tracks: list[str], requested_by: str) -> None:
        """JIT 生產線——播第 N 首時背景渲染第 N+1 首（解析+導聆稿+TTS），佇列裡最多
        一首未開播的巡禮曲。單首解析/渲染失敗就跳過該首，不中斷巡禮。全部播完才解除 _album_tour（放開點歌鎖）。
        stream_mode 變 False（loop 不在了）就不再等，避免鎖卡死。stop 由 _stop_album_tour cancel 本 task。"""
        from audiophile_fetcher import resolved_matches_track

        try:
            for track in tracks:
                try:
                    info = await self._resolve_yt_query(f"{artist} {track}")
                    if not info:
                        logger.info(f"📀 [AlbumTour] 找不到「{artist} {track}」，跳過")
                        continue
                    if not resolved_matches_track(info, track):
                        logger.info(f"📀 [AlbumTour] 「{artist} {track}」YouTube 配到別首（{info.get('title', '?')}），跳過")
                        vc = self._vc()
                        ch = vc.active_text_channel if vc else None
                        if ch:
                            await ch.send(f"📀 〈{track}〉在 YouTube 找不到對得上的音源，跳過這首。")
                        continue
                    # 巡禮已知乾淨名字：導聆 key / 查證字串 / 後續 DJ 都用它，不從 YouTube 髒標題洗
                    info['track'] = track
                    info['artist'] = artist
                    await self._prepare_audiophile_guide(info)
                except Exception as e:
                    logger.warning(f"📀 [AlbumTour] 「{track}」準備失敗，跳過：{e}")
                    continue
                # JIT：渲染已提前做完，等上一首巡禮曲開播（離開佇列）才入隊
                while self.stream_mode and self._album_tour_pending():
                    await asyncio.sleep(_ALBUM_TOUR_POLL_S)
                info['requested_by'] = requested_by
                info['_lane'] = 'album_tour'
                self._queue_user_song(info)
                self._ensure_stream_loop()
            # 最後一首播完才放開點歌鎖
            while self.stream_mode and (
                    self._album_tour_pending()
                    or (self._current_stream_info or {}).get('_lane') == 'album_tour'):
                await asyncio.sleep(_ALBUM_TOUR_POLL_S)
            logger.info("📀 [AlbumTour] 巡禮結束")
        finally:
            self._album_tour = None
            self._album_tour_task = None

    def _stop_album_tour(self, reason: str) -> None:
        """收掉巡禮：cancel runner、解除鎖、清掉佇列裡還沒播的巡禮曲
        （stop_stream 不清佇列，不清的話之後點歌會接著播到殘留巡禮曲）。沒巡禮 → no-op。"""
        task = getattr(self, '_album_tour_task', None)
        if getattr(self, '_album_tour', None) is None and task is None:
            return
        if task is not None and not task.done():
            task.cancel()
        self._album_tour = None
        self._album_tour_task = None
        self.stream_queue[:] = [i for i in self.stream_queue if i.get('_lane') != 'album_tour']
        logger.info(f"📀 [AlbumTour] 巡禮中止，原因: {reason}")

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
