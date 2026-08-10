"""MusicCog — 音樂子系統（從 VoiceController 抽離中）。

Phase 1–6 完成：MusicCog 持有所有音樂狀態並持有 5 個 slash commands。
音樂方法（_stream_loop、_radio_loop、_auto_recommend 等）仍在 VC，待 Phase 7+。

遷移進度：
  Phase 1 ✅  骨架 + stream_mode/radio_mode proxy
  Phase 2 ✅  stream subsystem state proxy (stream_queue, _current_stream_info, …)
  Phase 3 ✅  radio subsystem state proxy (radio_task, radio_paused, …)
  Phase 4 ✅  autoplay/recommendation state proxy (_recommend_spotlight_idx, _prefetch_cache, …)
  Phase 5 ✅  slash commands 遷移到 MusicCog (marvin_play/skip/play_control/recommend/radio)
  Phase 6 ✅  proxy boundary 穩定，無暫時 forwarding stub 需清除

後續（Phase 7+）：
  ⬜  _stream_loop / stop_stream / play_stream_song 方法遷移
  ⬜  _radio_loop / start_radio / stop_radio 方法遷移
  ⬜  _auto_recommend 方法遷移
  ⬜  IntentBus agents 直接讀寫 MusicCog（移除透過 VC proxy 的一跳）
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import random
import subprocess
import tempfile
import time
from typing import Optional

import yt_dlp

import discord
from discord import app_commands
from discord.ext import commands, tasks

from intent_agents.recommendation import (
    Recommendation,
    append_recommendation,
    time_of_day_bucket,
)
from memory_guard import is_memory_critical
from music_recommender import assign_unique_owners, build_member_pools, demote_low_quality_versions, find_recent_same_song, is_already_recommended, normalize_title, pick_candidates, ring_titles_for
from music_memory import extract_video_id
from intent_agents.find_song_agent import find_song_prompt
from intent_agents.lyrics_grounded_search import search_lyrics_grounded
from intent_agents.lyrics_seek import find_lyrics_timestamp
from persona_loader import load_dj_templates

logger = logging.getLogger(__name__)

_TASTE_PROFILE_CACHE = "records/taste_profiles.json"
_TASTE_FINGERPRINT_CACHE = "records/taste_fingerprint.json"
_SONG_BPM_STORE = "records/song_bpm.json"
_BPM_SAMPLE_SR = 11025
_DJ_TAIL_SFX_DIR = "assets/dj_sfx"
_DJ_TAIL_SFX_NAMES = ("scratch", "riser", "shoutout")
# 5s→8s：留更多餘裕給 _play_dj_tail_sfx 等下一首 preload 解碼完（見該處
# asyncio.wait_for），避免逼近歌1實際結束點才設 _dj_played_in_tail、跟主
# stream loop 換歌撞在一起（見 _run_tail_dj docstring）。
_DJ_TAIL_LEAD_S = 8.0
_DJ_TAIL_SFX_PRELOAD_WAIT_S = 2.0

_puck_mixer_client = None  # lazy singleton，見 _get_puck_client()


def _get_puck_client():
    """MARVIN_CAR_HARDWARE=pi_bt 才回傳 client；其餘硬體（ESP32 mk1/家用 Pi 3B）回 None、
    零行為改變。見 device/puck_mixer.py + project_car_puck_mk2_pi_zero2w_bt_mixer_validated。"""
    global _puck_mixer_client
    if os.getenv("MARVIN_CAR_HARDWARE", "").strip().lower() != "pi_bt":
        return None
    if _puck_mixer_client is None:
        from marvin_voice_core.puck_mixer_client import PuckMixerClient
        base_url = os.getenv("MARVIN_PUCK_BASE_URL", "")
        token = os.getenv("MARVIN_PUCK_TOKEN", "").strip() or None
        if not base_url:
            logger.warning("[PuckMixer] MARVIN_CAR_HARDWARE=pi_bt 但 MARVIN_PUCK_BASE_URL 未設，跳過")
            return None
        _puck_mixer_client = PuckMixerClient(base_url, token)
    return _puck_mixer_client


class MusicCog(commands.Cog):
    """音樂子系統（Strangler Fig 遷移中）。"""

    _PLAYED_EXCLUDE_TTL_S = 7 * 24 * 3600
    # T3 回收層放寬已播排除（讓 1-7 天前舊歌重回候選），但保留 24h 窗擋當天重播，
    # 否則 T1/T2 枯竭頻繁落 T3 時會把高播放數的歌同場一再回收（2026-06-24「鼓聲若響」2hr 播 11 次）。
    _T3_PLAYED_EXCLUDE_TTL_S = 24 * 3600
    _COLD_META_TIMEOUT_S = 5.0
    _SEAMLESS_SKIP_TIMEOUT_S = 10.0  # ⏭️ Seamless Skip 極端守護門檻 (10s)：確保第一首播放不提前中斷
    _MUSIC_CMD_DEDUP_WINDOW = 5.0
    _MUSIC_SAME_SONG_WINDOW = 30.0  # 同 speaker + 同正規化點歌字串：擋同一句重派（喚醒+無喚醒）
    # DJ 播報疊在歌上的音量（混音時 dj 分支的 gain）。降到 30% 不蓋過音樂。
    _DJ_INTERJECTION_VOLUME = 0.30

    # DJ 播報模板池資料源見 personas/dj_templates.yaml；選池邏輯/random.choice() 呼叫點不動
    _DJ_TEMPLATES = load_dj_templates()
    _DJ_EMPATHY_HOOK_TEMPLATES = tuple(_DJ_TEMPLATES["empathy_hooks"])

    _AUTOPILOT_DJ_PHRASES_PERSONAL = _DJ_TEMPLATES["autopilot_phrases"]["personal"]
    _AUTOPILOT_DJ_PHRASES_PERSONAL_NO_ARTIST = _DJ_TEMPLATES["autopilot_phrases"]["personal_no_artist"]
    _AUTOPILOT_DJ_PHRASES_GROUP = _DJ_TEMPLATES["autopilot_phrases"]["group"]
    _AUTOPILOT_DJ_PHRASES_GROUP_NO_ARTIST = _DJ_TEMPLATES["autopilot_phrases"]["group_no_artist"]
    _AUTOPILOT_DJ_PHRASES_LONG_TAIL = _DJ_TEMPLATES["autopilot_phrases"]["long_tail"]
    _AUTOPILOT_DJ_PHRASES_DISCOVERY = _DJ_TEMPLATES["autopilot_phrases"]["discovery"]
    _AUTOPILOT_DJ_PHRASES_SPOTLIGHT_ANCHOR = _DJ_TEMPLATES["autopilot_phrases"]["spotlight_anchor"]

    def __init__(self, bot):
        self.bot = bot
        # 跨切狀態 — VoiceController 透過 proxy property 讀寫這裡
        self.stream_mode: bool = False
        self.radio_mode: bool = False

        # 🎵 [Phase 2] Stream subsystem state (proxied from VoiceController)
        self.stream_volume: float = 0.10
        self._stream_play_gen: int = 0
        self._current_stream_url: Optional[str] = None
        self._stream_norm_gain: dict = {}   # url → 每首響度正規化常數增益
        self._last_user_song_seed: Optional[str] = None
        self.stream_queue: list = []        # list of {title, uploader, url, …}
        self._personal_shuffle: Optional[dict] = None  # 個人歌單連續隨機播 session
        self._personal_topup_inflight: bool = False     # 單飛守衛：同時只允許一個 topup
        self.stream_task = None
        self._tail_dj_task: Optional[asyncio.Task] = None  # [DJ Tail] 尾段串場排程 task
        self._current_stream_info = None
        self.stream_history: list = []      # 已播過的歌曲（用於上一首）
        self.stream_paused: bool = False
        self._current_lyrics: Optional[str] = None
        self._current_stream_comment: Optional[str] = None
        self._current_stream_start_time: Optional[float] = None  # HUD 進度條用
        self._active_control_view = None

        # 📻 [Phase 3] Radio subsystem state (proxied from VoiceController)
        self.radio_task = None
        self.radio_volume: float = 0.10
        self._radio_song_list: list = []
        self._radio_source = None
        self._radio_fade_task = None
        self.radio_paused: bool = False

        # 🎵 [Phase 4] Autoplay / recommendation state (proxied from VoiceController)
        self._recommend_spotlight_idx: int = -1
        self._mood_sensor = None
        self._cover_blacklist = None
        self._round_track_count: int = 0
        self._round_size: int = 3
        # 🎚️ [ThemedSet] 讀空氣主題歌單（env-gated MARVIN_THEMED_PLAYLIST，預設 OFF）
        self._THEMED_SET_COOLDOWN_S = 30 * 60   # 一張歌單約 30-40 分鐘，半小時內不重開
        self._THEMED_SET_NIGHTLY_CAP = 4        # 每晚上限，防抖動重打付費 LLM
        self._last_themed_set_ts: float = 0.0
        self._themed_sets_tonight: int = 0
        self._themed_set_date = None
        self._prefetch_cache: dict = {}   # url → Task[{'lyrics', 'comment'}]
        self._preload_music_cache: dict = {}   # url → Task[PreloadedF32MusicSource]
        # 🎵 [ReqGuard] 使用者點歌兩道防護（2026-07-04；邏輯在 music_request_guard.py）
        from music_request_guard import RecentRequestLedger, ResolveCache, QueryResolveCache
        self._req_ledger = RecentRequestLedger()    # 同人同曲 30s 去重（佇列空也擋）
        self._yt_resolve_cache = ResolveCache()     # videoId→info TTL 1h，重複點播免重抽 ~2s
        self._query_resolve_cache = QueryResolveCache()  # query→url 持久快取，點過的歌跳 ytsearch5(~6s)
        self._last_search: dict = {}      # username → {query, ts, source}
        self._last_music_cmd_time: dict[str, float] = {}  # speaker → ts, for dedup
        self._last_music_query: dict[str, tuple[str, float]] = {}  # speaker → (正規化點歌字串, ts)
        # 🐕 [Stream Watchdog] 使用者/系統主動停播（stop_stream）時設 True，抑制 watchdog
        # 自動復活；_ensure_stream_loop() 一旦真的（重）啟動迴圈就清掉（見該函式與
        # _stream_watchdog_loop，2026-08-01 佇列假死事故後補）。
        self._stream_user_stopped: bool = False

    def _vc(self):
        """取得 VoiceController cog；找不到回 None。"""
        return self.bot.cogs.get('VoiceController')

    @staticmethod
    def _autopilot_online_members(online: list[str]) -> list[str]:
        """autopilot 續推用的「在場者」清單：car 模式沒有 Discord 語音頻道，
        `vc.get_online_members()` 永遠回 []，會被 `_autorecommend_seed` 誤判成
        「空房」而永久停止續推（2026-07-25 車 puck 佇列播完停播事故）。
        車 puck 本身有 present/absent 心跳，會在這裡才進來就代表真的有人在車上，
        用 MARVIN_SATELLITE_SPEAKER（車載開場也用同一個 owner）當在場者。"""
        if online:
            return online
        if os.getenv("MARVIN_CAR_MODE", "").strip().lower() in ("1", "true", "yes", "on"):
            return [os.getenv("MARVIN_SATELLITE_SPEAKER", "狗與露")]
        return online

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

    # ── 🎵 Music subsystem methods ────────────────────────────────────────────

    async def start_radio(self, trigger: str = "未知觸發"):
        """📻 啟動電台：掃描歌單 → shuffle → 開始背景播放 Loop。"""
        import random
        if self.radio_mode:
            logger.warning("⚠️ [Radio] 電台已啟動，跳過重複啟動。")
            return

        songs_dir = "assets/songs"
        excluded = {"Oh Marvin.mp3"}
        try:
            all_songs = [
                os.path.join(songs_dir, f)
                for f in os.listdir(songs_dir)
                if f.endswith(".mp3") and f not in excluded
            ]
        except FileNotFoundError:
            logger.error(f"❌ [Radio] 找不到歌曲目錄: {songs_dir}")
            return

        if not all_songs:
            logger.warning("⚠️ [Radio] 歌單為空，無法啟動電台。")
            return

        random.shuffle(all_songs)
        self._radio_song_list = all_songs
        self.radio_mode = True
        logger.info(f"📻 [Radio] 電台啟動 (來源: {trigger})，共 {len(all_songs)} 首歌曲。")

        if self.radio_task and not self.radio_task.done():
            self.radio_task.cancel()
        self.radio_task = asyncio.create_task(self._radio_loop())

    async def stop_radio(self, reason: str = "未知原因"):
        """📻 停止電台：中斷播放 → 取消 Task → 重設狀態。"""
        if not self.radio_mode:
            return

        self.radio_mode = False
        self.radio_paused = False
        logger.info(f"📻 [Radio] 電台停止，原因: {reason}")

        if self.radio_task and not self.radio_task.done():
            self.radio_task.cancel()
            self.radio_task = None
        if self._radio_fade_task and not self._radio_fade_task.done():
            self._radio_fade_task.cancel()
            self._radio_fade_task = None
        self._radio_source = None

        guild_vc = next((v for v in self.bot.voice_clients if v.is_connected()), None)
        if guild_vc and guild_vc.is_playing():
            guild_vc.stop_playing()
            logger.info("📻 [Radio] 已立即停止當前播放的歌曲。")

    def _ensure_stream_loop(self) -> bool:
        """佇列有歌但 stream loop 沒在跑 → 叫醒它。回傳「是否剛叫醒」。

        2026-07-17 死鎖事故：/dismiss 會 cancel loop 但不清 stream_queue，使用者
        重點同一首歌撞上 _check_song_duplicate 早退 → 走不到重啟 loop 的程式碼 →
        佇列永遠卡著（唯一逃生出口是點一首不同的歌，但沒人會知道）。
        loop 活著時不動它——別把正在播的那首打斷。

        ⚠️ 判活看 **task**，不看 stream_mode：旗標會說謊。loop 被 cancel 時
        （非 stop_stream 那條）旗標停在 True 但沒人在播，只信旗標會 no-op →
        佇列卡死（2026-07-17 第二次事故）。
        """
        alive = self.stream_task is not None and not self.stream_task.done()
        if alive and self.stream_mode:
            return False
        if alive:
            self._cancel_stream_task("_ensure_stream_loop 收殘骸")   # 收尾中的殘骸 → 收掉重來
        self.stream_mode = True
        self.stream_volume = 0.10
        self._stream_user_stopped = False  # 有人／有東西要它跑了，解除 watchdog 抑制
        self.stream_task = asyncio.create_task(self._stream_loop())
        logger.warning(f"🎵 [Stream] loop 不在跑（flag={self.stream_mode} task_alive={alive}）"
                       f"→ 叫醒，佇列 {len(self.stream_queue)} 首")
        return True

    def _cancel_stream_task(self, reason: str) -> None:
        """統一 stream_task.cancel() 出口 + 記錄呼叫來源。

        2026-08-01 事故：迴圈在歌曲自然轉場瞬間被取消，但沒有任何 log 留下是誰
        呼叫的 `.cancel()`，只能靠時間軸推理、抓不到真兇。統一出口讓已知的呼叫
        點都留痕；仍抓不到的話至少能從留痕排除法縮小範圍。
        """
        if self.stream_task is not None and not self.stream_task.done():
            logger.info(f"🎵 [Stream] stream_task.cancel() 呼叫來源: {reason}")
            self.stream_task.cancel()

    async def stop_stream(self, reason: str = "未知原因"):
        """🎵 停止串流播放，清空當前狀態。"""
        if not self.stream_mode:
            return
        vc = self._vc()
        self.stream_mode = False
        self._stream_user_stopped = True  # 主動停播 → watchdog 別自己復活它
        self._personal_shuffle = None  # 🎲 停播一併收掉個人歌單 session，避免之後復活
        if vc is not None:
            vc.last_marvin_speech_time = time.time()
        self._current_stream_info = None
        self.stream_paused = False
        self._publish_now_playing_state(None)
        logger.info(f"🎵 [Stream] 停止，原因: {reason}")
        if self.stream_task and not self.stream_task.done():
            self._cancel_stream_task(f"stop_stream reason={reason}")
            self.stream_task = None
        if self._radio_fade_task and not self._radio_fade_task.done():
            self._radio_fade_task.cancel()
            self._radio_fade_task = None
        self._radio_source = None
        if vc is not None and vc._mixer is not None:
            vc._mixer.clear_music()

    async def _radio_volume_fade_loop(self):
        """📻 動態音量漸變：有人說話 → duck to 1%；靜默 1.5s 後 → fade up to 10%。"""
        IDLE_VOL  = 0.10
        DUCK_VOL  = 0.01
        TICK      = 0.05
        DUCK_RATE = 0.012
        RISE_RATE = 0.003
        DUCK_HOLD = 1.5
        try:
            while self.radio_mode or self.stream_mode:
                src = self._radio_source
                if src is not None:
                    vc = self._vc()
                    silence = time.time() - (vc.last_player_speech_time if vc is not None else 0.0)
                    target = IDLE_VOL if silence > DUCK_HOLD else DUCK_VOL
                    current = src.volume
                    if current > target + 0.001:
                        src.volume = max(target, current - DUCK_RATE)
                    elif current < target - 0.001:
                        src.volume = min(target, current + RISE_RATE)
                await asyncio.sleep(TICK)
        except asyncio.CancelledError:
            pass

    async def _radio_loop(self):
        """📻 背景播放迴圈：依序播放歌單，播完後 shuffle 重複。"""
        import random
        logger.info("📻 [Radio Loop] 電台迴圈已啟動。")
        try:
            while self.radio_mode:
                if not self._radio_song_list:
                    songs_dir = "assets/songs"
                    excluded = {"Oh Marvin.mp3"}
                    try:
                        all_songs = [
                            os.path.join(songs_dir, f)
                            for f in os.listdir(songs_dir)
                            if f.endswith(".mp3") and f not in excluded
                        ]
                    except FileNotFoundError:
                        logger.error("❌ [Radio Loop] 重新掃描失敗，停止電台。")
                        self.radio_mode = False
                        break
                    random.shuffle(all_songs)
                    self._radio_song_list = all_songs
                    logger.info(f"📻 [Radio Loop] 歌單播完，重新洗牌 ({len(all_songs)} 首)。")

                next_song = self._radio_song_list.pop()
                song_name = os.path.basename(next_song)
                logger.info(f"📻 [Radio Loop] 即將播放: {song_name}")

                vc = self._vc()
                if vc is not None:
                    metadata = self._extract_song_metadata(next_song)
                    cover_path = self._extract_song_cover(next_song)
                    active_ch = vc.active_text_channel
                else:
                    metadata = {"title": song_name, "artist": "未知"}
                    cover_path = None
                    active_ch = None

                if active_ch:
                    accent_color = (
                        self._extract_dominant_color(cover_path)
                        if cover_path
                        else discord.Color.dark_grey()
                    )
                    embed = discord.Embed(
                        title="📻 馬文電台：正在播放",
                        description="「...」",
                        color=accent_color,
                        timestamp=datetime.datetime.now(),
                    )
                    embed.add_field(name="🎵 歌曲名稱", value=f"`{metadata['title']}`", inline=False)
                    embed.add_field(name="👤 演出者", value=f"`{metadata['artist']}`", inline=True)
                    embed.add_field(name="🔊 當前音量", value=f"`{int(self.radio_volume * 100)}%`", inline=True)

                    if cover_path:
                        file = discord.File(cover_path, filename="cover.jpg")
                        embed.set_thumbnail(url="attachment://cover.jpg")
                        sent_msg = await active_ch.send(file=file, embed=embed)
                        if vc is not None:
                            asyncio.create_task(self._delayed_cleanup(cover_path))
                    else:
                        sent_msg = await active_ch.send(embed=embed)

                    async def _update_radio_comment(msg, title, artist, color, song_path, _vc_ref=vc):
                        from utils import pick_lyrics_snippet
                        lyrics_path = os.path.splitext(song_path)[0] + ".md"
                        section_name, snippet = pick_lyrics_snippet(lyrics_path)
                        if snippet:
                            song_ctx = f"歌名：{title}，演出者：{artist}，段落：{section_name}，歌詞：{snippet}"
                        else:
                            song_ctx = f"歌名：{title}，演出者：{artist}"
                        try:
                            comment = await self.bot.router.generate_dynamic_system_msg(
                                "radio_now_playing", context=song_ctx
                            )
                        except Exception:
                            return
                        try:
                            updated = discord.Embed(
                                title="📻 馬文電台：正在播放",
                                description=f"「{comment}」",
                                color=color,
                                timestamp=msg.embeds[0].timestamp if msg.embeds else datetime.datetime.now(),
                            )
                            updated.add_field(name="🎵 歌曲名稱", value=f"`{title}`", inline=False)
                            updated.add_field(name="👤 演出者", value=f"`{artist}`", inline=True)
                            updated.add_field(name="🔊 當前音量", value=f"`{int(self.radio_volume * 100)}%`", inline=True)
                            if msg.embeds and msg.embeds[0].thumbnail:
                                updated.set_thumbnail(url=msg.embeds[0].thumbnail.url)
                            await msg.edit(embed=updated)
                        except Exception as e:
                            logger.warning(f"⚠️ [Radio] embed 更新失敗: {e}")

                    asyncio.create_task(
                        _update_radio_comment(sent_msg, metadata["title"], metadata["artist"], accent_color, next_song)
                    )

                await self.play_radio_song(next_song)

                if self.radio_mode:
                    await asyncio.sleep(1.0)

        except asyncio.CancelledError:
            logger.info("📻 [Radio Loop] 電台迴圈被取消。")
            self.radio_paused = False
        except Exception as e:
            logger.error(f"❌ [Radio Loop] 發生異常: {e}")
            self.radio_mode = False
            self.radio_paused = False

    async def play_radio_song(self, file_path: str):
        """📻 播放單首電台歌曲，透過 VC mixer。"""
        if not os.path.exists(file_path):
            logger.warning(f"⚠️ [Radio Song] 找不到檔案: {file_path}")
            return

        vc = self._vc()
        device = vc._resolve_playback_device() if vc is not None else None
        if device is None:
            logger.warning("⚠️ [Radio Song] 無可用播放裝置（Discord VC / 本機喇叭皆無），跳過播放。")
            self.radio_mode = False
            self.radio_paused = False
            return

        src = discord.FFmpegPCMAudio(file_path, options="-vn")
        await vc._mixer_play_music(
            device, src,
            still_active=lambda: self.radio_mode,
            volume_attr="radio_volume",
        )

    # ── 🎵 Autopilot recommendation engine ───────────────────────────────────

    @staticmethod
    def _autorecommend_seed(requested_by: str | None, online_members: list[str]) -> str | None:
        """佇列空時決定要不要續推自動推薦、用誰當 seed user。回 None = 不續推。"""
        if not requested_by or requested_by == '未知':
            return None
        if requested_by.startswith('Marvin'):
            return online_members[0] if online_members else None
        return requested_by

    # 健康播放的最低秒數；低於此且非使用者 skip → 疑似 403/網路失敗（yt-dlp 串流網址
    # 過期，ffmpeg 開檔即 403 → play_stream_song 秒返）。
    _MIN_HEALTHY_PLAY_S = 3.0

    @classmethod
    def _should_retry_failed_song(
        cls, played_s: float, *, stream_active: bool, skipped: bool,
        requested_by: str | None, already_retried: bool,
    ) -> bool:
        """播的歌太短（疑 403/失敗）→ 該重抓網址重試一次，別靜默跳下一首。

        守門（全過才重試）：沒重試過 / 仍在串流(非 stop) / 非使用者 skip / 播放 < 健康門檻。
        **跟「誰點的」無關**——2026-07-07 bug：原本排除 Marvin 自動推薦，導致自動歌 403
        短播時靜默跳過（連 log 都沒有）。單次 force_fresh 重試對誰都安全（already_retried
        上鎖、每首只救一次、不會無限；使用者 skip 由 skipped 擋住不會誤重播）。
        """
        _ = requested_by  # 保留參數簽章相容；短播救援不再看點播者
        if already_retried or not stream_active or skipped:
            return False
        return played_s < cls._MIN_HEALTHY_PLAY_S

    @classmethod
    def _premature_cut(cls, played_s: float, duration) -> bool:
        """歌是否『中途被切』：播超過健康門檻(非開頭403)、卻遠短於真實總長(<80%)。

        用來讓「播到一半串流 URL 中途失效→跳下一首」變可見（ffmpeg stderr 進 DEVNULL＝
        log 隱形，且開頭 403 由 _should_retry_failed_song 處理，這裡只抓中途切）。
        """
        if not duration or duration <= 0:
            return False
        if played_s < cls._MIN_HEALTHY_PLAY_S:
            return False   # 開頭就掛→走 403 重試路徑，不算中途切
        return played_s < duration * 0.8

    def _load_taste_fingerprint(self) -> dict:
        """讀 records/taste_fingerprint.json（5 分鐘快取；缺檔/壞檔 → {} fail-open）。"""
        now = time.time()
        if hasattr(self, "_taste_fp_cache") and now - getattr(self, "_taste_fp_loaded_at", 0) < 300:
            return self._taste_fp_cache
        try:
            import json as _json
            with open(_TASTE_FINGERPRINT_CACHE, "r", encoding="utf-8") as f:
                self._taste_fp_cache = _json.load(f)
        except Exception:
            self._taste_fp_cache = {}
        self._taste_fp_loaded_at = now
        return self._taste_fp_cache

    def _current_bpm_filter(self) -> dict | None:
        """目前播放歌的 BPM（見 bpm_estimate.py 取樣落地）→ build_member_pools 的
        bpm_filter，讓下一輪候選偏好節奏接近的歌。目前歌沒 BPM 記錄（新歌/還沒取樣過）
        → None（不影響原排序，fail-open）。"""
        from bpm_estimate import read_bpm_store
        info = self._current_stream_info or {}
        vid = extract_video_id(info.get("webpage_url") or info.get("url") or "")
        if not vid:
            return None
        store = read_bpm_store(_SONG_BPM_STORE)
        entry = store.get(vid)
        if not isinstance(entry, dict) or entry.get("bpm") is None:
            return None
        return {"current_bpm": entry["bpm"], "store": store}

    async def _t2_discovery_candidates(self, members: list[str], exclude_titles: list[str]) -> list:
        """T2 discovery：多 seed → ytmusic radio 混合取相關新歌 → Candidate(direct_url)。"""
        mm = getattr(self.bot, 'music_memory', None)
        if mm is None:
            return []
        avoid_artists: list[str] = []
        if os.getenv("LLM_TASTE_T2", "off") == "on":
            try:
                import taste_profile
                _MAX_AGE = 8 * 86400
                avoid_artists = taste_profile.fresh_avoid_artists(_TASTE_PROFILE_CACHE, members, _MAX_AGE)
            except Exception as e:
                logger.warning(f"⚠️ [AutoRecommend] T2 LLM 品味快取讀取失敗，略過: {e}")
        try:
            _core = {a for a, _ in self._load_taste_fingerprint().get("core_artists", [])}
            for _a in mm.get_explore_avoid_artists():
                if _a not in _core and _a not in avoid_artists:
                    avoid_artists.append(_a)
        except Exception:
            logger.debug("[AutoRecommend] explore retreat avoid 合併失敗", exc_info=True)
        _N_SEEDS = 3
        # 多人種子輪替：每 N 首換主種子者(round-robin 在場者)、最後手動歌當 fresh lead
        # （N 首後淡出）、永遠混入其他在場者 → 不被單一人霸佔（見 seed_rotation.py）。
        import seed_rotation
        self._seed_epoch = getattr(self, '_seed_epoch', -1) + 1
        _since = getattr(self, '_auto_since_manual', _N_SEEDS)
        self._auto_since_manual = _since + 1
        # 各在場者的種子池＝他真人點過的歌（per-member，已排除 Marvin 自薦）；
        # LLM_TASTE_T2 on 時前置該人的 LLM 鄰近種子（curated taste）。
        _llm_on = os.getenv("LLM_TASTE_T2", "off") == "on"
        seeds_by_member = {}
        for _m in members:
            _pool = mm.get_played_seed_ids([_m], limit=20)
            if _llm_on:
                try:
                    import taste_profile
                    _pool = taste_profile.fresh_seed_ids(_TASTE_PROFILE_CACHE, [_m], 8 * 86400) + _pool
                except Exception:
                    pass
            seeds_by_member[_m] = _pool
        seeds = seed_rotation.order_rotating_seeds(
            members, seeds_by_member,
            epoch=self._seed_epoch, since_manual=_since,
            last_seed=getattr(self, '_last_user_song_seed', None),
            swap_every=_N_SEEDS, n=_N_SEEDS,
        )
        # rotating 不足 N 顆時用團體 liked 墊底
        if len(seeds) < _N_SEEDS:
            for vid in mm.get_liked_video_ids(members):
                if vid not in seeds:
                    seeds.append(vid)
                    if len(seeds) >= _N_SEEDS:
                        break
        logger.info(f"🎲 [AutoRecommend] 種子輪替 epoch={self._seed_epoch} "
                    f"主={seed_rotation.primary_member(members, self._seed_epoch, _N_SEEDS)} "
                    f"since_manual={_since} seeds={len(seeds)}")
        if not seeds:
            return []
        from ytmusic_radio import ytmusic_radio, blend_radio_results
        results = []
        for sd in seeds:
            try:
                r = await asyncio.to_thread(
                    ytmusic_radio, sd,
                    exclude_titles=exclude_titles, limit=self._round_size * 2,
                )
            except Exception as e:
                logger.warning(f"⚠️ [AutoRecommend] T2 radio seed={sd} 失敗，跳過: {e}")
                continue
            if r:
                results.append(r)
        if not results:
            logger.warning("⚠️ [AutoRecommend] T2 全 seed radio 空/失敗，退 T3")
            return []
        radio = blend_radio_results(results, exclude_titles=exclude_titles, limit=self._round_size * 3)
        if avoid_artists:
            import taste_profile
            _before = len(radio)
            radio = taste_profile.filter_avoided(radio, avoid_artists)
            if len(radio) < _before:
                logger.info(f"🚫 [AutoRecommend] T2 avoid 排除 {_before - len(radio)} 首（{avoid_artists}）")
        if not radio:
            return []
        logger.info(f"🎵 [AutoRecommend] T2 discovery: {len(seeds)} seeds 混合 → {len(radio)} 首相關新歌候選")
        from music_recommender import Candidate
        return [
            Candidate(anchor_title=c["title"], anchor_artist=c["artist"],
                      lane="discovery", mode="direct", target_member=None,
                      score=0.0, direct_url=c["url"])
            for c in radio
        ]

    # T4 排行榜輪替查詢（華語）——get_charts('TW') 回全球 playlists/藝人不乾淨，改華語搜尋 proxy。
    _T4_CHART_QUERIES = ("華語抒情精選", "華語流行 熱門", "華語 情歌 精選")

    @staticmethod
    def _extract_top_artists(songs: list, n: int = 4) -> list[str]:
        """從歌曲 list（get_top_songs_for_user 回的）抽 top 藝人（artist_of，去重保序、取前 n）。"""
        from taste_fingerprint import artist_of
        out: list[str] = []
        for s in songs:
            a = artist_of(s.get('title', '') if isinstance(s, dict) else '')
            if a and a not in out:
                out.append(a)
        return out[:n]

    async def _t4_fresh_discovery(self, members: list[str], spotlight: str, exclude_titles: list[str]) -> list:  # noqa: ARG002
        """T4 冒險發現：輪到的人(spotlight)的 top 藝人 + 排行榜 → search「還沒播過」的新歌。

        只在 T1/T2/T3 全枯竭才觸發（罕見）→ 值得冒險注入全新歌（使用者訂「觸發難就冒險」）。
        來源＝①spotlight 個人常聽藝人（在場者隨 spotlight 輪替→輪到每個人的歌手）②排行榜輪替。
        排除 avoid_artists（skip≥2 的藝人）。全失敗回 [] → 退最終回收保險。
        """
        mm = getattr(self.bot, 'music_memory', None)
        artists = self._extract_top_artists(
            mm.get_top_songs_for_user(spotlight, limit=20), n=4) if mm is not None else []
        if not artists:  # 該人無史 → 退全域口味指紋核心藝人
            artists = [a for a, _ in self._load_taste_fingerprint().get("core_artists", []) if a][:4]
        # 避開歌手：deterministic skip-avoid + LLM avoid_artists（同 T2 gate/快取）
        avoid_artists = list(mm.get_explore_avoid_artists()) if mm is not None else []
        if os.getenv("LLM_TASTE_T2", "off") == "on":
            try:
                import taste_profile
                _MAX_AGE = 8 * 86400
                # LLM 相近歌手（破回音室、挖他沒聽但會愛的）併入 search 來源
                for _a in taste_profile.fresh_adjacent_artists(_TASTE_PROFILE_CACHE, [spotlight], _MAX_AGE):
                    if _a not in artists:
                        artists.append(_a)
                for _a in taste_profile.fresh_avoid_artists(_TASTE_PROFILE_CACHE, members, _MAX_AGE):
                    if _a not in avoid_artists:
                        avoid_artists.append(_a)
            except Exception as e:
                logger.warning(f"⚠️ [AutoRecommend] T4 LLM taste 讀取失敗，略過: {e}")
        # 排行榜：隨 spotlight 輪替換一條華語 chart 查詢（輪到不同人配不同榜）
        chart_q = self._T4_CHART_QUERIES[self._recommend_spotlight_idx % len(self._T4_CHART_QUERIES)]
        _avoid_set = set(avoid_artists)
        queries = [q for q in (artists + [chart_q]) if q and q not in _avoid_set]
        if not queries:
            return []
        from ytmusic_radio import ytmusic_search_songs, blend_radio_results
        results = []
        for _q in queries:
            try:
                r = await asyncio.to_thread(
                    ytmusic_search_songs, _q,
                    exclude_titles=exclude_titles, limit=self._round_size * 2,
                )
            except Exception as e:
                logger.warning(f"⚠️ [AutoRecommend] T4 search '{_q}' 失敗，跳過: {e}")
                continue
            if r:
                results.append(r)
        if not results:
            logger.warning("⚠️ [AutoRecommend] T4 全 search 空/失敗，退最終回收")
            return []
        fresh = blend_radio_results(results, exclude_titles=exclude_titles, limit=self._round_size * 3)
        # LLM/skip avoid_artists 也套在結果上（chart 查詢可能回避開歌手的歌）
        if avoid_artists:
            import taste_profile
            _before = len(fresh)
            fresh = taste_profile.filter_avoided(fresh, avoid_artists)
            if len(fresh) < _before:
                logger.info(f"🚫 [AutoRecommend] T4 avoid 排除 {_before - len(fresh)} 首（{avoid_artists[:5]}）")
        if not fresh:
            return []
        logger.info(f"🎵 [AutoRecommend] T4 冒險發現: spotlight={spotlight} 藝人+LLM相近={artists} +排行榜『{chart_q}』 avoid={len(avoid_artists)} → {len(fresh)} 首未播新歌候選")
        from music_recommender import Candidate
        return [
            Candidate(anchor_title=c["title"], anchor_artist=c["artist"],
                      lane="discovery", mode="direct", target_member=None,
                      score=0.0, direct_url=c["url"])
            for c in fresh
        ]

    async def _llm_coverify(self, cand, exclude_titles: list[str]) -> str:
        """spotlight lane：請 LLM 推薦選定錨點歌的 cover 版本。回 "" 表示無推薦。"""
        slot = self.bot.music_memory.time_slot(time.time())
        prompt = (
            f"請推薦《{cand.anchor_title}》的【翻唱／cover 版本】（由其他藝人演繹）。\n"
            f"當前時段：{slot}\n"
            f"禁止推薦這些版本：{', '.join(exclude_titles[:20]) or '無'}\n"
            "規則：\n"
            "1. 優先推薦該歌的知名 cover（指定翻唱者更佳）。\n"
            "2. 若無合適 cover，推薦相同曲風／相關藝人的歌。\n"
            "回答格式（一行）：「翻唱藝人 - 歌名 (cover)」或「藝人 - 歌名」。不需要解釋。\n"
            "若真的沒有合適選擇請回答「無推薦」。"
        )
        rec = await self.bot.router._call_llm(
            system_prompt=f"你是 cover/翻唱推薦助手，聚焦在《{cand.anchor_title}》。",
            user_prompt=prompt,
            tier="simple",
        )
        rec = (rec or "").strip()
        return "" if (not rec or "無推薦" in rec) else rec

    def _recommend_blurb(self, cand, title: str, spotlight: str = "",
                         personal: bool = True) -> str:
        """依 lane 產生推薦時的自我說明文案。

        personal=False（歌不在掛名對象的點播歷史）→ 不指名、點給大家
        （2026-07-02 使用者訂：掛名「為X」必須是 X 點過的歌）。
        """
        if cand.lane == "group_resonance":
            return f"🎵 **【馬文精選】** 你們都有共鳴的《{title}》，再聽一次吧。"
        if not personal:
            if cand.lane == "discovery":
                return f"🎵 **【馬文精選】** 挖到新歌《{title}》，點給大家聽聽看。"
            return f"🎵 **【馬文精選】** 翻出《{title}》，點給大家。"
        who = cand.target_member or spotlight or "你"
        if cand.lane == "long_tail":
            return f"🎵 **【馬文精選】** 為 `{who}` 從塵封歌單挖出《{title}》。"
        if cand.lane == "discovery":
            return f"🎵 **【馬文精選】** 為 `{who}` 挖到新歌《{title}》，聽聽看。"
        return f"🎵 **【馬文精選】** 為 `{who}` 翻出的《{title}》。"

    def _themed_gate_open(self, now: float) -> bool:
        """🎚️ 主題歌單觸發閘：env on + 過冷卻 + 未超每晚上限（跨日自動重置）。"""
        if os.getenv("MARVIN_THEMED_PLAYLIST") != "1":
            return False
        today = datetime.date.fromtimestamp(now)
        if today != self._themed_set_date:
            self._themed_set_date = today
            self._themed_sets_tonight = 0
        if now - self._last_themed_set_ts < self._THEMED_SET_COOLDOWN_S:
            return False
        if self._themed_sets_tonight >= self._THEMED_SET_NIGHTLY_CAP:
            return False
        return True

    def _load_summary_entries(self):
        """讀 chat_summary_log → 日記 DiaryEntry（有 ts_str/core/speakers）。失敗回 []。"""
        try:
            from pathlib import Path
            from diary_comic.parser import parse_log
            return parse_log(Path("records/chat_summary_log.txt").read_text(encoding="utf-8"))
        except Exception:
            return []

    @staticmethod
    def _taste_match_owner(title: str, member_likes: dict, order: list) -> str | None:
        """歌名 vs 各成員 suki likes 的歌手強匹配。歌手名(≥2 字)== 抽出歌手 或 出現在歌名 →
        回該成員（依 order 優先）；否則 None。混雜的非音樂興趣(露營/股票)幾乎不會出現在歌名。"""
        from taste_fingerprint import artist_of
        artist = artist_of(title or "")
        for m in order:
            for like in (member_likes.get(m) or []):
                like = str(like).strip()
                if len(like) < 2:
                    continue
                if like == artist or like in (title or ""):
                    return m
        return None

    def _attribution_with_suki(self, mm, info: dict, spotlight: str) -> str:
        """autopilot 掛名：①真的點過→為X（既有）②否則歌手強匹配某在場者 suki 愛歌手→為X
        （記憶影響「為誰點」）③再否則點給大家。強匹配才掛（掛錯名比不掛名傷）。"""
        from music_memory import recommend_attribution, GROUP_ATTRIBUTION
        base = recommend_attribution(mm, info, spotlight)
        if base != GROUP_ATTRIBUTION:
            return base                      # 真的點過 → 保留既有掛名
        suki = getattr(getattr(self.bot, 'router', None), 'memory', None)
        if suki is None:
            return base
        vc = self._vc()
        members = (vc.get_online_members() if vc is not None else []) or ([spotlight] if spotlight else [])
        order = ([spotlight] if spotlight else []) + [m for m in members if m != spotlight]
        likes_map = {}
        for m in order:
            try:
                likes_map[m] = (suki.get_player_memory(m) or {}).get('likes', []) or []
            except Exception:
                likes_map[m] = []
        owner = self._taste_match_owner(info.get('title', ''), likes_map, order)
        return f"Marvin推薦（為{owner}）" if owner else base

    def _enqueue_themed_infos(self, infos: list, theme_title: str, spotlight: str,
                              exclude_titles: list, mm) -> list:
        """成塊入隊：套需 cog 狀態的閘（佇列/正在播去重、ring）+ 標 set 欄位。

        回『實際入隊』的 info 清單（caller 取 len() 當首數、並落日記 record）。
        """
        enqueued: list = []
        for info in infos:
            if self._check_song_duplicate(url=info.get('url', ''), title=info.get('title', ''),
                                          username=spotlight, webpage_url=info.get('webpage_url', '')):
                continue
            if is_already_recommended(info.get('title', ''), exclude_titles):
                continue
            # 掛名規則：themed 選歌通常非 spotlight 點過 → recommend_attribution 走點給大家，
            # 但 _attribution_with_suki 會再用 suki 愛歌手強匹配補「為X」
            info['requested_by'] = self._attribution_with_suki(mm, info, spotlight)
            info['_lane'] = 'themed'
            info['_spotlight'] = spotlight
            info['_set_id'] = theme_title
            info['_round_first'] = (len(enqueued) == 0)
            self.stream_queue.append(info)
            for _rt in ring_titles_for(info.get('title', ''), 'direct', info.get('title', '')):
                mm.add_recent_recommendation(_rt)
            enqueued.append(info)
        if enqueued:
            self._republish_queue_snapshot()
        return enqueued

    @staticmethod
    def _build_themed_announcement(theme_title: str, infos: list) -> str:
        """今夜歌單文字貼文：主題 + 每首歌名與策展理由（_pick_reason）。截到 Discord 2000 上限內。"""
        n = len(infos)
        lines = [f"🎚️ **【今夜歌單】** 我聽你們聊了一晚，為你們策展《{theme_title}》共 {n} 首："]
        for i, info in enumerate(infos, 1):
            title = (info.get('title') or '?').strip()[:60]
            reason = (info.get('_pick_reason') or '').strip()
            lines.append(f"`{i}.` **{title}**" + (f"\n> {reason}" if reason else ""))
        text = "\n".join(lines)
        return (text[:1900] + "…") if len(text) > 1900 else text

    async def _announce_themed_set(self, theme_title: str, enqueued_infos: list) -> None:
        vc = self._vc()
        # 同卡片 fallback：active_text_channel 未設(語音召喚)時退語音頻道內建文字區
        ch = None
        if vc is not None:
            ch = vc.active_text_channel or getattr(getattr(vc, 'voice_client', None), 'channel', None)
        if ch:
            try:
                await ch.send(self._build_themed_announcement(theme_title, enqueued_infos))
            except Exception:
                logger.debug("[ThemedSet] 宣告貼文失敗（忽略）", exc_info=True)

    async def _try_themed_set(self, members: list, exclude_titles: list,
                              spotlight: str, mm) -> int:
        """🎚️ 嘗試策展一張主題歌單入隊。回入隊首數（0 = 沒做 → caller 走一般 autopilot）。

        全程優雅降級：閘關 / 無主題 / LLM 失敗 / resolve 不足 / 任何例外 → 回 0，不中斷音樂。
        """
        if not self._themed_gate_open(time.time()):
            return 0
        try:
            from themed_playlist import (curate_themed_set, gather_theme_brief,
                                         record_themed_set, resolve_themed_set)
            from track_quality import is_non_song_video
            from music_memory import extract_video_id
            from llm_pool import call_paid_review

            brief = gather_theme_brief(self._load_summary_entries(),
                                       self._load_taste_fingerprint(), members, now=time.time())
            if brief is None:
                return 0
            themed = await curate_themed_set(brief, exclude_titles,
                                             call_fn=call_paid_review, set_size=self._round_size * 2)
            if themed is None or not themed.picks:
                return 0
            exclude_vids = mm.get_skipped_video_ids() | mm.get_recently_played_video_ids(
                self._PLAYED_EXCLUDE_TTL_S)
            infos = await resolve_themed_set(
                themed, resolve_fn=self._resolve_yt_query, exclude_vids=exclude_vids,
                is_non_song_fn=is_non_song_video, extract_vid_fn=extract_video_id)
            enqueued_infos = self._enqueue_themed_infos(infos, themed.theme_title, spotlight,
                                                        exclude_titles, mm)
            n = len(enqueued_infos)
            if n == 0:
                logger.info("🎚️ [ThemedSet] resolve+閘後 0 首可入隊 → fallback 一般 autopilot")
                return 0
            record_themed_set(themed.theme_title, enqueued_infos, ts=time.time())  # 落日記「今夜歌單」
            self._themed_sets_tonight += 1
            self._last_themed_set_ts = time.time()
            logger.info(f"🎚️ [ThemedSet]《{themed.theme_title}》入隊 {n} 首"
                        f"（今晚第 {self._themed_sets_tonight} 張）")
            await self._announce_themed_set(themed.theme_title, enqueued_infos)
            return n
        except Exception:
            logger.exception("[ThemedSet] 失敗，fallback 一般 autopilot")
            return 0

    async def _auto_recommend(self, username: str, *, _tier: int = 1):
        """佇列空 → 依在場成員的音樂記憶推薦下一首批。"""
        mm = getattr(self.bot, 'music_memory', None)
        if mm is None:
            return

        vc = self._vc()
        members = (vc.get_online_members() if vc is not None else []) or [username]

        self._recommend_spotlight_idx = (self._recommend_spotlight_idx + 1) % len(members)
        spotlight = members[self._recommend_spotlight_idx]

        recently = [s['title'] for s in list(self.stream_history)[-15:]]
        recommended = mm.get_recent_recommendation_titles()
        skipped = mm.get_skipped_titles(members)
        suki_hist: list[str] = []
        _suki = getattr(self.bot.router, 'memory', None)
        if _suki is not None:
            for m in members:
                suki_hist += (_suki.get_song_history(m) or [])[:10]
        exclude_titles = list(dict.fromkeys(recently + recommended + skipped + suki_hist))

        # 🎚️ [ThemedSet] 新一輪起手先試讀空氣主題歌單（env-gated，閘關/失敗回 0 → 走原 autopilot）
        if _tier == 1:
            _n_themed = await self._try_themed_set(members, exclude_titles, spotlight, mm)
            if _n_themed > 0:
                return

        vibe_filter = None
        vibe_label = None
        if self._mood_sensor is not None:
            try:
                self._mood_sensor.invalidate()
                active_ch = vc.active_text_channel if vc is not None else None
                guild_id = active_ch.guild.id if active_ch else 0
                vibe_label = await self._mood_sensor.current_vibe(guild_id=guild_id)
                vibe_filter = {"mood": vibe_label.mood, "topic": vibe_label.topic, "min_score": 0.0}
                logger.info(f"🎵 [AutoRecommend] vibe={vibe_label.mood} (engagement={vibe_label.engagement:.2f}, source={vibe_label.source})")
            except Exception as e:
                logger.warning(f"⚠️ [AutoRecommend] vibe sensor 失敗，fallback to no vibe filter: {e}")

        bpm_filter = self._current_bpm_filter()

        # per-member 候選 → 跨使用者唯一歸屬：同一首歌只歸一人（round-robin 平手代表），
        # 避免團體歌被分別指定給不同使用者重播。當輪只取 spotlight 自己的去重後候選。
        _member_pools = build_member_pools(
            members=members,
            songs=mm.all_songs(),
            exclude_titles=exclude_titles,
            now=time.time(),
            vibe_filter=vibe_filter,
            bpm_filter=bpm_filter,
        )
        pool = assign_unique_owners(_member_pools, rotation_order=members).get(spotlight, [])

        _skipped_vids = mm.get_skipped_video_ids()
        _taste_fp = self._load_taste_fingerprint()

        # k 多抽 3 倍當緩衝：入隊前把 cover/現場版降到隊尾，好版本先填滿 round（見下方 demote）。
        _k_buf = self._round_size * 3
        if _tier == 1:
            cands = pick_candidates(pool, k=_k_buf, top_n=max(9, _k_buf))
            ring_exclude = exclude_titles
            excluded_vids = _skipped_vids | mm.get_recently_played_video_ids(self._PLAYED_EXCLUDE_TTL_S)
            _played_titles = mm.get_recently_played_titles(self._PLAYED_EXCLUDE_TTL_S)
        elif _tier == 2:
            cands = await self._t2_discovery_candidates(members, exclude_titles)
            ring_exclude = exclude_titles
            excluded_vids = _skipped_vids | mm.get_recently_played_video_ids(self._PLAYED_EXCLUDE_TTL_S)
            _played_titles = mm.get_recently_played_titles(self._PLAYED_EXCLUDE_TTL_S)
        elif _tier == 3:
            # 放寬到 24h 而非砍光：仍回收 1-7 天前舊歌，但擋當天剛播過的，防同場收斂重播。
            # 候選池(歌名)與 enqueue 迴圈(video-id)同步排除 24h 已播，否則池子挑出剛播歌、
            # 迴圈又擋掉 → enqueue=0 → T3 無 fallback → 停播（2026-06-24 回報）。
            _t3_played = mm.get_recently_played_titles(self._T3_PLAYED_EXCLUDE_TTL_S)
            _t3_exclude = list(dict.fromkeys(skipped + _t3_played))
            _relaxed_pools = build_member_pools(
                members=members, songs=mm.all_songs(),
                exclude_titles=_t3_exclude,
                now=time.time(), vibe_filter=vibe_filter, bpm_filter=bpm_filter,
            )
            relaxed_pool = assign_unique_owners(_relaxed_pools, rotation_order=members).get(spotlight, [])
            cands = pick_candidates(relaxed_pool, k=_k_buf, top_n=max(9, _k_buf))
            ring_exclude = _t3_exclude
            excluded_vids = _skipped_vids | mm.get_recently_played_video_ids(self._T3_PLAYED_EXCLUDE_TTL_S)
            _played_titles = _t3_played
        else:
            # T4 冒險發現：T1-T3(個人史/radio 收斂)全枯竭→用核心藝人 catalog 搜「未播新歌」。
            # 罕見觸發＝值得冒險注入全新歌，不只回收舊歌（2026-07-08：個人史子集耗盡、radio 種子
            # 收斂到同批已播熱門歌；使用者訂「觸發難就冒險」）。新歌不在 24h 已播內、排除照舊。
            cands = await self._t4_fresh_discovery(members, spotlight, exclude_titles)
            ring_exclude = exclude_titles
            excluded_vids = _skipped_vids | mm.get_recently_played_video_ids(self._PLAYED_EXCLUDE_TTL_S)
            _played_titles = mm.get_recently_played_titles(self._PLAYED_EXCLUDE_TTL_S)

        # 🎚️ [Quality] cover/現場版降到隊尾——自動推薦 cover 11% vs 真人 3%，humans 避開。
        # 好版本先填滿 round；沒更好的時 cover/live 仍會播（不丟棄→不枯竭）。
        cands = demote_low_quality_versions(cands)
        if not cands:
            if _tier < 4:
                return await self._auto_recommend(username, _tier=_tier + 1)
            logger.debug("🎵 [AutoRecommend] 四層皆無候選，跳過（退最終回收保險）")
            return

        self._round_track_count = 0

        if self._cover_blacklist is None:
            try:
                from track_quality import CoverBlacklist
                self._cover_blacklist = CoverBlacklist.shared()
            except Exception:
                logger.exception("[AutoRecommend] CoverBlacklist init 失敗")

        enqueued = 0
        for cand in cands:
            if enqueued >= self._round_size:
                break
            if cand.direct_url:
                query = cand.direct_url
            elif cand.mode == "cover":
                query = await self._llm_coverify(cand, exclude_titles)
            else:
                query = f"{cand.anchor_artist} {cand.anchor_title}".strip() or cand.anchor_title
            if not query:
                continue

            try:
                info = await self._resolve_yt_query(query)
            except Exception as e:
                logger.debug(f"⚠️ [AutoRecommend] _resolve_yt_query fail '{query}': {e}")
                continue
            if not info:
                continue
            if self._check_song_duplicate(url=info['url'], title=info['title'], username=username, webpage_url=info.get('webpage_url', '')):
                logger.info(f"🎵 [AutoRecommend] {info['title']} 本場已播過，略過")
                continue
            if is_already_recommended(info['title'], ring_exclude):
                logger.info(f"🎵 [AutoRecommend] {info['title']} 已在 recent ring，略過")
                continue
            _cand_vid = extract_video_id(info.get('webpage_url') or info.get('url') or '')
            if _cand_vid and _cand_vid in excluded_vids:
                logger.info(f"🎵 [AutoRecommend] {info['title']} video-id 已播過/已skip，略過")
                continue
            _same = find_recent_same_song(info['title'], _played_titles)
            if _same:
                logger.info(f"🎵 [AutoRecommend] {info['title']} 與最近播過『{_same[:30]}』同歌不同上傳，略過")
                continue
            from track_quality import is_non_song_video
            _ns, _ns_reason = is_non_song_video(info.get('title', ''), info.get('duration'))
            if _ns:
                logger.info(f"🚫 [AutoRecommend] 非單曲略過 '{info['title']}': {_ns_reason}")
                continue
            if _tier == 2:
                from taste_fingerprint import explore_matches_floor
                if not explore_matches_floor(info.get('title', ''), _taste_fp):
                    logger.info(f"🎵 [AutoRecommend] explore 不合口味地板(語言)略過: {info['title']}")
                    continue

            if self._cover_blacklist is not None:
                try:
                    from track_quality import assess_track_quality
                    passes, reason = await assess_track_quality(
                        info['url'], info['title'],
                        blacklist=self._cover_blacklist,
                    )
                    if not passes:
                        logger.info(f"🚫 [AutoRecommend] Quality block '{info['title']}': {reason}")
                        continue
                except Exception:
                    logger.exception("[AutoRecommend] quality filter raised — fail-open")

            # 掛名（2026-07-02+07-09）：X 點過→為X；否則歌手強匹配某在場者 suki 愛歌手→為X；再否則點給大家
            info['requested_by'] = self._attribution_with_suki(mm, info, spotlight)
            info['_round_first'] = (enqueued == 0)
            info['_spotlight'] = spotlight
            info['_lane'] = cand.lane
            info['_anchor_title'] = cand.anchor_title
            info['_round_position'] = enqueued

            self.stream_queue.append(info)
            for _ring_title in ring_titles_for(info['title'], cand.mode, cand.anchor_title):
                mm.add_recent_recommendation(_ring_title)
            logger.info(f"🎵 [AutoRecommend] lane={cand.lane} round-#{enqueued+1}: {info['title']}")
            blurb = ""
            if enqueued == 0:
                vibe_tag = f" [vibe: {vibe_label.mood}]" if vibe_label else ""
                # 文案與掛名同規則：blurb 指名的人（target_member 優先）也要真的點過這首
                _blurb_who = cand.target_member or spotlight
                _personal = bool(_blurb_who) and mm.is_requester(info, _blurb_who)
                blurb = self._recommend_blurb(cand, info['title'], spotlight=spotlight,
                                              personal=_personal) + vibe_tag
                # 2026-07-08 使用者：這段推薦文字不貼頻道了——推薦會播出來(DJ 語音)+有歌曲卡，文字多餘。
                # blurb 仍計算，供日記/推薦紀錄 append_recommendation 用。

            _recent_titles = [
                s.get("title", "") for s in self.stream_history[-3:] if isinstance(s, dict)
            ]
            append_recommendation(self._build_autopilot_rec(
                spotlight=spotlight, title=info['title'], lane=cand.lane, mode=cand.mode,
                anchor_title=cand.anchor_title, blurb=blurb, now=time.time(),
                channel_state_extras={
                    "vibe_mood": vibe_label.mood if vibe_label else None,
                    "vibe_engagement": round(vibe_label.engagement, 2) if vibe_label else None,
                    "queue_position": enqueued,
                    "round_first": info['_round_first'],
                    "queue_depth": len(self.stream_queue),
                    "recent_history_titles": _recent_titles,
                    "spotlight_member": spotlight,
                },
            ))

            next_url = info.get('url', '')
            if next_url and next_url not in self._prefetch_cache and vc is not None:
                self._prefetch_cache[next_url] = asyncio.create_task(self._fetch_song_meta(info))

            enqueued += 1

        logger.info(f"🎵 [AutoRecommend] T{_tier} round 完成: enqueued={enqueued}/{self._round_size}")
        if enqueued:
            self._republish_queue_snapshot()
        if enqueued == 0 and _tier < 4:
            await self._auto_recommend(username, _tier=_tier + 1)

    @staticmethod
    def _build_autopilot_rec(*, spotlight, title, lane, mode, anchor_title, blurb, now,
                              channel_state_extras=None) -> "Recommendation":
        """把 autopilot 推薦包成 Recommendation（offline feedback 用）。"""
        channel_state = dict(channel_state_extras or {})
        channel_state["lane"] = lane
        channel_state["mode"] = mode
        channel_state["time_of_day"] = time_of_day_bucket(now)
        return Recommendation(
            ts=now, agent="music", speaker=spotlight,
            trigger="queue_empty", selected=title,
            reason_internal=f"queue_empty:{lane}:{mode}:{anchor_title}",
            explanation_uttered=blurb, feedback_window_s=300,
            channel_state=channel_state,
        )

    # ── 🎲 個人歌單連續隨機播 ────────────────────────────────────────────────

    async def start_personal_shuffle(self, username: str) -> tuple[bool, str]:
        """連續隨機播放某使用者點過的『全部』歌（不重複、播完為止）。

        一次只墊一首待播（見 _personal_shuffle_topup），不塞爆佇列，別人現場點歌照樣
        進得來。池子＝music_memory 裡 requesters 含該使用者的所有歌，純隨機洗牌。
        """
        mm = getattr(self.bot, 'music_memory', None)
        if mm is None:
            return (False, "音樂記憶尚未就緒。")
        pool = [s for s in mm.all_songs().values()
                if username in (s.get("requesters") or {})]
        if not pool:
            return (False, f"{username} 還沒點過任何歌，沒有歌單可以播。")
        import random
        random.shuffle(pool)
        self._personal_shuffle = {"user": username, "remaining": pool}
        logger.warning(f"🎲 [PersonalShuffle] start user={username} pool={len(pool)} stream_mode={self.stream_mode}")
        await self._personal_shuffle_topup()
        if not self.stream_mode:
            self.stream_mode = True
            self.stream_volume = 0.10
            self._stream_user_stopped = False
            if self.stream_task and not self.stream_task.done():
                self._cancel_stream_task("start_personal_shuffle")
            self.stream_task = asyncio.create_task(self._stream_loop())
        msg = f"🎲 開始連續隨機播放 {username} 的歌單（{len(pool)} 首，播完為止、不重複）。"
        vc = self._vc()
        ch = vc.active_text_channel if vc is not None else None
        if ch is not None:
            try:
                await ch.send(msg)
            except Exception:
                pass
        return (True, msg)

    def stop_personal_shuffle(self) -> bool:
        """關掉個人歌單連續播，並清掉佇列裡還沒播的個人墊位 → 下一首立刻回一般推薦／
        主題歌單（補位邏輯看 _personal_shuffle is None 即走 _auto_recommend）。

        回傳先前是否在進行中。當前正在播的那首（已 pop 出佇列）會自然播完。
        """
        was = self._personal_shuffle is not None
        self._personal_shuffle = None
        self.stream_queue[:] = [it for it in self.stream_queue if it.get("_lane") != "personal"]
        self._republish_queue_snapshot()
        return was

    def _personal_shuffle_pending(self) -> bool:
        """佇列裡是否已有一首個人歌單待播歌（保證一次只墊一首）。"""
        return any(it.get("_lane") == "personal" for it in self.stream_queue)

    async def _personal_shuffle_topup(self) -> bool:
        """個人歌單補位：佇列尾墊『一首』他的歌。

        已有待播個人歌 → 不補（回 True）。池空 → 收掉 session、回退一般推薦（回 False）。
        成功墊一首 → 回 True。
        """
        sess = self._personal_shuffle
        if not sess:
            return False
        # 無連線語音（被 dismiss/撤離）→ 結束 session，別讓 stream loop 一直 churn 解析+跳過。
        # 多條離開語音路徑不一定都有清 session，這裡當總關（2026-06-29 死鎖事故相鄰根因）。
        if not any(v.is_connected() for v in self.bot.voice_clients):
            logger.warning(f"🎲 [PersonalShuffle] 無連線語音，結束 {sess['user']} 的個人歌單 session。")
            self._personal_shuffle = None
            return False
        # 單飛守衛：stream loop 的 <2 分支會 fire-and-forget 噴多個 topup task；pending
        # 檢查與 append 之間隔著慢 resolve（log 滿滿 >5s timeout），併發的兩個 topup 會同時
        # 通過檢查各塞一首 → 兩首搶播。inflight 旗標在第一個 await 前同步設好，後到的直接退。
        if self._personal_topup_inflight:
            return True
        if self._personal_shuffle_pending():
            return True
        self._personal_topup_inflight = True
        user = sess["user"]
        try:
            while sess["remaining"]:
                song = sess["remaining"].pop(0)
                query = (song.get("webpage_url") or song.get("url")
                         or f"{song.get('uploader', '')} {song.get('title', '')}".strip())
                if not query:
                    continue
                try:
                    info = await self._resolve_yt_query(query)
                except Exception as e:
                    logger.debug(f"⚠️ [PersonalShuffle] resolve 失敗 '{query}': {e}")
                    continue
                if not info:
                    continue
                if self._check_song_duplicate(url=info.get('url', ''), title=info.get('title', ''),
                                              username=user, webpage_url=info.get('webpage_url', ''), check_history=False):
                    continue
                info['requested_by'] = user
                info['_lane'] = 'personal'
                self.stream_queue.append(info)
                self._republish_queue_snapshot()
                # WARNING 級：music_cog 的 INFO 目前被壓掉，個人歌單要看得到才好診斷搶播
                logger.warning(f"🎲 [PersonalShuffle] 墊一首（{user}）: {info['title']}（剩 {len(sess['remaining'])} 首）")
                return True
            # 池空 → 收尾
            self._personal_shuffle = None
            vc = self._vc()
            ch = vc.active_text_channel if vc is not None else None
            if ch is not None:
                try:
                    await ch.send(f"🎲 {user} 的歌單播完了，回到一般推薦。")
                except Exception:
                    pass
            logger.warning(f"🎲 [PersonalShuffle] {user} 歌單播畢，session 結束。")
            return False
        finally:
            self._personal_topup_inflight = False

    @staticmethod
    def _eligible_replay_pool(history: list, skip_vids: set) -> list:
        """最終安全網選池：從播放歷史挑可重播的舊歌，排除最近 min(5, 歷史-1) 首(防立即重複)+skip 過的。

        歷史 <2 首 → 回 []（真沒得循環，讓串流正常停）。舊門檻是「歷史 <6 首」，但那只是
        「排除最近 5 首」順手訂出來的數字，不是真的沒歌可放——短場次(剛重啟/才聽幾首)一律
        打不開安全網，還會在 `_last_resort_replay` 靜默失敗（2026-08-10 事故）。
        """
        hist = [s for s in history if isinstance(s, dict) and s.get('webpage_url')]
        if len(hist) < 2:
            return []
        recent_n = min(5, len(hist) - 1)
        recent_vids = {extract_video_id(s.get('webpage_url', '')) for s in hist[-recent_n:]}
        out = []
        for s in hist[:-recent_n]:
            v = extract_video_id(s.get('webpage_url', ''))
            if v and v not in recent_vids and v not in skip_vids:
                out.append(s)
        return out

    async def _last_resort_replay(self) -> bool:
        """三層 autopilot 全枯竭（歌庫 24h 內被播光→候選全被『已播過』濾掉）時的最終安全網：
        從本場歷史挑一首舊歌重播，保證只要有足夠歷史就永不靜默停播（無限續歌本意；
        2026-06-24/07-08 停播事故）。force_fresh 重抓避開過期 URL。回 True=補到歌。"""
        mm = getattr(self.bot, 'music_memory', None)
        skip_vids = mm.get_skipped_video_ids() if mm is not None else set()
        pool = self._eligible_replay_pool(list(self.stream_history), skip_vids)
        if not pool:
            logger.warning(f"⚠️ [AutoRecommend] 絕境回收失敗：本場歷史不足可回收（{len(self.stream_history)}首）")
            return False
        import random
        pick = random.choice(pool)
        info = await self._resolve_yt_query(pick['webpage_url'], force_fresh=True)
        if not info or not info.get('url'):
            logger.warning(f"⚠️ [AutoRecommend] 絕境回收失敗：重抓網址失敗「{pick.get('title')}」")
            return False
        info['requested_by'] = 'Marvin推薦（點給大家）'
        self.stream_queue.append(info)
        self._republish_queue_snapshot()
        logger.info(f"🔁 [AutoRecommend] 絕境回收：三層枯竭→重播「{info['title']}」（永不靜默停）")
        return True

    def _resolve_requester_avatar(self, vc, requester: str) -> str | None:
        """點播者頭像 URL：Marvin 推薦→bot 頭像；真人→從語音頻道成員 display_name 找；找不到→bot 兜底。"""
        try:
            bot_av = str(self.bot.user.display_avatar.url) if getattr(self.bot, 'user', None) else None
            if not requester or requester.startswith('Marvin'):
                return bot_av
            # vc 是 VoiceController cog → 語音頻道走 vc.voice_client.channel（非 vc.channel）
            ch = getattr(getattr(vc, 'voice_client', None), 'channel', None)
            if ch is not None:
                for m in ch.members:
                    if not m.bot and m.display_name == requester:
                        return str(m.display_avatar.url)
            return bot_av
        except Exception:
            return None

    async def _post_music_cards(self, active_ch, vc, info: dict) -> None:
        """貼①歌曲卡（封面全幅+點播者頭像圓徽合成圖）②控制台（刪舊貼新在底部）。背景執行。"""
        logger.info(f"🎛️ [Card] 貼卡 requester={info.get('requested_by')} cover={bool(info.get('thumbnail'))} ch={getattr(active_ch,'id',None)}")
        from cogs.voice_views import PlayControlView, build_song_embed, build_control_embed
        # ① 歌曲卡：合成封面+頭像；任一步失敗 → 退純封面（不阻斷）
        image_url = None
        file = None
        try:
            cover_url = info.get('thumbnail')
            avatar_url = self._resolve_requester_avatar(vc, info.get('requested_by', ''))
            if cover_url and avatar_url:
                import io
                import aiohttp
                from music_cover_card import compose_cover_with_avatar
                async with aiohttp.ClientSession() as s:
                    async with s.get(cover_url) as r1:
                        cov = await r1.read()
                    async with s.get(avatar_url) as r2:
                        av = await r2.read()
                pal = info.get('palette') or []
                png = await asyncio.to_thread(
                    compose_cover_with_avatar, cov, av,
                    title=info.get('title', ''),
                    primary=(pal[0] if len(pal) >= 1 else None),
                    secondary=(pal[1] if len(pal) >= 2 else None),
                )
                file = discord.File(io.BytesIO(png), filename="cover.png")
                image_url = "attachment://cover.png"
        except Exception as e:
            logger.warning(f"⚠️ [Card] 封面+頭像合成失敗，退純封面: {e}")
        try:
            _embed = build_song_embed(info, image_url=image_url)
            await active_ch.send(embed=_embed, file=file) if file else await active_ch.send(embed=_embed)
        except Exception as e:
            logger.warning(f"⚠️ [Card] 歌曲卡貼文失敗: {e}")
        # ② 控制台：刪掉上一則、貼新的在最下面
        _old = self._active_control_view
        if _old is not None and getattr(_old, 'message', None):
            try:
                await _old.message.delete()
            except Exception:
                pass
        view = PlayControlView(vc)
        self._active_control_view = view
        try:
            view.message = await active_ch.send(embed=build_control_embed(vc), view=view)
        except Exception as e:
            logger.warning(f"⚠️ [Card] 控制台貼文失敗: {e}")

    def _publish_now_playing_state(self, info: dict | None) -> None:
        """把現正播放狀態寫到跨進程橋接檔，讓 main_satellite.py 的 /now（HUD）讀得到。

        main_satellite.py 是不登入 Discord 的獨立進程，自己的 MusicCog 永遠是空的，
        得靠這個檔案橋接真實播放狀態（見 now_playing_state.py）。寫檔失敗（磁碟/序列化
        問題）不該打斷播放，靜默吞掉。

        HUD 只在家用：瀏覽器 satellite（MARVIN_SATELLITE_BROWSER，在外用手機）是「在外」
        場景，這個模式下不寫橋接檔，避免蓋掉家用 HUD 該看的 Discord 真實播放狀態
        （橋接檔只有一份、沒有來源標記，寫了就會蓋掉）。

        車載 puck 則是動態判斷（不是「MARVIN_CAR_MODE 有開就永遠不寫」這種死開關）：
        只有 puck 真的在場、心跳夠新鮮（is_car_actively_in_use）才略過；puck 沒插電/
        熄火/整個沒連上時，就算 MARVIN_CAR_MODE=1 也照樣把 Discord 的播放狀態寫回
        家用 HUD——不然車模式忘了關就會靜默斷掉家裡黑膠封面（2026-07-25 踩過）。
        """
        if os.getenv("MARVIN_SATELLITE_BROWSER", "").strip().lower() in ("1", "true", "yes", "on"):
            return
        if os.getenv("MARVIN_CAR_MODE", "").strip().lower() in ("1", "true", "yes", "on"):
            from car_presence_state import is_car_actively_in_use
            if is_car_actively_in_use(now=time.time()):
                return
        try:
            from now_playing_state import save_now_playing_state
            if info:
                queue = [{"title": s.get("title", ""), "by": s.get("requested_by", ""),
                          "thumbnail": s.get("thumbnail", "") or ""}
                         for s in self.stream_queue[:10]]
                save_now_playing_state(
                    playing=True,
                    title=info.get("title", ""),
                    by=info.get("requested_by", ""),
                    cover=info.get("thumbnail", ""),
                    palette=info.get("palette", []),
                    queue=queue,
                    duration=info.get("duration"),
                    song_start_time=self._current_stream_start_time,
                    comment=self._current_stream_comment,
                )
            else:
                save_now_playing_state(playing=False)
        except Exception:
            pass

    def _republish_queue_snapshot(self) -> None:
        """佇列變動（補歌/新點歌/移除個人歌單）後重寫橋接檔，不用等下一首開播 HUD 才刷新。"""
        self._publish_now_playing_state(self._current_stream_info)

    # ── 🎵 Stream loop & playback ────────────────────────────────────────────

    async def _stream_loop(self):
        """🎵 依序播放佇列中的歌曲。"""
        logger.info("🎵 [Stream Loop] 串流迴圈啟動。")
        try:
            while self.stream_mode:
                if not self.stream_queue:
                    # 🎲 個人歌單連續播：佇列空先墊他下一首（一次一首）；池空才回退一般推薦
                    if self._personal_shuffle is not None:
                        await self._personal_shuffle_topup()
                        if self.stream_queue:
                            continue                      # 墊到歌了 → 去播
                        if self._personal_shuffle is not None:
                            # ⚠️ 死鎖防護：topup 沒實際入隊（in-flight 的 create_task 還在慢
                            # resolve）→ 必須 await sleep 讓出 loop，否則 `while 佇列空: await
                            # topup()→inflight 立刻 return True` 會 busy-spin 凍結 event loop、
                            # in-flight topup 也永遠跑不完（2026-06-29 心跳阻塞 9 分鐘事故）。
                            await asyncio.sleep(0.5)
                            continue
                        # else：池空、session 已清 → 落下面一般推薦
                    vc = self._vc()
                    _rb = (self._current_stream_info or {}).get('requested_by')
                    online = self._autopilot_online_members(vc.get_online_members() if vc is not None else [])
                    _seed = self._autorecommend_seed(_rb, online)
                    if _seed:
                        await self._auto_recommend(_seed)
                    # 三層 autopilot 補不到 → 最終安全網：從歷史回收重播，永不靜默停
                    if not self.stream_queue and await self._last_resort_replay():
                        continue
                    if not self.stream_queue:
                        break
                    continue

                vc = self._vc()
                info = self.stream_queue.pop(0)
                self._current_stream_info = info
                self._current_stream_start_time = None
                self._publish_now_playing_state(info)
                self._current_lyrics = None
                self._current_stream_comment = None
                self.stream_paused = False
                title = info['title']
                requested_by = info.get('requested_by', '未知')
                logger.info(f"🎵 [Stream Loop] 播放: {title} (點播：{requested_by})")
                self.stream_history.append(info)

                if hasattr(self.bot, 'music_memory'):
                    self.bot.music_memory.record_play(info, requested_by)

                try:
                    from bridge_emitters import emit_music_started_to_bridge
                    asyncio.create_task(emit_music_started_to_bridge(
                        self.bot,
                        {"title": title, "style": info.get("style") or info.get("uploader", ""),
                         "target": requested_by, "started_ts": time.time(),
                         "source": info.get("source", "stream")},
                        requested_by,
                    ))
                except Exception as e:
                    logger.debug(f"⚠️ [Companion_Bridge] music_started hook skipped: {e}")

                url = info.get('url', '')
                prefetch_task = self._prefetch_cache.pop(url, None)
                # 🎵 [Play-First] 只用「已就緒」的 meta；沒好就不等（使用者定：先播音樂，
                # meta 阻塞就放棄 DJ TTS）。未就緒 → 本首放棄 DJ、先出聲、歌詞/評論背景補。
                meta = self._ready_meta(prefetch_task)
                if meta is not None:
                    logger.info(f"🔮 [Prefetch] 命中預取快取: {title}")
                    self._current_stream_comment = meta.get('comment')
                    self._current_lyrics = meta.get('lyrics')
                    dj_data = meta.get('dj')
                    self._republish_queue_snapshot()
                else:
                    self._current_stream_comment = None
                    self._current_lyrics = None
                    dj_data = None   # meta 未就緒 → 放棄 DJ，不阻塞出聲
                    _bg = prefetch_task if prefetch_task is not None else asyncio.create_task(self._fetch_song_meta(info))

                    def _apply_bg_meta(t, _self=self):
                        m = t.result() if not t.cancelled() and t.exception() is None else None
                        if isinstance(m, dict):
                            _self._current_stream_comment = m.get('comment')
                            _self._current_lyrics = m.get('lyrics')
                            _self._republish_queue_snapshot()   # HUD DJ 銳評卡靠這次補推更新

                    _bg.add_done_callback(_apply_bg_meta)
                    logger.info(f"🎵 [Play-First] meta 未就緒，先播音樂、放棄本首 DJ、meta 背景補：{title}")

                # 🎛️ 每首歌：貼歌曲卡（封面+頭像合成）+ 控制台刪舊貼新在底部。
                # 背景 task：封面合成要下載圖片，不擋 play_stream_song 出聲；info 傳快照防下一首覆蓋。
                # active_text_channel 只在 /summon 斜線指令設定；語音召喚/重連時為 None →
                # 退回貼到語音頻道自己的內建文字區（VoiceChannel.send()），卡片才不會第一首缺席。
                _vch = getattr(getattr(vc, 'voice_client', None), 'channel', None) if vc is not None else None
                active_ch = (vc.active_text_channel or _vch) if vc is not None else None
                if active_ch and vc is not None:
                    asyncio.create_task(self._post_music_cards(active_ch, vc, dict(info)))
                else:
                    logger.info(f"🎛️ [Card] 跳過貼卡：active_ch=None vc={vc is not None}")

                if self.stream_queue:
                    next_info = self.stream_queue[0]
                    next_url = next_info.get('url', '')
                    if next_url not in self._prefetch_cache and vc is not None:
                        self._prefetch_cache[next_url] = asyncio.create_task(self._fetch_song_meta(next_info))
                        logger.info(f"🔮 [Prefetch] 開始預取下一首: {next_info['title']}")

                if len(self.stream_queue) < 2:
                    if self._personal_shuffle is not None:
                        # 🎲 個人歌單模式：補位走他的歌單。已有 in-flight topup 或已墊一首就
                        # 不再 spawn（skip 連按時 loop 快速空轉，否則噴一堆 task 互搶）。
                        if not self._personal_topup_inflight and not self._personal_shuffle_pending():
                            asyncio.create_task(self._personal_shuffle_topup())
                    else:
                        online = self._autopilot_online_members(vc.get_online_members() if vc is not None else [])
                        seed = self._autorecommend_seed(requested_by, online)
                        if seed:
                            asyncio.create_task(self._auto_recommend(seed))

                dj_audio = dj_data.get('audio_path') if isinstance(dj_data, dict) else None
                # [DJ Tail] 尾段派發成功（上一首 _run_tail_dj 播完並標記）→ 本首開頭不重播
                if info.get('_dj_played_in_tail'):
                    logger.info(f"[DJ Tail] {title} DJ 已在上一首尾段播出，跳過開頭重播")
                    dj_audio = None
                    dj_data = None
                if dj_data and not dj_audio and vc is not None:
                    await self._maybe_play_dj_interjection(dj_data)

                self._current_song_skipped = False
                song_start_time = time.time()
                self._current_stream_start_time = song_start_time
                self._republish_queue_snapshot()   # HUD 進度條要靠這次補推的 song_start_time
                song_lyrics_snapshot = self._current_lyrics or ""
                playback_completion = "natural"

                # [DJ Tail] 在播 N 期間排尾段 task：只要 duration 已知就排，下一首在點火
                # 當下才抓 stream_queue[0]（autopilot 常播放中才排下一首，開播時綁定會抓空）。
                if vc is not None and info.get('duration'):
                    self._tail_dj_task = asyncio.create_task(
                        self._run_tail_dj(info, song_start_time)
                    )
                    def _clear_tail_task(t, _self=self):
                        if _self._tail_dj_task is t:
                            _self._tail_dj_task = None
                    self._tail_dj_task.add_done_callback(_clear_tail_task)
                    logger.info(f"[DJ Tail] 已排尾段 task：{title}（點火時抓下一首）")

                try:
                    await self.play_stream_song(
                        info['url'], title, dj_audio_path=dj_audio,
                        highlight_start_s=info.get('highlight_start_s'),
                    )
                except Exception:
                    playback_completion = "stopped"
                    raise
                finally:
                    # [DJ Tail] 歌播完（自然/中斷）後取消尾段 task（若仍未觸發）
                    if self._tail_dj_task is not None and not self._tail_dj_task.done():
                        self._tail_dj_task.cancel()
                        self._tail_dj_task = None
                    try:
                        from bridge_emitters import emit_music_ended_to_bridge
                        completion = playback_completion if self.stream_mode else "stopped"
                        asyncio.create_task(emit_music_ended_to_bridge(
                            self.bot, {"title": title}, completion
                        ))
                    except Exception as e:
                        logger.debug(f"⚠️ [Companion_Bridge] music_ended hook skipped: {e}")

                # 🔁 點的歌只播了一瞬（疑 yt-dlp 網址過期→ffmpeg 403）→ 重抓網址重試一次，
                # 別讓它被自動推薦洗掉。DJ 報歌走 mixed（隨 ffmpeg 一起失敗）→ 首次 403 不誤報，
                # 只在確定能播的那次才響＝「確定能播的歌才說出來」。
                _played_s = time.time() - song_start_time
                # 精華起播（highlight_start_s）讓實播天生比 metadata 全長短一截，中途切/短
                # 播判斷都要扣掉這段位移，否則正常播完的精華曲會被誤判成「中途切」。
                _effective_duration = info.get('duration')
                if _effective_duration and info.get('highlight_start_s'):
                    _effective_duration = max(0.0, _effective_duration - info['highlight_start_s'])
                # 🔎 中途切偵測（診斷用）：播到一半串流 URL 失效→提早結束，ffmpeg 靜默不留 log。
                # 只印不重試（中途切要 seek 續播是另一步，先確認頻率再決定）。
                if not getattr(self, "_current_song_skipped", False) and self._premature_cut(_played_s, _effective_duration):
                    logger.warning(
                        f"⚠️ [Stream] 「{title}」疑中途切：實播 {_played_s:.0f}s / 全長 "
                        f"{_effective_duration}s（串流 URL 中途失效？非開頭 403、非你 skip）"
                    )
                if self._should_retry_failed_song(
                        _played_s, stream_active=self.stream_mode,
                        skipped=getattr(self, "_current_song_skipped", False),
                        requested_by=requested_by, already_retried=False):
                    _wp = info.get('webpage_url') or info.get('url')
                    logger.info(f"🔁 [Stream] 點的歌只播 {_played_s:.1f}s，疑似 403，重抓網址重試：{title}")
                    # force_fresh：跳過快取，否則命中的是剛 403 的同一份死 URL → 又 403（無意義重試）
                    _fresh = await self._resolve_yt_query(_wp, force_fresh=True) if _wp else None
                    if _fresh and _fresh.get('url'):
                        try:
                            await self.play_stream_song(
                                _fresh['url'], title, dj_audio_path=dj_audio,
                                highlight_start_s=_fresh.get('highlight_start_s'),
                            )
                        except Exception:
                            logger.warning(f"⚠️ [Stream] 重試也失敗，讓下一首接手：{title}")
                    else:
                        logger.warning(f"⚠️ [Stream] 重抓網址失敗（無 webpage_url 或解析空），讓下一首接手：{title}")

                if vc is not None:
                    asyncio.create_task(self._analyze_song_reactions(info, song_start_time, song_lyrics_snapshot))

                if self.stream_mode:
                    await asyncio.sleep(1.0)

            self.stream_mode = False
            self._current_stream_info = None
            self._publish_now_playing_state(None)
            vc = self._vc()
            if vc is not None:
                vc.last_marvin_speech_time = time.time()
            logger.info("🎵 [Stream Loop] 佇列播放完畢。")
            active_ch = vc.active_text_channel if vc is not None else None
            if vc is not None and hasattr(vc, 'stt_logger'):
                vc.stt_logger.info("[串流結束] 音樂佇列播放完畢")
            if active_ch:
                await active_ch.send("🎵 **【串流播放完畢】** 佇列已空。就跟馬文的希望一樣——消失殆盡。")

        except asyncio.CancelledError:
            # 旗標必須反映現實：沒清的話 stream_mode 會停在 True 但沒人在播，
            # 之後每次點歌的「叫醒」判斷都會被騙 → 佇列永遠卡死（2026-07-17 事故）。
            self.stream_mode = False
            self._publish_now_playing_state(None)
            # exc_info：留下取消當下卡在哪個 await（play_stream_song/mixer 的哪一行）。
            # 2026-08-01 事故：迴圈被取消但抓不到兇手，只能靠時間軸推理；已知呼叫點
            # 都已改走 _cancel_stream_task 留痕，這裡補上「被取消時人在哪」那一半。
            logger.warning("🎵 [Stream Loop] 串流迴圈被取消（stream_mode 已歸位 False）。", exc_info=True)
        except Exception as e:
            logger.error(f"❌ [Stream Loop] 發生異常: {e}")
            self.stream_mode = False
            self._publish_now_playing_state(None)

    async def _await_reconnect_device(self, vc, *, timeout_s: float = 12.0, interval_s: float = 0.5):
        """語音 WS 短暫斷線（如 close code 1006）→ discord.py 會自動重連，中間 ~數秒
        _resolve_playback_device() 回 None。輪詢等 device 回來，避免一次短暫重連視窗害整條
        音樂佇列被 stream_mode=False 永久收攤（2026-07-10 實測：00:49 一次 1006→下一首撞
        「無可用播放裝置」→佇列直接「播放完畢」再也沒歌）。逾時仍 None＝真的斷了、caller 收攤；
        期間被停播（stream_mode False）也提早退出。"""
        if vc is None:
            return None
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if not self.stream_mode:
                return None
            await asyncio.sleep(interval_s)
            device = vc._resolve_playback_device()
            if device is not None:
                logger.info("🎵 [Stream Song] 語音短暫斷線已重連，續播佇列。")
                return device
        return None

    async def play_stream_song(self, url: str, title: str, dj_audio_path: str | None = None,
                                highlight_start_s: float | None = None):
        """🎵 播放單首串流音樂，等待播放完成後 return。

        highlight_start_s：YouTube「最多人重播」熱力圖挑出的精華起點（見
        youtube_heatmap.pick_highlight_start），有給就從這秒開始播（-ss），
        不影響 DJ 混音模式（use_mix，另一條較少走的路徑，保持舊行為）。
        """
        import shlex

        vc = self._vc()
        # 走輸出接縫：本機模式回 LocalSpeakerDevice、Discord 回 DiscordPlaybackDevice(vc)、
        # 皆無回 None（不再寫死 Discord voice_client，否則本機模式音樂直接 bail 無聲）。
        device = vc._resolve_playback_device() if vc is not None else None
        # device None 但仍在串流 session → 多半是語音 WS 短暫斷線重連中（1006），別立刻整條
        # 收攤，先有界等重連（見 _await_reconnect_device）。逾時才真的放棄。
        if device is None and vc is not None and self.stream_mode:
            device = await self._await_reconnect_device(vc)
        if device is None:
            logger.warning("⚠️ [Stream Song] 無可用播放裝置（Discord VC / 本機喇叭皆無，等重連逾時），跳過。")
            self.stream_mode = False
            return

        self._current_stream_url = url
        use_mix = dj_audio_path and os.path.exists(dj_audio_path)

        if use_mix:
            vol = self.stream_volume
            djv = self._DJ_INTERJECTION_VOLUME
            fc = (
                f"[0:a]asplit=2[dj_sc][dj_mix];"
                f"[dj_sc]apad=whole_dur=9999[dj_pad];"
                f"[dj_mix]volume={djv:.3f}[dj_q];"  # DJ 播報降到 30%，不蓋過音樂
                f"[1:a]loudnorm=I=-14:TP=-1.5:LRA=11,volume={vol:.3f}[music];"
                f"[music][dj_pad]sidechaincompress=threshold=0.02:ratio=8:attack=5:release=600[ducked];"
                f"[ducked][dj_q]amix=inputs=2:duration=longest:normalize=0[out]"
            )
            before_opts = (
                f"-i {shlex.quote(dj_audio_path)} "
                "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -probesize 32M"
            )
            options = f"-vn -bufsize 512k -filter_complex \"{fc}\" -map [out]"
            logger.info(f"🎙️ [DJ Mix] 混音模式：{os.path.basename(dj_audio_path)}")
            if vc is not None:
                vc._mixer.set_volume(1.0)
                await vc._mixer_play_music(
                    device, discord.FFmpegPCMAudio(url, before_options=before_opts, options=options),
                    still_active=lambda: self.stream_mode,
                )
        else:
            p12_opts = {
                'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -probesize 32M',
                'options': '-vn -bufsize 512k',
            }
            if highlight_start_s:
                p12_opts['before_options'] = f'-ss {highlight_start_s:.2f} ' + p12_opts['before_options']
            if url not in self._stream_norm_gain and vc is not None:
                asyncio.create_task(self._measure_norm_gain_bg(url))
            if vc is not None:
                # DJ Tail 點火時已背景 preload（見 _start_music_preload）→ 有就直接用、
                # 零等待；沒有（沒走過尾段轉場，如第一首/被 skip）就退回現場建 ffmpeg 音源。
                preloaded, fresh = await self._resolve_music_source(
                    url, lambda: discord.FFmpegPCMAudio(url, **p12_opts))
                await vc._mixer_play_music(
                    device, fresh,
                    still_active=lambda: self.stream_mode, volume_attr="stream_volume",
                    preloaded=preloaded,
                )

    @app_commands.command(name="marvin_recommend", description="[Stream] 讓馬文根據你的點播記憶推薦下一首")
    async def marvin_recommend(self, interaction: discord.Interaction):
        await interaction.response.defer()
        vc = self._vc()
        if not vc:
            await interaction.followup.send("❌ 語音系統尚未就緒。", ephemeral=True)
            return
        username = interaction.user.display_name
        if not hasattr(self.bot, 'music_memory'):
            await interaction.followup.send("音樂記憶系統尚未啟動。", ephemeral=True)
            return
        music_ctx = self.bot.music_memory.get_user_music_context(username)
        if not music_ctx:
            await interaction.followup.send(
                f"我對 `{username}` 的品味一無所知。先去多點幾首歌讓我學習再說。", ephemeral=True
            )
            return
        await interaction.followup.send(f"🔮 **【馬文精選】** 正在為 `{username}` 挑選...")
        await vc._auto_recommend(username)

    # ── 🎵 Song metadata / fetch helpers ────────────────────────────────────────

    def _parse_song_title_artist(self, info: dict) -> tuple[str, str]:
        """從 info 解析出乾淨的 title 和 artist，處理 'Artist - Title' 格式。"""
        raw_title = info.get('title', '')
        artist = info.get('artist') or info.get('uploader', '')
        if ' - ' in raw_title and not info.get('track'):
            parts = raw_title.split(' - ', 1)
            return parts[1].strip(), parts[0].strip()
        return info.get('track') or raw_title, artist

    def _dj_clean_name(self, info: dict) -> tuple[str, str]:
        """DJ 播報專用乾淨歌名（track→catalog videoId→regex 剝雜訊）。與歌詞路徑的
        _parse_song_title_artist 分開：catalog 的「藝人 歌名」合併格式不適合 lrclib 查詞。"""
        from song_name_clean import dj_display_name
        from music_memory import extract_video_id
        return dj_display_name(info, extract_vid=extract_video_id)

    async def _fetch_lyrics_synced(self, info: dict) -> str | None:
        """像 _fetch_lyrics_raw 但保留 [mm:ss.xx] timestamp（給 lyrics_seek 用）。"""
        import aiohttp
        title, artist = self._parse_song_title_artist(info)
        try:
            import syncedlyrics
            lrc = await asyncio.to_thread(
                syncedlyrics.search,
                f"{title} {artist}".strip(),
                providers=["NetEase", "Lrclib", "Musixmatch", "Genius"],
            )
            if lrc and "[" in lrc:
                return lrc
        except Exception as e:
            logger.debug(f"⚠️ [LyricsSynced/syncedlyrics] {e}")
        try:
            async with aiohttp.ClientSession() as session:
                params = {'track_name': title, 'artist_name': artist}
                async with session.get('https://lrclib.net/api/get', params=params,
                                       timeout=aiohttp.ClientTimeout(total=8)) as r:
                    if r.status == 200:
                        data = await r.json()
                        synced = data.get('syncedLyrics')
                        if synced:
                            return synced
        except Exception as e:
            logger.debug(f"⚠️ [LyricsSynced/lrclib] {e}")
        return None

    async def _fetch_lyrics_raw(self, info: dict) -> str | None:
        """Pure lyrics fetch：syncedlyrics (NetEase 優先) → lrclib.net fallback。"""
        import re, aiohttp
        title, artist = self._parse_song_title_artist(info)
        duration = int(info.get('duration') or 0)

        def _strip_lrc(lrc: str) -> str:
            return re.sub(r'\[\d+:\d+\.\d+\]\s?', '', lrc).strip()

        try:
            import syncedlyrics
            lrc = await asyncio.to_thread(
                syncedlyrics.search,
                f"{title} {artist}".strip(),
                providers=["NetEase", "Lrclib", "Musixmatch", "Genius"],
            )
            if lrc:
                return _strip_lrc(lrc)
        except Exception as e:
            logger.debug(f"⚠️ [Lyrics/syncedlyrics] {e}")

        try:
            async with aiohttp.ClientSession() as session:
                params = {'track_name': title, 'artist_name': artist, 'duration': duration}
                async with session.get('https://lrclib.net/api/get', params=params,
                                       timeout=aiohttp.ClientTimeout(total=8)) as r:
                    if r.status == 200:
                        data = await r.json()
                        plain = data.get('plainLyrics') or ''
                        if plain:
                            return plain
        except Exception as e:
            logger.debug(f"⚠️ [Lyrics/lrclib] {e}")
        return None

    async def _fetch_comment_raw(self, info: dict) -> str | None:
        """Pure Marvin commentary fetch via LLM，注入使用者音樂記憶。"""
        parts = [f"歌名：{info['title']}，頻道：{info.get('uploader', '')}"]
        requested_by = info.get('requested_by', '')
        if requested_by and not requested_by.startswith('Marvin'):
            parts.append(f"點播者：{requested_by}")
            if hasattr(self.bot, 'music_memory'):
                music_ctx = self.bot.music_memory.get_user_music_context(requested_by)
                if music_ctx:
                    parts.append(music_ctx)
        try:
            return await self.bot.router.generate_dynamic_system_msg(
                "stream_now_playing", context="\n".join(parts)
            )
        except Exception:
            return None

    @staticmethod
    def _autopilot_dj_phrase(spotlight: str, clean_title: str, clean_artist: str,
                              lane: str = "", anchor: str = "") -> str:
        """為 autopilot 推薦歌曲生成 DJ 台詞，理由依 lane 而定（DJ 編個理由）。"""
        import random
        who = spotlight or "你"
        if lane == "group_resonance":
            pool = (MusicCog._AUTOPILOT_DJ_PHRASES_GROUP if clean_artist
                    else MusicCog._AUTOPILOT_DJ_PHRASES_GROUP_NO_ARTIST)
        elif lane == "long_tail":
            pool = MusicCog._AUTOPILOT_DJ_PHRASES_LONG_TAIL
        elif lane == "discovery":
            pool = MusicCog._AUTOPILOT_DJ_PHRASES_DISCOVERY
        elif anchor and anchor != clean_title:
            pool = MusicCog._AUTOPILOT_DJ_PHRASES_SPOTLIGHT_ANCHOR
        else:
            pool = (MusicCog._AUTOPILOT_DJ_PHRASES_PERSONAL if clean_artist
                    else MusicCog._AUTOPILOT_DJ_PHRASES_PERSONAL_NO_ARTIST)
        tmpl = random.choice(pool)
        return tmpl.format(who=who, title=clean_title, artist=clean_artist, anchor=anchor)

    @staticmethod
    def _autopilot_pick_reason(info: dict) -> str:
        """autopilot 選這首的理由（給 DJ LLM 當素材，語意同 _autopilot_dj_phrase 的 lane 分流）。"""
        who = info.get('_spotlight', '') or '大家'
        lane = info.get('_lane', '')
        if lane == 'group_resonance':
            return "這首是大家都有共鳴的歌"
        if lane == 'long_tail':
            return f"{who} 很久沒點到這首了"
        if lane == 'discovery':
            return f"照 {who} 的口味挖出來的新歌"
        anchor = info.get('_anchor_title', '')
        if anchor:
            return f"因為 {who} 點過《{anchor}》才接這首"
        return f"這首是 {who} 平常會聽的歌"

    @staticmethod
    def _current_season() -> str:
        """由當前月份推台北季節（北半球）。給 DJ 串場的環境沉浸用。"""
        mon = time.localtime().tm_mon
        if mon in (3, 4, 5):
            return "春天"
        if mon in (6, 7, 8):
            return "夏天"
        if mon in (9, 10, 11):
            return "秋天"
        return "冬天"

    @staticmethod
    def _city_label() -> str:
        """車載 ESP32 puck 的 GPS 訊號 → DJ 環境行用的城市/區名。

        沒有新鮮訊號（不在車上）時退回「台中」（家裡預設）。讀檔/座標推算失敗
        不該讓 DJ 串場掛掉，走跟 _life_cores_async 一樣的降級哲學。
        """
        try:
            from gps_context import city_label
            from location_state import load_location_state
            return city_label(load_location_state(), now=time.time())
        except Exception:
            return "台中"

    @staticmethod
    def _themed_dj_text(info: dict) -> str:
        """🎚️ 主題歌單的歌 → 用 LLM 策展時寫的選歌理由當 DJ 播報詞（其餘歌回 ""）。"""
        if info.get('_lane') == 'themed':
            return (info.get('_pick_reason') or '').strip()
        return ''

    _QUICK_SEGUE_TEMPLATES = tuple(_DJ_TEMPLATES["quick_segue"]["default"])
    _QUICK_SEGUE_TEMPLATES_INTIMATE = tuple(_DJ_TEMPLATES["quick_segue"]["intimate"])
    _QUICK_SEGUE_TEMPLATES_ENERGETIC = tuple(_DJ_TEMPLATES["quick_segue"]["energetic"])

    @classmethod
    def _quick_segue_text(cls, n_online: int = 0) -> str:
        """沒有任何話題/素材可用時的本地過場模板——純接歌，跳過 LLM。

        n_online 決定語氣（跟原本 group-size ctx 提示同一套門檻），quick 模式
        沒有 LLM 可以照 ctx 調語氣，改本地挑模板池達到同樣效果。
        """
        import random
        if n_online == 1:
            pool = cls._QUICK_SEGUE_TEMPLATES_INTIMATE
        elif n_online >= 4:
            pool = cls._QUICK_SEGUE_TEMPLATES_ENERGETIC
        else:
            pool = cls._QUICK_SEGUE_TEMPLATES
        return random.choice(pool)

    def _life_cores(self, entries, now: float,
                    present_speakers: set[str] | None = None) -> list:
        """日記 entries → DJ 雞湯用的近日生活素材（純函式包裝，測試用此點注入）。

        回傳 LifeCore 列表（含事件主角），供 dj_topic_selector.select_mode 判斷
        主角現在在不在場。present_speakers: 在場人集合，傳給
        recent_life_cores_with_speakers 做 privacy filter。None = 不過濾
        （fail-open，vc 不可用時的預設）。
        """
        from dj_life_context import recent_life_cores_with_speakers
        return recent_life_cores_with_speakers(entries, now=now, present_speakers=present_speakers)

    async def _life_cores_async(self) -> list[str]:
        """讀日記檔取生活素材。606K 檔的 read+parse 走 to_thread，不阻塞 event loop。
        任何失敗回 []（DJ 少一味料，不該讓整條串場掛掉）。

        在場人（vc.get_online_members）傳給 privacy filter，
        讓敏感 entry 在參與者不全在場時自動過濾。
        vc 不可用 → present_speakers=None（不過濾，fail-open）。
        """
        present_speakers: set[str] | None = None
        try:
            _vc = self._vc()
            if _vc is not None:
                present_speakers = set(_vc.get_online_members())
        except Exception as e:
            logger.debug(f"[DJ Life] 讀在場人失敗，privacy filter 跳過: {e}")
        try:
            entries = await asyncio.to_thread(self._load_summary_entries)
            return self._life_cores(entries, time.time(),
                                    present_speakers=present_speakers)
        except Exception as e:
            logger.debug(f"⚠️ [DJ Life] 生活素材抽取失敗，DJ 改走無生活素材: {e}")
            return []

    def _dj_topic_store(self):
        """DJ 話題冷卻表的 lazy 單例（跨呼叫共用同一份記憶體狀態＋disk-backed）。"""
        store = getattr(self, "_dj_topic_cooldown_store", None)
        if store is None:
            from dj_topic_selector import TopicCooldownStore
            store = TopicCooldownStore()
            self._dj_topic_cooldown_store = store
        return store

    def _present_interests(self) -> list[str]:
        """在場成員在 suki_memory 的興趣，供話題選擇器沒有『最近生活』可用時當引子。
        任何失敗回 []（DJ 少一味料，不該讓整條串場掛掉）。"""
        try:
            suki = getattr(getattr(self.bot, 'router', None), 'memory', None)
            vc = self._vc()
            if suki is None or vc is None:
                return []
            out = []
            for m in vc.get_online_members():
                # 按最近才被強化排序，別老是講分數最高的舊愛好（跳針）。
                for like in suki.get_recent_liked_items(m, limit=2):
                    like = str(like).strip()
                    if like:
                        out.append(f"{m}喜歡{like}")
            return out
        except Exception:
            return []

    async def _fetch_dj_interjection_raw(self, info: dict) -> dict | None:
        """預先生成 DJ 播報：LLM 文字 + TTS 預渲染音訊。回傳 {'text', 'audio_path'} 或 None。"""
        requester = info.get('requested_by', '')
        if not requester:
            return None

        if requester.startswith('Marvin'):
            _pos = info.get('_round_position', 0)
            if _pos > 0:
                await asyncio.sleep(_pos * 3.0)

        mm = getattr(self.bot, 'music_memory', None)
        play_count, feelings, lyric_match = 0, [], ''
        if mm:
            key = mm._key(info)
            song_data = mm._data.get('songs', {}).get(key, {})
            play_count = song_data.get('requesters', {}).get(requester, 0)
            r = song_data.get('reactions', {}).get(requester, {})
            feelings = r.get('feelings', [])
            lyric_match = r.get('lyric_match', '')

        conv_lines = []
        conv_buf = getattr(getattr(self.bot, 'engine', None), 'conv_buffer', None)
        if conv_buf:
            for entry in conv_buf.get_last_n_utterances(4):
                if entry.get('speaker') != 'Marvin':
                    conv_lines.append(f"{entry['speaker']}：「{entry['text'][:25]}」")

        slot = mm.time_slot(time.time()) if mm else ''
        title = info.get('title', '')
        # 餵 LLM 用乾淨歌名（別給完整 YouTube 標題，否則 DJ 會照唸一長串）。
        _clean_t, _clean_a = self._dj_clean_name(info)
        _song_label = f"{_clean_a} - {_clean_t}" if _clean_a else _clean_t
        ctx = [f"歌曲：{_song_label or title}", f"點播者：{requester}"]
        # 上一首 ↔ 下一首故事延伸：反向找第一首不是自己的 history 歌（相容 Play-First
        # 背景路徑 stream_history[-1] 就是自己的情況）。第一首歌沒有上一首，跳過。
        prev_title = ''
        for s in reversed((getattr(self, 'stream_history', None) or [])[-3:]):
            if isinstance(s, dict):
                t = s.get('title', '')
                if t and t != title:
                    prev_title = t
                    break
        if prev_title:
            ctx.append(f"上一首剛播完：《{prev_title}》")
        if play_count >= 2:
            ctx.append(f"{requester} 第 {play_count} 次點這首")
        if feelings:
            ctx.append(f"情感記錄：{' / '.join(feelings[:2])}")
        if lyric_match:
            ctx.append(f"歌詞呼應：{lyric_match[:60]}")
        # 環境沉浸：城市/區（GPS 訊號，沒有則退回台北）+ 季節（日期推）+ 時段。
        # 不再無條件塞進 ctx——只有 mode == "atmosphere" 被選中時才當開場素材用，
        # 其餘時候別讓它變成 LLM 隨手可用的預設開場（治「每次都靠環境/天氣開場」）。
        season = self._current_season()
        city = self._city_label()
        env = f"環境：{city} · {season}"
        if slot:
            env += f" · {slot}"
        if conv_lines:
            ctx.append("頻道近期對話：\n" + '\n'.join(conv_lines))
        # 本地決定這次串場怎麼寫，LLM 不必自己判斷「有沒有話題、要不要硬掰、
        # 這件事是不是點播者本人的」——樣版/素材/在場判斷全部本地做完，LLM 只負責
        # 把選定的素材寫成自然的過場文字。
        # 順序：近期生活（主角要在場，否則換下一個候選）→ 在場興趣 → 都沒有時在
        # 對話銜接/上一首銜接/純接歌 之間本地輪替（治「每次都靠環境/天氣開場」）。
        from dj_topic_selector import select_mode
        life = await self._life_cores_async()
        interests = self._present_interests()

        _vc_ref = None
        present_members: set[str] | None = None
        try:
            _vc_ref = self._vc()
            if _vc_ref is not None:
                present_members = set(_vc_ref.get_online_members())
        except Exception:
            pass  # fail-open：vc 不可用時不過濾在場人

        # autopilot 策展理由算在 mode 選擇之前：有理由可講時別讓它被 fallback 輪替
        # 排進 quick（quick 沒素材時直接跳過 LLM，會把這個好料浪費掉）。
        _autopilot_reason = ''
        if requester.startswith('Marvin'):
            _autopilot_reason = self._autopilot_pick_reason(info) or ''

        topic, mode = select_mode(
            life, interests, self._dj_topic_store(),
            present_members=present_members,
            has_conversation=bool(conv_lines),
            has_prev_song=bool(prev_title),
        )
        if _autopilot_reason and mode in ("quick", "atmosphere"):
            mode = "reason"

        # 開場鉤子提示依「歌會中的心理機制」分兩類套用：
        #   代入感（life/interest）——這是聽眾自己的事，別只是轉述，要讓人覺得被說中。
        #   氣氛精準（atmosphere）——緊扣這個時間/地點，像特別為這一刻準備的。
        # conversation/prev_song 本身就是銜接類，維持原本的過場方向指示即可。
        if mode == "life":
            ctx.append(f"最近生活：\n・{topic}")
            ctx.append(random.choice(self._DJ_EMPATHY_HOOK_TEMPLATES))
        elif mode == "interest":
            ctx.append(f"在場興趣：\n・{topic}")
            ctx.append(random.choice(self._DJ_EMPATHY_HOOK_TEMPLATES))
        elif mode == "prev_song":
            ctx.append("串場方向：延續上一首的情緒銜接過去就好，不用硬掰新話題。")
        elif mode == "conversation":
            ctx.append("串場方向：用剛才頻道對話的氣氛自然接過去就好，不用硬掰新話題。")
        elif mode == "atmosphere":
            ctx.append(env)
            ctx.append("開場鉤子：緊扣現在的時間/地點氛圍切入，像是特別為這一刻準備的，不用硬掰別的話題。")
        if _autopilot_reason:
            ctx.append(f"選這首的理由：{_autopilot_reason}")

        # Group size → 語氣：1 人親密、4+ 人 live DJ 節奏；2-3 人不加（維持原有行為）。
        # vc() 不可用時靜默略過。quick 模式沒有 LLM 可以照 ctx 調語氣，改本地選模板池。
        _n_online = 0
        try:
            if _vc_ref is not None:
                _n_online = len(_vc_ref.get_online_members())
                if _n_online == 1:
                    ctx.append("只有一個人在聽，語氣親密一點，像對老朋友說話。")
                elif _n_online >= 4:
                    ctx.append("人多，說短一點、節奏精簡，像 live DJ 播報。")
        except Exception:
            pass  # fail-open：語氣注入失敗不影響 DJ 生成

        # 長度 gate 統一放寬到 dj_story：Marvin autopilot 模板/themed 理由也別再被 5s
        # music_intro 砍成「狗與露」這種殘句（autopilot DJ 被截斷的根因）。
        gate_task = "dj_story"
        text = self._themed_dj_text(info)   # 主題歌單：直接播策展時寫好的理由，不重複燒 LLM
        if not text and mode == "quick":
            # 沒有任何素材可用 → 本地固定模板直接接歌，跳過 LLM（零出錯風險、零延遲、零花費）。
            text = self._quick_segue_text(_n_online)
        if not text:
            # autopilot 與真人點歌共用這條 LLM 雞湯（走 tier=simple 免費層）。
            try:
                text = await self.bot.router.generate_dynamic_system_msg(
                    'dj_interjection', context='\n'.join(ctx)
                )
            except Exception as e:
                logger.warning(f"⚠️ [DJ Prefetch] LLM 失敗: {e}")
                text = ""
            text = (text or '').strip()
            # LLM 空手（失敗/quota）→ autopilot 退回原本的模板台詞，別掉到報幕 fallback
            if not text and requester.startswith('Marvin'):
                from song_name_clean import clean_title_regex
                clean_title, clean_artist = self._dj_clean_name(info)
                text = self._autopilot_dj_phrase(
                    info.get('_spotlight', ''), clean_title, clean_artist,
                    lane=info.get('_lane', ''),
                    anchor=clean_title_regex(info.get('_anchor_title', '')),
                )
                logger.info("🎙️ [DJ Prefetch] LLM 空手 → 退回 autopilot 模板")

        text = (text or '').strip()
        if len(text) < 2:
            clean_title, clean_artist = self._dj_clean_name(info)
            if clean_artist:
                text = f"DJ Marvin為你帶來{clean_artist}演唱的{clean_title}，{requester} 點的"
            else:
                text = f"DJ Marvin為你帶來《{clean_title}》，{requester} 點的"
            logger.info("🎙️ [DJ Prefetch] 採用 fallback template")

        from tts_length_policy import truncate_for_tts
        gated_text, was_cut = truncate_for_tts(
            text, gate_task, self.bot.tts_engine.get_estimated_duration
        )
        if was_cut:
            logger.info(f"🚦 [TTS Gate] DJ intro 超上限截斷({gate_task}): '{text}' → '{gated_text}'")
            text = gated_text

        audio_path = None
        try:
            audio_path = await self.bot.tts_engine.generate_audio(text)
        except Exception as e:
            logger.warning(f"⚠️ [DJ Prefetch] TTS 預渲染失敗，改用即時串流: {e}")

        logger.info(f"🎙️ [DJ Prefetch] 完成: {text[:30]}… (audio={'✓' if audio_path else '✗'})")
        return {'text': text, 'audio_path': audio_path}

    @staticmethod
    def _ready_meta(prefetch_task) -> dict | None:
        """Play-First：只回傳『已就緒』的 prefetch meta；未就緒/失敗/非 dict → None。

        None 時 caller 先出聲、放棄本首 DJ、meta 背景補——不讓 LLM meta 生成阻塞出聲
        （Plan12 即時混音，DJ 本就疊在 ducked 音樂上，等它生成完才出聲沒意義）。
        """
        if prefetch_task is not None and prefetch_task.done():
            try:
                m = prefetch_task.result()
            except Exception:
                return None
            return m if isinstance(m, dict) else None
        return None

    async def _fetch_song_meta(self, info: dict) -> dict:
        """並行 fetch 歌詞、馬文評語、DJ 播報（含 TTS 預渲染）。"""
        lyrics, comment, dj = await asyncio.gather(
            self._fetch_lyrics_raw(info),
            self._fetch_comment_raw(info),
            self._fetch_dj_interjection_raw(info),
            return_exceptions=True,
        )
        return {
            'lyrics': lyrics if isinstance(lyrics, str) else None,
            'comment': comment if isinstance(comment, str) else None,
            'dj': dj if isinstance(dj, dict) else None,
        }

    async def _meta_with_ack_fallback(self, info: dict, requested_by: str) -> dict:
        """冷啟動 meta fetch + 5s timeout fallback。"""
        try:
            return await asyncio.wait_for(
                self._fetch_song_meta(info),
                timeout=self._COLD_META_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            title = info.get('title', '未知曲目')
            logger.warning(
                f"⚠️ [Stream] _fetch_song_meta >{self._COLD_META_TIMEOUT_S}s timeout, "
                f"用 hardcoded fallback (song={title}, by={requested_by})"
            )
            who = requested_by or "某人"
            return {
                "lyrics": None,
                "comment": None,
                "dj": {
                    "text": f"下一首是《{title}》，{who} 點的。",
                    "audio_path": None,
                },
            }

    async def _fire_puck_crossfade(self, puck_client, next_url: str,
                                    buffer_s: float = 2.0, crossfade_s: float = 4.0) -> None:
        """[PuckMixer] Pi mk2 純音樂 crossfade：queue_next 後留 buffer_s 給 Pi 背景
        yt-dlp+ffmpeg 起手緩衝，再送 crossfade。跟本地 Discord mixer/DJ 口白邏輯
        完全獨立（見呼叫點 _run_tail_dj），queue_next 失敗就放棄、不重試（下一輪
        tail-fire 或下一首開頭會再給機會）。"""
        ok = await puck_client.queue_next(next_url)
        if not ok:
            logger.warning(f"[PuckMixer] queue_next 失敗，放棄本次 crossfade: {next_url}")
            return
        await asyncio.sleep(buffer_s)
        await puck_client.crossfade(crossfade_s)

    async def _run_tail_dj(self, cur_info: dict, song_start_time: float):
        """[DJ Tail] 滑動窗串場：當前歌結束前 _DJ_TAIL_LEAD_S 秒點火，DJ 疊當前歌尾巴 + 溢進下一首開頭。

        關鍵：點火時刻只依「當前歌 duration」算（開播即可知），**下一首在點火當下
        才從 stream_queue[0] 抓**——因為 autopilot 常在播放中才把下一首排入 queue，
        開播時綁定會抓到空的。DJ 掛 mixer TTS 層（與 _music 獨立），set_music_source
        換歌不中斷 DJ、音樂持續 duck → DJ 自然橫跨切歌點。

        任何無法安全派發的情境（duration 未知/歌太短/已過窗、點火時沒有下一首、
        下一首無預渲染 audio、被 skip、私語模式）一律 return，讓下一首走舊路
        （混進開頭 or _maybe_play_dj_interjection）。
        """
        from dj_tail_schedule import tail_dj_fire_delay

        title_cur = cur_info.get('title', '?')

        duration = cur_info.get('duration')
        if not duration:
            logger.info(f"[DJ Tail] {title_cur} duration 未知，退回舊行為")
            return
        # 精華起播（highlight_start_s）讓實際播放時間軸位移了一截——elapsed 是從
        # 「起播那秒」算起，尾段點火要抓的是「離實際結束還有多久」，duration 要跟著扣掉
        # 位移，否則會算成離結尾還很久（其實早就快撥完了），點火時間表全錯。
        if cur_info.get('highlight_start_s'):
            duration = max(0.0, duration - cur_info['highlight_start_s'])

        elapsed = time.time() - song_start_time
        # 滑動窗：當前歌結束前 _DJ_TAIL_LEAD_S 秒點火，DJ（~15s）疊尾巴 + 溢進下一首開頭。
        delay = tail_dj_fire_delay(duration, elapsed, lead_s=_DJ_TAIL_LEAD_S)
        if delay is None:
            logger.info(f"[DJ Tail] {title_cur} 過窗或歌太短，退回舊行為")
            return

        logger.info(f"[DJ Tail] {title_cur} 尾段點火倒數 {delay:.1f}s")
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            logger.info(f"[DJ Tail] {title_cur} 尾段 task 被取消（skip/stop）")
            return

        # re-check：歌仍在播、沒被 skip、仍是同一首
        if not self.stream_mode:
            logger.info(f"[DJ Tail] {title_cur} stream 已停，不派發")
            return
        if getattr(self, '_current_song_skipped', False):
            logger.info(f"[DJ Tail] {title_cur} 已被 skip，不派發")
            return
        if self._current_stream_info is not cur_info:
            logger.info(f"[DJ Tail] {title_cur} 歌已切換，不派發")
            return

        # 點火當下才抓下一首（此時 autopilot 幾乎必定已排入 queue）
        next_info = self.stream_queue[0] if self.stream_queue else None
        if next_info is None:
            logger.info(f"[DJ Tail] {title_cur} 點火時 queue 仍空、無下一首，退回舊行為")
            return
        title_next = next_info.get('title', '?')

        # [PuckMixer] pi_bt 硬體(Pi mk2)：額外送純音樂 crossfade 訊號給 Pi，跟下面
        # 本地 Discord mixer 的 DJ 口白邏輯完全獨立、不共用旗標、不影響其他硬體行為
        # （DJ 口白走另一條未實作的 TTS 串流管線，這裡只管換歌）。fire-and-forget
        # 背景 task，不阻塞/不改變既有 flow 的時序。
        puck_client = _get_puck_client()
        next_url = next_info.get('url', '')
        if puck_client is not None and next_url:
            asyncio.create_task(self._fire_puck_crossfade(puck_client, next_url))

        dj_meta = await self._resolve_tail_dj_meta(next_info)
        if dj_meta is None:
            logger.info(f"[DJ Tail] {title_next} 無可用預渲染 DJ，退回舊行為")
            return

        logger.info(f"[DJ Tail] 點火！疊播 {title_next} 的 DJ 在 {title_cur} 尾段")
        # 2026-07-25：跟 DJ 開場白同時，背景先把下一首整首解碼好（preload_f32_source
        # 消除 mixer 中段爆音的代價是換源前要等整首解碼完；不先做，這段延遲就會落在
        # 「DJ 開場白講完」跟「下一首出聲」中間，變成聽得到的中斷）。DJ 開場白＋尾段疊播
        # 還有 ~_DJ_TAIL_LEAD_S 秒窗口，剛好夠蓋掉解碼時間。
        self._start_music_preload(next_info)
        await self._maybe_play_dj_interjection(dj_meta)
        await self._play_dj_tail_sfx(next_info)
        next_info['_dj_played_in_tail'] = True
        logger.info(f"[DJ Tail] {title_next} 已標記 _dj_played_in_tail=True")

    def _start_music_preload(self, info: dict) -> None:
        """[DJ Tail] 背景預解碼下一首整首音樂進記憶體，供 play_stream_song 換源時直接用
        （見 _resolve_music_source）。idempotent：同一 url 不重複起 task。

        cache 最多留 2 個未被領取的 task——一首完整解碼是幾十 MB，正常路徑幾秒內就會被
        play_stream_song 領走清掉，這裡只是防呆（例如點火後又被 skip，task 沒人領走）。
        """
        url = info.get('url', '')
        if not url or url in self._preload_music_cache:
            return
        while len(self._preload_music_cache) >= 2:
            _stale_url, stale_task = self._preload_music_cache.popitem()
            stale_task.cancel()

        highlight = info.get('highlight_start_s')

        async def _do():
            from local_mixing_source import S16ToF32MusicSource, preload_f32_source
            p12_opts = {
                'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -probesize 32M',
                'options': '-vn -bufsize 512k',
            }
            if highlight:
                # -ss 放在 -i 前（input seeking），ffmpeg 用容器索引快跳，不必解碼到那秒。
                p12_opts['before_options'] = f'-ss {highlight:.2f} ' + p12_opts['before_options']
            s16 = discord.FFmpegPCMAudio(url, **p12_opts)
            return await asyncio.to_thread(preload_f32_source, S16ToF32MusicSource(s16))

        self._preload_music_cache[url] = asyncio.create_task(_do())

    async def _resolve_music_source(self, url: str, ffmpeg_factory):
        """回傳 (preloaded, fresh_s16_source)——恰好一個非 None。

        cache 有這個 url 的預解碼 task（完成或進行中皆可，await 等它）就用，領走後從 cache
        清掉；沒有、或預解碼失敗（例如網路斷）→ 退回現場用 ffmpeg_factory() 建全新 s16 音源
        （跟修這個之前的行為一致，不會因為預解碼失敗就播不出來）。
        """
        preload_task = self._preload_music_cache.pop(url, None)
        if preload_task is not None:
            try:
                source = await preload_task
                logger.info("[DJ Tail] 換源命中預解碼，零等待")
                return source, None
            except Exception as e:
                logger.info(f"[DJ Tail] 預解碼失敗，退回現場解碼: {e}")
        return None, ffmpeg_factory()

    async def _resolve_tail_dj_meta(self, next_info: dict) -> dict | None:
        """取下一首已預渲染的 DJ meta（有 audio 檔才回）；不可用回 None（退回舊路）。

        下一首若還沒 prefetch（autopilot 較晚排入 queue）→ 現場補建一個並存回 cache，
        供後續 loop 複用（不重複 fetch）。這樣點火時一定拿得到 DJ、不會白白退回開頭。
        """
        url = next_info.get('url', '')
        prefetch_task = self._prefetch_cache.get(url)
        if prefetch_task is None:
            prefetch_task = asyncio.create_task(self._fetch_song_meta(next_info))
            if url:
                self._prefetch_cache[url] = prefetch_task
        try:
            meta = await prefetch_task
        except asyncio.CancelledError:
            return None
        except Exception as e:
            logger.info(f"[DJ Tail] prefetch 失敗: {e}")
            return None
        if not isinstance(meta, dict):
            return None
        dj_meta = meta.get('dj')
        if not isinstance(dj_meta, dict):
            return None
        audio_path = dj_meta.get('audio_path')
        if not audio_path or not os.path.exists(audio_path):
            return None
        return dj_meta

    async def _play_tail_dj_after_skip(self, next_info: dict) -> None:
        """手動 skip 後背景解析/播放 DJ 串場，逾時或出錯都不影響已經生效的 skip。"""
        try:
            timeout_s = self._SEAMLESS_SKIP_TIMEOUT_S
            try:
                dj_meta = await asyncio.wait_for(
                    self._resolve_tail_dj_meta(next_info),
                    timeout=timeout_s,
                )
            except asyncio.TimeoutError:
                logger.warning(f"⚠️ [Seamless Skip] DJ meta 背景解析逾時 >{timeout_s}s，放棄串場")
                return

            if dj_meta is not None:
                next_info['_dj_played_in_tail'] = True
                await self._maybe_play_dj_interjection(dj_meta)
        except Exception as e:
            logger.warning(f"⚠️ [Seamless Skip] 背景 DJ 串場出錯: {e}")

    async def _maybe_play_dj_interjection(self, dj: dict | None):
        """播放預先生成的 DJ 播報。有預渲染音訊則直接播檔案，否則即時串流。"""
        if not dj:
            return
        text = dj.get('text', '')
        audio_path = dj.get('audio_path')
        if not text:
            return

        vc = self._vc()
        if vc is None:
            return
        # 私語模式：聽>>講，不主動唸 DJ 播報（autopilot 與今夜歌單共用此路）
        if getattr(vc, '_intimate_mode', False):
            return
        vc._tts_protected = True
        try:
            if audio_path and os.path.exists(audio_path):
                # 尾段 DJ：走 TTS 層（duck 音樂、非阻塞、撐過歌1→歌2 換源）。
                # 不可用 play_local_file——那條把檔案設成音樂層來源會替換掉正在播的歌，
                # DJ 只播到切歌點就被下一首蓋掉（使用者實測「只聽到狗與露就停」）。
                await vc.play_dj_on_tts_layer(audio_path)
            else:
                await vc.play_tts(text, already_in_channel=True)
        finally:
            vc._tts_protected = False

    async def _synthesize_dynamic_scratch(self, next_info: dict) -> str | None:
        """抓下一首已預解碼的 PCM、即時合成專屬該曲的黑膠刷碟聲。抓不到/沒 ready/合成
        失敗一律回 None——刻意不留靜態備用檔，交給呼叫端直接放棄這輪 SFX（見
        _play_dj_tail_sfx：沒有 fallback 音效，播不出來就是這輪真的沒抓到 PCM，訊號
        要乾淨，別用預錄音檔混過去）。

        preload 是背景整首解碼，點火當下十之八九還沒好——與其一次性 done() 檢查
        （幾乎必定 miss），改用 wait_for 主動等一小段（在 _DJ_TAIL_LEAD_S 的窗口內
        仍有餘裕），拉高真正用上真實 PCM 的機率。asyncio.shield：等待逾時只放棄
        「這次用它」，不能連 preload task 本身也砍掉——它還要留給 _resolve_music_source
        換源時用。
        """
        url = next_info.get('url', '')
        preload_task = self._preload_music_cache.get(url)
        if preload_task is None or preload_task.cancelled():
            return None

        try:
            preloaded = await asyncio.wait_for(
                asyncio.shield(preload_task), timeout=_DJ_TAIL_SFX_PRELOAD_WAIT_S,
            )
        except (asyncio.TimeoutError, asyncio.CancelledError):
            return None
        except Exception as e:
            logger.debug(f"[DJ Tail] preload 讀取失敗: {e}")
            return None

        try:
            frames = getattr(preloaded, '_frames', None)
            if not frames or len(frames) < 50:
                return None
            import hashlib
            import numpy as np
            from scripts.gen_dj_sfx import gen_scratch_from_pcm, _write_wav
            raw_bytes = b"".join(frames[:100])
            raw_f32 = np.frombuffer(raw_bytes, dtype=np.float32).reshape(-1, 2)
            dynamic_samples = gen_scratch_from_pcm(raw_f32, rate=48000)
            # 固定檔名在多首歌同時點火時會互踩（寫入中被下一次點火覆蓋/搶讀半寫檔），
            # 用 url hash 隔開。
            url_hash = hashlib.md5(url.encode()).hexdigest()[:10]
            dynamic_path = f"/tmp/scratch_dynamic_{url_hash}.wav"
            _write_wav(dynamic_path, dynamic_samples)
            return dynamic_path
        except Exception as e:
            logger.debug(f"[DJ Tail] 動態 scratch 合成失敗: {e}")
            return None

    async def _play_dj_tail_sfx(self, next_info: dict | None = None):
        """[DJ Tail] DJ 口白播完後，隨機疊一支轉場音效（riser 合成自
        scripts/gen_dj_sfx.py；dj_airhorn 方波太刺耳已從輪替池移除，函式仍留著給
        gen_dj_sfx.py 素材產出用；shoutout 是 edge-tts 用 Marvin 現役聲線 zh-TW-YunJheNeural
        rate=-20% pitch=-15Hz 錄的「Yo，DJ 馬文！」報名 stamp；scratch 是即時抓下一首
        PCM 合成的動態黑膠刷碟聲）進 TTS 層——跟口白同一條佇列接續播出，落在尾段疊播
        溢進下一首開頭的窗口內。

        scratch 沒有靜態備用檔：抓不到下一首 PCM 就這輪不放，不用預錄音檔頂替——
        「這次沒聽到刷碟聲」本身就是訊號，別讓 fallback 把失敗蓋掉。找不到 vc 就靜靜
        放棄，不影響主流程。"""
        vc = self._vc()
        if vc is None:
            return
        name = random.choice(_DJ_TAIL_SFX_NAMES)

        if name == "scratch":
            path = await self._synthesize_dynamic_scratch(next_info) if next_info else None
            if path is None:
                logger.info("[DJ Tail] SFX：scratch 抽中但沒抓到下一首 PCM，這輪不放")
                return
            logger.info("[DJ Tail] SFX：scratch（動態合成）")
        else:
            path = os.path.join(_DJ_TAIL_SFX_DIR, f"{name}.wav")
            if not os.path.exists(path):
                return
            logger.info(f"[DJ Tail] SFX：{name}")

        try:
            # 轉場音效不是講話，音量比照音樂 10% 感受，別用口白的滿幅正規化（太搶戲）。
            await vc.play_dj_on_tts_layer(path, peak=0.1)
        except Exception as e:
            logger.debug(f"⚠️ [DJ Tail] SFX 疊播失敗（不影響主流程）: {e}")


    async def _analyze_song_reactions(self, info: dict, song_start_time: float, lyrics: str):
        """歌曲結束後掃描對話，分析聆聽反應並寫入音樂記憶。"""
        if not hasattr(self.bot, 'music_memory'):
            return
        conv = self.bot.engine.conv_buffer
        elapsed = time.time() - song_start_time
        harvest = conv.get_harvest(song_start_time, before=5.0, after=elapsed + 2.0)
        if not harvest.strip():
            return

        lyrics_hint = lyrics[:400] if lyrics else "無歌詞資料"
        prompt = (
            f"歌曲《{info['title']}》剛才播放完畢。\n\n"
            f"播放期間的對話：\n{harvest}\n\n"
            f"歌詞片段：{lyrics_hint}\n\n"
            "請分析每位成員對這首歌的反應，**只記錄有明顯感受的人**。\n"
            "輸出 JSON（不加 markdown）：\n"
            '{"reactions": {"成員名": {"feelings": ["情緒詞"], "quotes": ["他說的具體語句"], '
            '"lyric_match": "歌詞與他的話的呼應描述，無則空字串"}}}'
        )
        try:
            import json as _json
            raw = await self.bot.router._call_llm(
                system_prompt="你是音樂聆聽反應分析助手，只記錄有明顯情感的成員，不過度推測。",
                user_prompt=prompt,
                is_json=True,
                tier="simple",
            )
            reactions = _json.loads(raw).get("reactions", {})
            if reactions:
                self.bot.music_memory.record_reactions(info, reactions)
                logger.info(f"🎵 [MusicMemory] 記錄 {len(reactions)} 人的反應: {info['title']}")
                try:
                    from bridge_emitters import emit_music_reaction_to_bridge
                    for username, r in reactions.items():
                        feelings = r.get("feelings", []) or []
                        tag = "love" if feelings else "silent"
                        asyncio.create_task(emit_music_reaction_to_bridge(
                            self.bot, username, info, tag
                        ))
                except Exception as e:
                    logger.debug(f"⚠️ [Companion_Bridge] music_reaction hook skipped: {e}")
        except Exception as e:
            logger.debug(f"⚠️ [MusicMemory] 反應分析失敗: {e}")

    async def _get_audio_duration(self, path: str) -> float:
        """使用 ffprobe 取得本地音訊檔案的時長（秒）。"""
        try:
            import json as _json
            ffprobe = "/opt/homebrew/bin/ffprobe" if os.path.exists("/opt/homebrew/bin/ffprobe") else "ffprobe"
            proc = await asyncio.create_subprocess_exec(
                ffprobe, '-v', 'quiet', '-print_format', 'json', '-show_streams', path,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            data = _json.loads(stdout)
            for stream in data.get('streams', []):
                if stream.get('codec_type') == 'audio':
                    return float(stream.get('duration', 3.0))
        except Exception:
            pass
        return 3.0

    async def _measure_norm_gain_bg(self, url: str):
        """[響度正規化] 背景取樣歌曲 25/50/75% 三點量整合響度 → 算常數增益存 _stream_norm_gain[url]。

        順便在同一趟 ffmpeg（同取樣點、同一個 process 兩個輸出：ebur128→null 給
        stderr 響度統計、raw f32le mono→stdout 給 BPM 估算）取 PCM 估 BPM，落地存
        records/song_bpm.json（見 bpm_estimate.py）——BPM 分析不擋、不影響既有響度
        正規化行為，失敗只是沒存到 BPM。"""
        if url in self._stream_norm_gain:
            return
        import numpy as np

        from bpm_estimate import estimate_bpm_from_pcm, median_bpm, write_bpm
        from loudness_norm import (
            sample_positions, parse_ebur128_integrated, average_lufs, compute_loudness_gain,
        )
        info = self._current_stream_info or {}
        duration = float(info.get("duration") or 0)
        lufs_vals: list[float | None] = []
        bpm_vals: list[float | None] = []
        for pos in sample_positions(duration):
            try:
                proc = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-nostats", "-ss", f"{pos:.1f}", "-t", "20", "-i", url,
                    "-af", "ebur128", "-f", "null", "-",
                    "-vn", "-ac", "1", "-ar", str(_BPM_SAMPLE_SR), "-f", "f32le", "pipe:1",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
                lufs_vals.append(parse_ebur128_integrated(stderr.decode("utf-8", "ignore")))
                pcm = np.frombuffer(stdout, dtype=np.float32)
                bpm_vals.append(estimate_bpm_from_pcm(pcm, _BPM_SAMPLE_SR))
            except Exception:
                lufs_vals.append(None)
                bpm_vals.append(None)
        video_id = extract_video_id(info.get("webpage_url") or info.get("url") or "")
        bpm = median_bpm(bpm_vals)
        if bpm is not None and video_id:
            write_bpm(_SONG_BPM_STORE, video_id, bpm)
            logger.info(f"🥁 [BPM] {url[:40]} 估計 {bpm:.0f} BPM → 存 {video_id}")
        avg = average_lufs(lufs_vals)
        if avg is None:
            logger.warning(f"⚠️ [LoudNorm] {url[:40]} 響度量測無結果，用 raw 音量")
            return
        gain = compute_loudness_gain(avg)
        self._stream_norm_gain[url] = gain
        logger.info(f"🎚️ [LoudNorm] 量測完成 I≈{avg:.1f} LUFS → 增益 {gain:.2f}x（每首套一次）")

    def _extract_song_metadata(self, file_path: str):
        """📻 [Marvin Radio] 使用 ffprobe 提取標題與演出者。"""
        try:
            ffprobe_path = "/opt/homebrew/bin/ffprobe" if os.path.exists("/opt/homebrew/bin/ffprobe") else "ffprobe"
            cmd = [ffprobe_path, "-v", "quiet", "-print_format", "json", "-show_format", file_path]
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            data = json.loads(result.stdout)
            tags = data.get("format", {}).get("tags", {})
            return {
                "title": tags.get("title", os.path.basename(file_path)),
                "artist": tags.get("artist", "未知藝術家")
            }
        except Exception as e:
            logger.error(f"⚠️ [Radio Metadata] 提取失敗: {e}")
            return {"title": os.path.basename(file_path), "artist": "未知藝術家"}

    def _extract_song_cover(self, file_path: str):
        """📻 [Marvin Radio] 使用 ffmpeg 提取封面至暫存檔。"""
        try:
            temp_fd, temp_path = tempfile.mkstemp(suffix=".jpg")
            os.close(temp_fd)
            ffmpeg_path = "/opt/homebrew/bin/ffmpeg" if os.path.exists("/opt/homebrew/bin/ffmpeg") else "ffmpeg"
            cmd = [ffmpeg_path, "-y", "-i", file_path, "-an", "-vcodec", "copy",
                   "-f", "image2", "-frames:v", "1", temp_path]
            subprocess.run(cmd, capture_output=True, check=True)
            if os.path.exists(temp_path) and os.path.getsize(temp_path) > 0:
                return temp_path
            if os.path.exists(temp_path):
                os.remove(temp_path)
            return None
        except Exception:
            if 'temp_path' in locals() and os.path.exists(temp_path):
                os.remove(temp_path)
            return None

    def _extract_dominant_color(self, cover_path: str) -> discord.Color:
        """📻 [Marvin Radio] 從封面圖提取主色調，回傳 discord.Color。"""
        try:
            from PIL import Image
            img = Image.open(cover_path).convert("RGB")
            img = img.resize((60, 60), Image.LANCZOS)
            quantized = img.quantize(colors=8)
            palette = quantized.getpalette()
            best_color = None
            best_score = -1.0
            for i in range(8):
                r, g, b = palette[i * 3], palette[i * 3 + 1], palette[i * 3 + 2]
                lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255.0
                if lum < 0.10 or lum > 0.90:
                    continue
                max_c = max(r, g, b) / 255.0
                min_c = min(r, g, b) / 255.0
                denom = 1.0 - abs(2.0 * lum - 1.0)
                sat = (max_c - min_c) / denom if denom > 0.001 else 0.0
                score = sat * 0.7 + (1.0 - abs(lum - 0.5) * 2) * 0.3
                if score > best_score:
                    best_score = score
                    best_color = (r, g, b)
            if best_color:
                return discord.Color.from_rgb(*best_color)
        except Exception as e:
            logger.debug(f"⚠️ [Cover Color] 提取失敗: {e}")
        return discord.Color.dark_grey()

    async def _delayed_cleanup(self, file_path: str, delay: float = 10.0):
        """📻 [Marvin Radio] 延後刪除暫存檔，確保 Discord 上傳完成。"""
        try:
            await asyncio.sleep(delay)
            if os.path.exists(file_path):
                os.remove(file_path)
        except Exception:
            pass

    # ── Phase 7F: queue / resolve helpers ────────────────────────────────────

    def _check_song_duplicate(self, url: str, title: str, username: str,  # noqa: ARG002
                              *, webpage_url: str = "", check_history: bool = True) -> bool:
        """回傳 True 表示此 session 已有同一首歌，應跳過加入佇列。

        check_history=False：只擋「還在佇列」，不擋「本場播過」。給使用者手動點播用——
        skip 過的歌進了 stream_history，但手動點回來是刻意正向更正，應放行。

        但「正在播的那首」一律擋（不受 check_history 影響）：防同一句經 snapshot 喚醒
        + debounce wakeless 兩路徑各入隊一次造成背對背雙播（2026-06-23 隔壁老樊 incident；
        兩路徑相隔 12s，時間窗去重全過期、#1 已開播不在佇列 → 漏。內容去重不怕時序）。

        身份比對兩層（同 video-id 或同正規化歌名即視為重複）：
        ① **穩定 video-id**（從 webpage_url 抽），不是 info['url']——後者是 yt-dlp 每次解析
           都重產的 googlevideo 暫時串流網址（帶 expiry token），同一首歌兩次解析會得到不同
           url，比 url 永遠不等 → 同歌入隊兩首（2026-06-29 對等關係 incident）。
        ② **normalize_title 正規化歌名**：擋同名變體（cover/live/重傳但不同 video-id）。歌手
           仍在原始標題裡 → 同名不同曲衝突低。兩層都拿不到才退回舊 url 比對。
        """
        cand_vid = extract_video_id(webpage_url or url or "")
        cand_nt = normalize_title(title or "")

        def _same(item: dict) -> bool:
            iv = extract_video_id(item.get("webpage_url") or item.get("url") or "")
            if cand_vid and iv and iv == cand_vid:
                return True  # ① 同一個 YouTube 影片
            it = normalize_title(item.get("title") or "")
            if cand_nt and it and it == cand_nt:
                return True  # ② 同名變體
            if not cand_vid and not cand_nt:  # 候選毫無穩定身份 → 退回舊 url 比對
                return bool(url) and item.get("url") == url
            return False

        cur = self._current_stream_info
        if cur and _same(cur):
            return True
        for item in self.stream_queue:
            if _same(item):
                return True
        if check_history:
            for item in self.stream_history:
                if _same(item):
                    return True
        return False

    @staticmethod
    def _normalize_request_query(query: str) -> str:
        """點歌字串正規化，當『同一句重派』去重 key：去前綴喚醒/播放動詞 + 空白 + 大小寫。

        不靠『播』動詞本身比對（'播放X' 與 '播X' 去掉動詞後同一句），STT 把播聽成波也只差
        在被去掉的前綴。注意：這是「同句去重」用的，不是歌名標準化（同名異曲交給內容去重）。
        """
        import re
        q = (query or "").strip().casefold()
        q = re.sub(r"^(馬文|马文|marvin)?\s*(幫我|帮我|請|请|麻煩|麻烦)?\s*"
                   r"(播放一下|播放|播|放一下|放|來首|来首|來|来|點播|点播|點|点)\s*", "", q)
        return re.sub(r"\s+", "", q)

    @staticmethod
    def _user_song_insert_index(queue: list[dict]) -> int:
        """使用者自選曲的插入位置：排在所有既有使用者曲之後、第一首 Marvin 自動曲之前。"""
        for i, item in enumerate(queue):
            if str(item.get('requested_by') or '').startswith('Marvin'):
                return i
        return len(queue)

    def _queue_user_song(self, info: dict) -> None:
        """使用者自選曲照點歌順序排（FIFO），插在既有使用者曲之後、auto-recommend 之前。

        skip-override：手動點播蓋過先前 skip——記 played_again + 重置 consecutive-skip 計數。
        """
        # 🎵 [ReqDedup] 同人同曲 30s 去重：佇列去重只看佇列（第一發已 pop 去播時
        # 佇列空、第二發漏過，7/3-4 實錘）；ledger 與佇列狀態無關（唯一入隊點）
        _vid = extract_video_id(info.get('webpage_url') or '')
        _spk = info.get('requested_by') or ''
        if _vid:
            if self._req_ledger.is_dup(_spk, _vid, time.time()):
                logger.info(f"🎵 [ReqDedup] {_spk} 30s 內重複點 {_vid}，跳過入隊（誤觸/殘餘）")
                return
            self._req_ledger.mark(_spk, _vid, time.time())
        self.stream_queue.insert(self._user_song_insert_index(self.stream_queue), info)
        self._republish_queue_snapshot()
        # 🎵 [Play-First] 點歌當下就背景預取 meta，讓 DJ/歌詞大多來得及（又不阻塞出聲）
        _u = info.get('url', '')
        if _u and _u not in self._prefetch_cache:
            try:
                self._prefetch_cache[_u] = asyncio.create_task(self._fetch_song_meta(info))
            except RuntimeError:
                pass  # 無 running loop（同步/測試呼叫）→ 略過預取
        try:
            user = info.get('requested_by') or ''
            title = info.get('title') or ''
            mm = getattr(self.bot, 'music_memory', None)
            if mm and user and title:
                mm.add_recommendation_feedback(user, title, "played_again")
            # _consecutive_skips_by_url 仍在 VC；透過 _vc() 存取
            vc = self._vc()
            if vc is not None:
                vc._consecutive_skips_by_url.pop(info.get('url') or '', None)
            import re as _re
            _m = _re.search(r"(?:v=|youtu\.be/|/watch\?v=)([A-Za-z0-9_-]{11})",
                            info.get('webpage_url') or '')
            if _m:
                self._last_user_song_seed = _m.group(1)
                self._auto_since_manual = 0  # 手動點歌 → 重置 freshness，這首當 fresh lead 種子
                self._last_user_song_requester = user or ''  # 控制台「跟誰最近點歌」顯示用
        except Exception:
            logger.debug("[Queue] skip-override / seed 更新失敗", exc_info=True)

    def _cancel_stale_prefetch(self, speaker: str) -> None:
        """bus 接走 intent 時，取消 dangling speculative LLM prefetch。"""
        prefetch_map = getattr(self.bot.router, "_pending_prefetch", None)
        if not isinstance(prefetch_map, dict):
            return
        task = prefetch_map.pop(speaker, None)
        if task is not None and not task.done():
            task.cancel()

    def _record_song_skip(self) -> None:
        """把當前播放歌曲的 videoId 記入持久化 skip 排除集。

        fail-open：拿不到歌/mm 不存在 → no-op。
        """
        mm = getattr(self.bot, 'music_memory', None)
        cur = self._current_stream_info
        if mm is None or not cur:
            return
        url = cur.get("webpage_url") or cur.get("url") or ""
        if url:
            try:
                mm.record_skipped_video_id(url)
                from taste_fingerprint import artist_of
                _artist = artist_of(cur.get("title", ""))
                if _artist:
                    mm.record_artist_skip(_artist, url)
            except Exception:
                logger.exception("[Skip] record_skipped_video_id 失敗")

    def _build_recommendation_extras(self) -> dict:
        """給 recommendation log 灌 controller scope 的 rich context。read-only / sync。"""
        extras: dict = {
            "queue_depth": len(self.stream_queue),
            "recent_history_titles": [
                s.get("title", "") for s in self.stream_history[-3:]
                if isinstance(s, dict)
            ],
        }
        if self._mood_sensor is not None:
            cached_vibe = getattr(self._mood_sensor, "_cache", None)
            if cached_vibe is not None:
                extras["vibe_mood"] = getattr(cached_vibe, "mood", None)
        return extras

    async def _resolve_yt_query(self, query: str, force_fresh: bool = False) -> dict | None:
        """使用 yt-dlp 解析搜尋關鍵字或 URL，回傳串流資訊 dict。在 executor 中執行以避免阻塞。

        force_fresh：跳過所有快取，強制重抓（403 重試專用——串流 URL 過期時快取存的
        是同一份死 URL，命中只會再 403，必須真的重新 extract 拿新 URL）。
        """
        from music_search import pick_best_music_candidate
        from youtube_heatmap import pick_highlight_start

        if is_memory_critical():
            logger.warning("⚠️ [Stream] memory critical, skipping yt-dlp resolve")
            return None

        # 🎵 [QueryCache] 文字查詢點過的歌 → 拿回 videoId URL，跳過 ytsearch5(~6s)
        _orig_text_query = query if not query.startswith('http') else None
        _used_query_cache = False
        if _orig_text_query is not None and not force_fresh:
            _qhit = self._query_resolve_cache.get(_orig_text_query)
            if _qhit and _qhit.get('webpage_url'):
                logger.info(f"🎵 [QueryCache] '{_orig_text_query[:30]}' 命中→{_qhit.get('title','')[:30]}，改 URL 解析跳搜尋")
                query = _qhit['webpage_url']
                _used_query_cache = True

        # 🎵 [ResolveCache] URL 直點且 1h 內解析過 → 免重抽 ~2s（重複點播是常態使用模式）
        _cache_vid = extract_video_id(query) if query.startswith('http') else None
        if _cache_vid and not force_fresh:
            _cached = self._yt_resolve_cache.get(_cache_vid, time.time())
            if _cached is not None:
                logger.info(f"🎵 [ResolveCache] {_cache_vid} 快取命中，跳過 yt-dlp")
                return _cached

        ydl_opts = {
            'format': 'bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best',
            'quiet': True,
            'no_warnings': True,
            'noplaylist': True,
            # ytsearch5 抽 5 個候選時，其中一支不可用(移除/地區鎖)不該讓整個搜尋 raise。
            # ignoreerrors → 壞片變 None（下方 `if e` 過濾已接），改用可用候選。
            # （2026-06-22 incident：sk9fkcxhYRw This video is not available 整單炸。）
            'ignoreerrors': True,
        }
        is_url = query.startswith('http')

        def _extract():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                if is_url:
                    info = ydl.extract_info(query, download=False)
                    if not info:
                        return None
                    chosen = info if 'url' in info else None
                else:
                    info = ydl.extract_info(f'ytsearch5:{query}', download=False)
                    entries = [e for e in (info.get('entries') or []) if e] if info else []
                    if not entries:
                        return None
                    chosen = pick_best_music_candidate(entries)
                    if chosen:
                        logger.info(
                            f"🎵 [Stream] 候選中挑出：{chosen.get('title','?')[:40]} "
                            f"(category={chosen.get('categories', [])})"
                        )
                if not chosen or 'url' not in chosen:
                    return None
                _duration = chosen.get('duration', 0)
                return {
                    'title': chosen.get('title', 'Unknown'),
                    'uploader': chosen.get('uploader', chosen.get('channel', 'Unknown')),
                    'url': chosen['url'],
                    'thumbnail': chosen.get('thumbnail'),
                    'webpage_url': chosen.get('webpage_url', ''),
                    'duration': _duration,
                    # 「最多人重播」熱力圖挑出的精華起點；沒有/太短/太靠尾聲則 None
                    # （=從頭播，跟舊行為相容）。播放端（play_stream_song/_start_music_preload/
                    # _run_tail_dj）要用這個位移調整實際起播點與尾段點火時間表。
                    'highlight_start_s': pick_highlight_start(chosen.get('heatmap'), _duration),
                }

        def _cache_put(res):
            # 成功解析回填快取（鍵用結果的 videoId——搜尋型 query 也受益於後續 URL 直點）
            if res:
                _rv = extract_video_id(res.get('webpage_url') or '')
                if _rv:
                    self._yt_resolve_cache.put(_rv, res, time.time())
                # 文字查詢解析成功 → 記住 query→url，下次同句跳 ytsearch5
                if _orig_text_query and res.get('webpage_url'):
                    self._query_resolve_cache.put(_orig_text_query, res['webpage_url'], res.get('title', ''))
            return res

        loop = asyncio.get_event_loop()

        async def _extract_with_retry():
            try:
                return await loop.run_in_executor(None, _extract)
            except OSError as e:
                if getattr(e, "errno", None) == 11:
                    logger.warning("⚠️ [Stream] yt-dlp Errno 11 deadlock，200ms 後重試")
                    await asyncio.sleep(0.2)
                    try:
                        return await loop.run_in_executor(None, _extract)
                    except Exception as e2:
                        logger.error(f"❌ [Stream] yt-dlp 重試後仍失敗: {e2}", exc_info=True)
                        return None
                logger.error(f"❌ [Stream] yt-dlp 解析失敗 (OSError): {e}", exc_info=True)
                return None

        res = await _extract_with_retry()
        if res is None and _used_query_cache:
            # 快取的 URL 失效（影片下架/地區鎖等）→ 清掉，改用原始文字重搜。
            # 透明 fallback：同一次請求就換到替代連結，使用者無感，不是「清掉就不播」。
            logger.info(f"🎵 [QueryCache] 快取 URL 失效，清除並用原文字重搜 '{_orig_text_query[:30]}'")
            self._query_resolve_cache.delete(_orig_text_query)
            query = _orig_text_query
            is_url = False
            res = await _extract_with_retry()
        res = await self._apply_itunes_cover(res)
        return _cache_put(res)

    async def _apply_itunes_cover(self, res):
        """用 iTunes 方形專輯封面取代 YT 縮圖（失敗/低信心一律退回原縮圖）。

        單一改點：res['thumbnail'] 是全站封面唯一源頭（音樂卡 PIL、embed、/now 顯示端），
        在此換掉即全部沿用；且解析在進快取前完成，ResolveCache 免費快取不重打 iTunes。
        """
        if not res:
            return res
        try:
            import itunes_cover
            yt = res.get('thumbnail')
            art = await itunes_cover.resolve_cover(
                res.get('title', ''), res.get('uploader'), fallback=yt
            )
            if art and art != yt:
                res['yt_thumbnail'] = yt
                res['thumbnail'] = art
                logger.info(f"🎨 [Cover] iTunes 封面取代 YT 縮圖：{(res.get('title') or '?')[:30]}")
        except Exception as e:
            logger.warning(f"⚠️ [Cover] iTunes 解析失敗，用原縮圖：{type(e).__name__}: {e}")
        # 從最終封面抽主色調色盤（給 vinyl splatter 用；失敗 → [] 不影響封面）
        try:
            import cover_palette
            res['palette'] = await cover_palette.extract_palette(res.get('thumbnail'), n=4)
        except Exception as e:
            logger.warning(f"⚠️ [Cover] 抽色失敗：{type(e).__name__}: {e}")
        return res

    async def _safe_music_command(self, speaker: str, query: str, cmd: str):
        """Top-level wrapper：任何 music command 路徑都該過這層 try/except。"""
        try:
            await self._handle_voice_music_command(speaker, query, cmd)
        except Exception as e:
            logger.error(
                f"❌ [Music Command Crash] {speaker} {cmd} '{query[:40]}': "
                f"{type(e).__name__}: {e}",
                exc_info=True,
            )
            vc = self._vc()
            if vc:
                asyncio.create_task(vc._play_ack("music_fail", speaker=speaker))
            ch = vc.active_text_channel if vc else None
            if ch:
                try:
                    await ch.send(
                        f"❌ 音樂系統暫時出錯了 (`{type(e).__name__}`)，等一下再試。"
                    )
                except Exception:
                    pass

    async def _handle_voice_music_command(self, speaker: str, query: str, cmd: str):
        """執行語音觸發的音樂指令，回應只貼頻道不走 TTS。

        入口 dedup：同 speaker 5s 內重複呼叫直接 silently skip，避免
        IBA-T0 / bus / speculative 多路徑同時觸發造成 yt-dlp 並發
        Errno 11 deadlock（5/18 17:23 incident）。
        """
        _now = time.time()
        _last = self._last_music_cmd_time.get(speaker, 0)
        if _now - _last < self._MUSIC_CMD_DEDUP_WINDOW:
            logger.info(
                f"🎵 [Music Dedup] {speaker} {cmd} 在 {_now - _last:.1f}s 前已觸發過音樂指令，跳過"
            )
            return
        self._last_music_cmd_time[speaker] = _now
        # query-aware 去重：同 speaker + 同正規化點歌字串 → 擋同一句重派（喚醒+無喚醒兩路徑，
        # 相隔可 >5s 超過時間窗）。只對 play（skip/stop 等控制指令不能用同字串擋，會誤殺連按）。
        if cmd == "play":
            _nq = self._normalize_request_query(query)
            _prev = self._last_music_query.get(speaker)
            if _nq and _prev and _prev[0] == _nq and _now - _prev[1] < self._MUSIC_SAME_SONG_WINDOW:
                logger.info(f"🎵 [Music Dedup] {speaker} 同句『{query[:30]}』{_now - _prev[1]:.1f}s 內重複點播，跳過（重派）")
                return
            self._last_music_query[speaker] = (_nq, _now)
        logger.info(f"🎵 [Music Command] {speaker} 觸發語音音樂指令: {cmd} | query='{query[:40]}'")

        vc = self._vc()
        if cmd == "play":
            if vc:
                asyncio.create_task(vc._play_ack("music", speaker=speaker))
        ch = vc.active_text_channel if vc else None
        # 可播放 = 有輸出裝置（本機 LocalSpeakerDevice 或 Discord 連線中 VC）。
        # 不再只認 Discord VC，否則本機模式 play/pause/resume 全被擋。
        _can_play = vc is not None and vc._resolve_playback_device() is not None
        _mixer = vc._mixer if vc else None

        import random

        replies = {
            "skip":   ["⏭️ 好，換下一首。連這首都嫌的話宇宙真的沒希望了。",
                       "⏭️ 跳過。反正每首歌最終都是一樣的空虛。"],
            "stop":   ["⏹️ 停了。寂靜回來了。這才是本質。",
                       "⏹️ 好，音樂停了。沉默果然才是永恆的。"],
            "pause":  ["⏸️ 暫停了。靜止的美，就像我的希望一樣。",
                       "⏸️ 好，我讓它靜止。"],
            "resume": ["▶️ 繼續播了。聲音填補了虛空，但也只是暫時的。",
                       "▶️ 好，繼續。"],
        }

        if cmd == "skip":
            if not self.stream_mode and not self.radio_mode:
                if ch: await ch.send("😑 沒有歌在播，要我跳過什麼？")
                return
            self._record_song_skip()
            self._current_song_skipped = True  # 標記：讓 stream loop 別把 skip 當 403 失敗去重試
            # [DJ Tail] skip → 取消尾段 task，不讓它在下一首開頭前誤觸發
            if self._tail_dj_task is not None and not self._tail_dj_task.done():
                self._tail_dj_task.cancel()
                self._tail_dj_task = None

            # ⏭️ [Quick Skip] 手動 skip 要立即生效，DJ 串場改背景執行不擋路。
            # 原本這裡 await 到 DJ meta 解析/播放完才清空第一首，逼近
            # _SEAMLESS_SKIP_TIMEOUT_S=10s 逾時時使用者會覺得指令沒反應（2026-08-06
            # 事故：喚醒到 skip 生效隔了 10.9s，使用者以為指令沒吃到又講一次）。
            # PCM 預載跟 DJ 串場改丟背景 task，不擋這裡的立即回覆。
            next_info = self.stream_queue[0] if self.stream_queue else None
            if next_info is not None and self.stream_mode:
                self._start_music_preload(next_info)
                asyncio.create_task(self._play_tail_dj_after_skip(next_info))

            if _mixer is not None:
                _mixer.clear_music()
            reply = random.choice(replies["skip"])
            if ch: await ch.send(reply)
            if vc: vc.stt_logger.info(f"[音樂控制→{speaker}] 指令=skip | bot={reply} (plan12=True)")

        elif cmd == "stop":
            if not self.stream_mode and not self.radio_mode:
                if ch: await ch.send("😑 本來就沒在播了。")
                return
            if self.radio_mode:
                await self.stop_radio(reason="語音指令停止")
            if self.stream_mode:
                await self.stop_stream(reason="語音指令停止")
            reply = random.choice(replies["stop"])
            if ch: await ch.send(reply)
            if vc: vc.stt_logger.info(f"[音樂控制→{speaker}] 指令=stop | bot={reply}")

        elif cmd == "pause":
            if not self.stream_mode and not self.radio_mode:
                if ch: await ch.send("😑 沒有在播可以暫停。")
                return
            if not _can_play:
                if ch: await ch.send("😑 找不到語音連線。")
                return
            if self.stream_mode and not self.stream_paused:
                if _mixer is not None:
                    _mixer.set_paused(True)
                self.stream_paused = True
            elif self.radio_mode and not self.stream_mode and not self.radio_paused:
                if _mixer is not None:
                    _mixer.set_paused(True)
                self.radio_paused = True
            else:
                if ch: await ch.send("😑 已經在暫停了。")
                return
            reply = random.choice(replies["pause"])
            if ch: await ch.send(reply)
            if vc: vc.stt_logger.info(f"[音樂控制→{speaker}] 指令=pause | bot={reply} (plan12=True)")

        elif cmd == "resume":
            if not self.stream_paused and not self.radio_paused:
                if ch: await ch.send("😑 沒有東西在暫停。")
                return
            if not _can_play:
                if ch: await ch.send("😑 找不到語音連線。")
                return
            if self.stream_paused:
                if _mixer is not None:
                    _mixer.set_paused(False)
                self.stream_paused = False
            elif self.radio_paused:
                if _mixer is not None:
                    _mixer.set_paused(False)
                self.radio_paused = False
            reply = random.choice(replies["resume"])
            if ch: await ch.send(reply)
            if vc: vc.stt_logger.info(f"[音樂控制→{speaker}] 指令=resume | bot={reply} (plan12=True)")

        elif cmd == "play":
            search = vc._extract_music_search_query(query) if vc else query
            if not _can_play:
                if ch: await ch.send("❌ 我不在語音頻道中，先用 `/summon` 召喚我。")
                return
            if not search:
                if ch: await ch.send("🎵 要放什麼歌？你說了等於沒說。")
                return

            raw_search = search
            correction_note = ""
            wrong = None
            if hasattr(self.bot, 'music_memory') and self.bot.music_memory:
                corrected, wrong = self.bot.music_memory.apply_stt_correction(speaker, search)
                if wrong:
                    search = corrected
                    correction_note = f" *(語音修正：{wrong} → {corrected})*"
            self._last_search[speaker] = {'query': raw_search, 'ts': time.time(), 'source': 'voice'}

            if ch:
                status_msg = await ch.send(f"🔍 **正在搜尋：** `{search}`...{correction_note}")
            else:
                status_msg = None
            info = await self._resolve_yt_query(search)
            if not info:
                if status_msg: await status_msg.edit(content=f"❌ 找不到 `{search}`，就跟意義一樣——不存在。")
                if vc: asyncio.create_task(vc._play_ack("music_fail", speaker=speaker))
                return
            info['requested_by'] = speaker
            if vc:
                vc.stt_logger.info(
                    f"[點歌-語音] 使用者={speaker} | 搜尋={raw_search}{f' (修正→{search})' if wrong else ''} | 結果={info['title']} / {info.get('uploader', '?')}"
                )
            if self._check_song_duplicate(url=info['url'], title=info['title'], username=speaker, webpage_url=info.get('webpage_url', ''), check_history=False):
                # 已在佇列 → 仍要確保 loop 活著（零鍵盤：使用者只能靠再喊一次求救）
                revived = self._ensure_stream_loop()
                if status_msg:
                    await status_msg.edit(content=f"⏭️ 「{info['title']}」已在佇列待播了。"
                                                  + ("（播放已恢復）" if revived else ""))
                return
            if self.radio_mode:
                await self.stop_radio(reason="語音音樂指令接管")
            self._queue_user_song(info)
            if self._ensure_stream_loop():
                from cogs.voice_views import PlayControlView
                existing_view = self._active_control_view
                if ch and existing_view and getattr(existing_view, 'message', None):
                    try:
                        await existing_view.message.edit(embed=existing_view._build_embed(), view=existing_view)
                        if status_msg: await status_msg.delete()
                    except Exception:
                        view = PlayControlView(vc)
                        self._active_control_view = view
                        if status_msg: await status_msg.edit(content=None, embed=view._build_embed(), view=view)
                        if status_msg: view.message = status_msg
                elif ch and status_msg:
                    view = PlayControlView(vc)
                    self._active_control_view = view
                    await status_msg.edit(content=None, embed=view._build_embed(), view=view)
                    view.message = status_msg
            else:
                from cogs.voice_views import PlayControlView
                existing_view = self._active_control_view
                if ch and existing_view and getattr(existing_view, 'message', None):
                    try:
                        await existing_view.message.edit(embed=existing_view._build_embed(), view=existing_view)
                        if status_msg: await status_msg.delete()
                    except Exception:
                        view = PlayControlView(vc)
                        self._active_control_view = view
                        if status_msg: await status_msg.edit(content=None, embed=view._build_embed(), view=view)
                        if status_msg: view.message = status_msg
                elif ch and status_msg:
                    view = PlayControlView(vc)
                    self._active_control_view = view
                    await status_msg.edit(content=None, embed=view._build_embed(), view=view)
                    view.message = status_msg

    async def _handle_find_song(self, mode: str, payload: str, speaker: str):
        """FindSongAgent handler：依模式識別歌名 → 報出識別結果 → 交給播放路徑。"""
        vc = self._vc()
        ch = vc.active_text_channel if vc else None
        ident: str = ""

        if mode == "find_lyrics" and payload and payload.strip():
            grounded = await search_lyrics_grounded(
                getattr(self.bot.router, "google_client", None),
                payload.strip(),
            )
            if grounded:
                ident = grounded

        if not ident:
            user_prompt = find_song_prompt(mode, payload)
            if not user_prompt:
                return
            try:
                raw = await self.bot.router._call_llm(
                    system_prompt="你是精準的歌曲識別助手，只輸出一行「藝人 - 歌名」。",
                    user_prompt=user_prompt,
                )
                ident = (raw or "").strip().splitlines()[0].strip() if raw else ""
                if ident.startswith("無"):
                    ident = ""
            except Exception as e:
                logger.debug(f"⚠️ [FindSong] 失敗: {e}")
                return

        if not ident:
            if ch:
                await ch.send(f"🔎 **【找歌】** 找不到符合「{payload}」的歌，換個說法試試？")
            if vc: asyncio.create_task(vc._play_ack("music_fail", speaker=speaker))
            return

        seek_suffix = ""
        if mode == "find_lyrics":
            try:
                lrc = await self._fetch_lyrics_synced({"title": ident})
                if lrc:
                    hit = find_lyrics_timestamp(lrc, payload)
                    if hit:
                        ts_sec, line = hit
                        mm, ss = divmod(int(ts_sec), 60)
                        seek_suffix = f"（「{line}」在 {mm:02d}:{ss:02d}）"
            except Exception as e:
                logger.debug(f"⚠️ [LyricSeek] {e}")

        if ch:
            await ch.send(
                f"🔎 **【找歌】** 我找到的應該是 `{ident}`{seek_suffix}，幫你播了。"
            )
        await self._safe_music_command(speaker, ident, "play")

    @tasks.loop(seconds=90.0)
    async def _stream_watchdog_loop(self):
        """🐕 [Stream Watchdog] 主動偵測『迴圈死了但沒人發現』，不用等使用者手動點歌
        才觸發 _ensure_stream_loop() 的自癒（2026-08-01 事故：迴圈莫名死掉／process
        重啟後迴圈沒重建，佇列裡明明有歌卻安靜了 17-46 分鐘，靠的是使用者剛好開口
        點歌才救回）。

        只在有明確訊號『音樂本該在播』時動手（佇列非空、或 flag 卡在 True 但沒
        task）；完全靜默、沒人點過歌的狀態不會被這裡誤觸發成自動開播——那是
        summon / 手動點歌才該決定的事，不是這個 watchdog 的責任。
        `stop_stream()` 主動停播時會設 `_stream_user_stopped` 抑制本迴圈，避免使用者
        說「停」之後被這裡偷偷復活。
        """
        if self._stream_user_stopped:
            return
        alive = self.stream_task is not None and not self.stream_task.done()
        if alive:
            return
        if not self.stream_queue and not self.stream_mode:
            return  # 沒訊號顯示「本該在播」，不主動開播
        vc = self._vc()
        if vc is None:
            return
        online = self._autopilot_online_members(
            vc.get_online_members() if hasattr(vc, 'get_online_members') else []
        )
        if not online:
            return
        logger.warning(
            f"🐕 [Stream Watchdog] 偵測到串流迴圈死掉但沒人發現"
            f"（flag={self.stream_mode} 佇列={len(self.stream_queue)}首）→ 主動救回"
        )
        self._ensure_stream_loop()

    async def cog_load(self) -> None:
        logger.info("[MusicCog] Phase 5 已載入（stream + radio + autoplay state + slash commands 就緒）")
        self._stream_watchdog_loop.start()

    async def cog_unload(self) -> None:
        self._stream_watchdog_loop.cancel()


async def setup(bot) -> None:
    await bot.add_cog(MusicCog(bot))
