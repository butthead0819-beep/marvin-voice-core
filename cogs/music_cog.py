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
import logging
import os
import random
import subprocess
import time
from typing import Awaitable, Callable, Optional

import yt_dlp

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.music_cog_commands import MusicCommandsMixin
from cogs.music_cog_subsystem import MusicSubsystemMixin
from cogs.music_cog_personal_shuffle import MusicPersonalShuffleMixin
from cogs.music_cog_audio_meta import MusicAudioMetaMixin
from cogs.music_cog_autopilot import MusicAutopilotMixin
from cogs.music_cog_story_arc import MusicStoryArcMixin
from cogs.music_cog_dj_lyrics import MusicDJLyricsMixin
from cogs.music_cog_tail_dj import MusicTailDJMixin
from memory_guard import is_memory_critical
from music_recommender import normalize_title
from music_memory import extract_video_id
from intent_agents.find_song_agent import find_song_prompt
from intent_agents.lyrics_grounded_search import search_lyrics_grounded
from intent_agents.lyrics_seek import find_lyrics_timestamp

logger = logging.getLogger(__name__)

# 開播/preload起跑當下解碼正忙著搶 CPU/網路，響度+BPM量測延後這麼久再起跑，
# 避開開播頭幾秒的資源尖峰（2026-08-25：量測 blocking event loop 疑似造成開頭斷續/加速）。
_NORM_GAIN_MEASURE_DELAY_S = 6.0

# 2026-08-18：YouTube 對這台 Mac 的來源 IP 節流（連續多天 403 Forbidden 攀升，
# 見 incident_youtube_403_ip_throttle_2026-08-17 記憶），實測登入身分的請求能
# 繞過——匿名 ANDROID_VR client（省簽章/n-challenge解密）沒辦法帶 cookies
# （yt-dlp 會直接跳過該 client），只能改用需要簽章解密的一般 client + cookies，
# 靠 remote_components=['ejs:github'] 下載 JS challenge solver（需要本機裝
# deno，`brew install deno`）解出來。
#
# 兩種 cookies 來源，依序嘗試、任何一種失敗就退到下一種（最終退回無 cookies
# 匿名解析，零行為改變）：
#   ① cookiesfrombrowser（優先）：直接讀 Chrome 目前登入的 session，永遠最新，
#      不用手動匯出/不會過期。代價：第一次要使用者手動點過一次 macOS Keychain
#      授權（讀 Chrome Safe Storage 密鑰），已完成；理論上之後授權失效需要
#      再跳一次窗，若那時候是無人值守跑會卡住（run_in_executor 佔用一個
#      thread pool 工作緒），這是接受的已知風險（沒有簡單方法讓 Python
#      thread 帶 timeout 強制中斷），出問題時 log 會清楚顯示哪個 client 失敗
#      方便排查。
#   ② cookiefile：使用者手動從瀏覽器匯出的 cookies.txt，不進 repo/不進
#      .env，放 home 目錄外部；有效期通常數週，過期需重新匯出（見
#      scripts/check_yt_cookies_freshness.py）。
_YT_COOKIES_FROM_BROWSER = os.getenv("MARVIN_YT_COOKIES_FROM_BROWSER", "chrome").strip() or None
_YT_COOKIES_FILE = os.path.expanduser(
    os.getenv("MARVIN_YT_COOKIES_FILE", "~/.config/marvin/youtube_cookies.txt"))

def _get_puck_client():
    """MARVIN_CAR_HARDWARE=esp32_edge_mix 才回傳 client；其餘硬體（pi_bt 車 puck、家用
    Pi 3B 等）回 None。

    2026-08-20：pi_bt（Pi Zero 2W 車 puck）不再有專屬 client——換歌決策/DJ口白改回
    跟家用喇叭共用同一顆 mixer、走 /audio_stream「收音機」模式（見
    main_satellite.py::setup_satellite 的 TeeSpeakerOutput 說明），不需要 Mac 主動
    POST 指令給 Pi 這條 control-plane 了（原本的 marvin_voice_core/puck_mixer_client.py
    已隨之退役）。esp32_edge_mix 車 puck 永遠是它自己撥出連線，Mac 沒辦法主動推指令，
    改寫進本地佇列，ESP32 用既有心跳節奏輪詢 /car_commands 拿指令
    （見 marvin_voice_core/puck_command_queue.py）。"""
    hardware = os.getenv("MARVIN_CAR_HARDWARE", "").strip().lower()
    if hardware != "esp32_edge_mix":
        return None
    from marvin_voice_core.puck_command_queue import PuckCommandQueueClient, get_default_queue
    return PuckCommandQueueClient(get_default_queue())


class MusicCog(MusicCommandsMixin, MusicSubsystemMixin, MusicPersonalShuffleMixin, MusicAudioMetaMixin, MusicAutopilotMixin, MusicStoryArcMixin, MusicDJLyricsMixin, MusicTailDJMixin, commands.Cog):
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

    # dj_topic_selector.select_mode() 的 mode → tts_engine 情緒（見 _EMOTION_ADJUST）：
    # 只調 rate/pitch（edge-tts 沒有真情緒 style 可用）。沒列到的 mode（quick/
    # conversation/reason 等）用預設 "normal"，不特別調。
    _DJ_MODE_TO_TTS_EMOTION = {
        "life": "upbeat",
        "interest": "upbeat",
        "atmosphere": "calm",
        "prev_song": "calm",
        "emotional_highlight": "calm",
        "news": "upbeat",
    }

    def __init__(self, bot):
        self.bot = bot
        # 跨切狀態 — VoiceController 透過 proxy property 讀寫這裡
        self.stream_mode: bool = False
        self.radio_mode: bool = False

        # 🎵 [Phase 2] Stream subsystem state (proxied from VoiceController)
        # Discord 音量壓回 10%；車 puck 音量策略在裝置端另外處理，維持滿幅
        # 讓 puck_mixer 端有完整動態範圍可調（見 596dbea「stream_volume 滿幅」）。
        # 2026-08-26：使用者確認 Discord/車機是各自獨立的兩個 process，各自預設值
        # 不同、音量/TTS 各自連動即可，不需要統一成同一個數字。
        self._default_stream_volume: float = (
            1.0 if os.getenv("MARVIN_CAR_MODE", "").strip().lower() in ("1", "true", "yes", "on")
            else 0.10
        )
        self.stream_volume: float = self._default_stream_volume
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
        self._current_stream_explanation: Optional[str] = None  # 🎯 推薦解釋（槽位填空，見 explanation_slotfill.py）
        self._current_stream_start_time: Optional[float] = None  # HUD 進度條用
        self._active_control_view = None
        self._active_lyrics_message = None  # 歌詞獨立訊息（換歌刪舊貼新，不留存舊歌歌詞）

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
        # 🎲 [T2 SeedCache] 同一 seed 的 radio 原始結果快取（TTL 內免重打 ytmusicapi）：
        # seed 輪替常見同一顆種子連續多輪被選中（見 seed_rotation.py 的 round-robin），
        # radio 推薦短期內不太會變，快取原始 50 首、exclude_titles 每次本地重套即可。
        self._t2_seed_cache: dict[str, tuple[float, list[dict]]] = {}
        self._T2_SEED_CACHE_TTL_S = 3600
        # 🎚️ [ThemedSet] 讀空氣主題歌單（env-gated MARVIN_THEMED_PLAYLIST，預設 OFF）
        self._THEMED_SET_COOLDOWN_S = 30 * 60   # 一張歌單約 30-40 分鐘，半小時內不重開
        self._THEMED_SET_NIGHTLY_CAP = 4        # 每晚上限，防抖動重打付費 LLM
        self._last_themed_set_ts: float = 0.0
        self._themed_sets_tonight: int = 0
        self._themed_set_date = None
        # 🎭 [DJ Joke Interlude] 頻道安靜（非熱烈聊天）時，crossfade 串場偶爾換成馬文式
        # 厭世冷笑話（跟 /marvin_joke 共用風格範例庫，見 joke_examples.py）。冷卻起點設
        # 在啟動當下（不是 0），避免剛開機/剛連上就先講一則——也讓每個測試用的新 cog
        # 實例預設冷卻中，不會意外把既有 DJ 串場測試岔到笑話分支。
        self._DJ_JOKE_COOLDOWN_S = 30 * 60
        self._last_dj_joke_ts: float = time.time()
        # 📖 [StoryArc] 故事弧線節目（dj_story_arc.py）進行中旗標——自成一體播放協程，
        # 不碰 stream_queue/_stream_loop/_run_tail_dj，跟一般 autopilot 互斥（見 story_arc 指令）。
        self._story_arc_active: bool = False
        self._STORY_ARC_BGM_VOLUME: float = 0.05  # 口白約10%感覺時，BGM抓一半5%，別蓋過口白
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
        # 🧹 [clear_queue] 「這首播完就停」延遲旗標——見 _stream_loop() 開頭 reset
        # + 迴圈頂端消費、_queue_user_song() 入隊時取消（2026-09-11 PR2）。
        self._pending_stop_after_song: bool = False

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

    # ── 🎵 Stream loop & playback ────────────────────────────────────────────

    async def _stream_loop(self):
        """🎵 依序播放佇列中的歌曲。"""
        logger.info("🎵 [Stream Loop] 串流迴圈啟動。")
        # 🧹 [clear_queue] 單一 reset 點：不管這個 coroutine 是被 _ensure_stream_loop
        # 或 personal_shuffle 路徑（直接 create_task）叫起，任何跨 session 殘留的
        # True 都在這裡歸位，不散落在多個啟動點各補一次（2026-09-11 PR2，
        # plan-eng-review outside voice round2）。
        self._pending_stop_after_song = False
        _stopped_via_pending = False
        try:
            while self.stream_mode:
                if self._pending_stop_after_song:
                    self._pending_stop_after_song = False
                    _stopped_via_pending = True
                    break
                if not self.stream_queue:
                    keep_going = await self._stream_loop_topup()
                    if not keep_going:
                        break
                    continue

                vc = self._vc()
                info = self.stream_queue.pop(0)
                self._current_stream_info = info
                self._current_stream_start_time = None
                # 🎯 推薦解釋在 _auto_recommend 已算好存進 info（record_play 之前，見
                # _compute_recommend_explanation docstring），這裡直接讀、不用等 meta——
                # 要在 _publish_now_playing_state 之前設好，第一次發布就帶對的值。
                self._current_stream_explanation = info.get('_explanation')
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

                dj_audio, _dj_played_in_tail = await self._stream_loop_prepare_and_announce(
                    info, vc, title, requested_by)

                self._stream_loop_fire_puck(info, _dj_played_in_tail)

                self._current_song_skipped = False
                song_start_time = time.time()
                self._current_stream_start_time = song_start_time
                self._republish_queue_snapshot()   # HUD 進度條要靠這次補推的 song_start_time
                song_lyrics_snapshot = self._current_lyrics or ""
                playback_completion = "natural"

                playback_started = self._stream_loop_schedule_tail_dj(info, vc, title)

                try:
                    await self.play_stream_song(
                        info['url'], title, dj_audio_path=dj_audio,
                        highlight_start_s=info.get('highlight_start_s'),
                        started_future=playback_started,
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

                await self._stream_loop_retry_if_dropped(info, song_start_time, requested_by, dj_audio)

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
            # 🧹 [clear_queue] pending-stop 觸發的收尾不重複發「佇列已空」——
            # clear_queue handler 自己的 ack 已經講過「這首放完就停」了。
            if active_ch and not _stopped_via_pending:
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

    # ── _stream_loop 拆解出的 helper method（Extract Method，2026-09-11，見
    # plan-eng-review 設計檔 jackhuang-main-design-stream-loop-extract-20260911.md）──

    async def _stream_loop_topup(self) -> bool:
        """佇列空時的補位邏輯。回 True＝迴圈頂端重查（可能已補到歌或該再等）；
        回 False＝該 break（三層 autopilot 都補不到 + last_resort 也失敗）。
        個人歌單補到歌 / busy-spin 防護 sleep / auto_recommend 成功，三種情況目前
        都回 True（呼叫端只做 continue，無害）——未來要分別記 log/metrics 才需要
        細分回傳型別。"""
        # 🎲 個人歌單連續播：佇列空先墊他下一首（一次一首）；池空才回退一般推薦
        if self._personal_shuffle is not None:
            await self._personal_shuffle_topup()
            if self.stream_queue:
                return True                      # 墊到歌了 → 去播
            if self._personal_shuffle is not None:
                # ⚠️ 死鎖防護：topup 沒實際入隊（in-flight 的 create_task 還在慢
                # resolve）→ 必須 await sleep 讓出 loop，否則 `while 佇列空: await
                # topup()→inflight 立刻 return True` 會 busy-spin 凍結 event loop、
                # in-flight topup 也永遠跑不完（2026-06-29 心跳阻塞 9 分鐘事故）。
                await asyncio.sleep(0.5)
                return True
            # else：池空、session 已清 → 落下面一般推薦
        vc = self._vc()
        _rb = (self._current_stream_info or {}).get('requested_by')
        online = self._autopilot_online_members(vc.get_online_members() if vc is not None else [])
        _seed = self._autorecommend_seed(_rb, online)
        if _seed:
            await self._auto_recommend(_seed)
        # 三層 autopilot 補不到 → 最終安全網：從歷史回收重播，永不靜默停
        if not self.stream_queue and await self._last_resort_replay():
            return True
        if not self.stream_queue:
            return False
        return True

    def _stream_loop_fire_puck(self, info: dict, dj_played_in_tail: bool) -> None:
        """[PuckMixer] esp32_edge_mix 專用：沒經過 _fire_puck_crossfade 接手的歌
        （開場第一首、skip、或上一首沒排到尾段 task）要送硬 play 讓裝置端從乾淨
        狀態開始播——跟 _fire_puck_crossfade 對稱，那邊只在尾段轉場時接手 standby
        deck，不會有人叫它 play。見 _play_open()/_run_tail_dj() 前的說明。

        2026-08-20：pi_bt（車 puck Pi Zero 2W）不再走這條——換歌決策/DJ口白
        改回跟家用喇叭共用同一顆 mixer（見 main_satellite.py::setup_satellite
        的 TeeSpeakerOutput + /audio_stream「收音機」模式說明），_get_puck_client()
        對 pi_bt 回 None，下面這段自然被跳過。"""
        if dj_played_in_tail:
            return
        puck_client = _get_puck_client()
        puck_url = info.get('webpage_url', '')
        if puck_client is not None and puck_url:
            asyncio.create_task(
                self._fire_puck_play(
                    puck_client, puck_url, title=info.get('title'),
                    highlight_start_s=info.get('highlight_start_s'),
                    duration=info.get('duration'))
            )

    def _stream_loop_schedule_tail_dj(self, info: dict, vc, title: str) -> "asyncio.Future | None":
        """[DJ Tail] 在播 N 期間排尾段 task：只要 duration 已知就排，下一首在點火
        當下才抓 stream_queue[0]（autopilot 常播放中才排下一首，開播時綁定會抓空）。
        回傳 playback_started future（vc is None 時回 None，原邏輯不變）——
        song_start_time 是「決定要播」那刻蓋的，離「真的出聲」還隔著 highlight_start_s
        的網路 seek + 整首解碼，拿它當基準會讓尾段提早點火（見 project_dj_tail_seek_latency）
        ——改傳 playback_started future，_run_tail_dj 改等 _mixer_play_music 真出聲才起算。"""
        playback_started: "asyncio.Future | None" = None
        if vc is not None:
            playback_started = asyncio.get_event_loop().create_future()
        if vc is not None and info.get('duration'):
            self._tail_dj_task = asyncio.create_task(
                self._run_tail_dj(info, playback_started)
            )
            def _clear_tail_task(t, _self=self):
                if _self._tail_dj_task is t:
                    _self._tail_dj_task = None
            self._tail_dj_task.add_done_callback(_clear_tail_task)
            logger.info(f"[DJ Tail] 已排尾段 task：{title}（點火時抓下一首）")
        return playback_started

    async def _stream_loop_retry_if_dropped(
        self, info: dict, song_start_time: float, requested_by: str, dj_audio: str | None,
    ) -> None:
        """🔁 點的歌只播了一瞬（疑 yt-dlp 網址過期→ffmpeg 403）→ 重抓網址重試一次，
        別讓它被自動推薦洗掉。DJ 報歌走 mixed（隨 ffmpeg 一起失敗）→ 首次 403 不誤報，
        只在確定能播的那次才響＝「確定能播的歌才說出來」。

        刻意比正常播放路徑薄——不重排 tail-dj / 不重發 playback_started / 不重貼卡 /
        不重 fire puck，這是既有的不對稱行為，不要在這裡「補全」。"""
        title = info['title']
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

    async def _stream_loop_prepare_and_announce(
        self, info: dict, vc, title: str, requested_by: str,
    ) -> "tuple[str | None, bool]":
        """歌開播前的準備與公告：解析預取 meta（Play-First，未就緒不阻塞出聲）、
        貼歌曲卡、預取下一首、觸發 autopilot/個人歌單背景補位、DJ 口白一致性守門、
        開頭 DJ 插話。回 (dj_audio, dj_played_in_tail)：dj_data 本身只在這段內部
        消費（一致性守門判斷用），呼叫端沒人再讀，故不回傳。

        內部四步順序固定：consistency guard → dj_played_in_tail guard →
        splice_owner_voice_clip → maybe_play_dj_interjection，不可調換（調換會讓
        警告 log 消失或 DJ 插話誤發）。

        有跨迭代副作用：讀/寫 self._prefetch_cache（pop 掉本首的、寫入下一首的），
        不是純函式——下一首的 prefetch 要留到下一輪迭代才會被讀到。"""
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
        # 🛡️ [Consistency Guard] 檢查退回開頭播放的 DJ 口白是否提及了錯誤的上一首
        if dj_data and dj_data.get('prev_title_used'):
            real_prev = self.stream_history[-2].get('title', '') if len(self.stream_history) >= 2 else ''
            if real_prev:
                from song_name_clean import clean_title_regex
                norm_used = clean_title_regex(dj_data['prev_title_used']).strip().lower()
                norm_real = clean_title_regex(real_prev).strip().lower()
                if norm_used and norm_real and norm_used != norm_real:
                    logger.warning(
                        f"🛡️ [Stream Loop Consistency Guard] 預期上一首《{dj_data['prev_title_used']}》與實際《{real_prev}》不符，捨棄過期口白"
                    )
                    dj_data = None
                    dj_audio = None
        # [DJ Tail] 尾段派發成功（上一首 _run_tail_dj 播完並標記）→ 本首開頭不重播
        dj_played_in_tail = bool(info.get('_dj_played_in_tail'))
        if dj_played_in_tail:
            logger.info(f"[DJ Tail] {title} DJ 已在上一首尾段播出，跳過開頭重播")
            dj_audio = None
            dj_data = None
        if dj_audio:
            dj_audio = await self._splice_owner_voice_clip(dj_audio, info)
        if dj_data and not dj_audio and vc is not None:
            await self._maybe_play_dj_interjection(dj_data)

        return dj_audio, dj_played_in_tail



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
                                highlight_start_s: float | None = None,
                                started_future: "asyncio.Future | None" = None,
                                still_active=None):
        """🎵 播放單首串流音樂，等待播放完成後 return。

        highlight_start_s：YouTube「最多人重播」熱力圖挑出的精華起點（見
        youtube_heatmap.pick_highlight_start），有給就從這秒開始播（-ss），
        不影響 DJ 混音模式（use_mix，另一條較少走的路徑，保持舊行為）。

        started_future：真正出聲（_mixer_play_music 的 set_music_source）那一刻才
        set_result(time.time())，給 _run_tail_dj 當「已播秒數」的基準——highlight_start_s
        的網路 seek + 整首解碼都花時間，用「call 這個函式前」蓋的時間戳會系統性偏早，
        見 project_dj_tail_seek_latency。無下一首派發需求的呼叫端可不傳。

        still_active：`_mixer_play_music` 用來判斷「還要不要繼續播」的 callable，預設
        `None` → 退回 `lambda: self.stream_mode`（一般 autopilot/radio 的既有行為，不變）。
        `_play_story_arc` 這種自成一體、刻意不設 `stream_mode=True` 的呼叫端要傳自己的
        判斷（例如 `lambda: self._story_arc_active`）——否則 `still_active()` 一開始就是
        False，`_mixer_play_music` 的 while 迴圈第一輪就判定「該停了」，歌完全沒真的
        播出來就被 `clear_music()` 收掉（2026-08-17 story arc 第一次真機測試踩到）。
        """
        import shlex

        if still_active is None:
            still_active = lambda: self.stream_mode  # noqa: E731

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
                    still_active=still_active,
                    started_at=started_future,
                )
        else:
            p12_opts = {
                'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -probesize 32M',
                'options': '-vn -bufsize 512k',
            }
            if highlight_start_s:
                p12_opts['before_options'] = f'-ss {highlight_start_s:.2f} ' + p12_opts['before_options']
            if url not in self._stream_norm_gain and vc is not None:
                asyncio.create_task(self._measure_norm_gain_bg(
                    url,
                    duration=float((self._current_stream_info or {}).get("duration") or 0),
                    highlight_start_s=highlight_start_s,
                    info=self._current_stream_info,
                    delay_s=_NORM_GAIN_MEASURE_DELAY_S,
                ))
            if vc is not None:
                # DJ Tail 點火時已背景 preload（見 _start_music_preload）→ 有就直接用、
                # 零等待；沒有（沒走過尾段轉場，如第一首/被 skip）就退回現場建 ffmpeg 音源。
                preloaded, fresh = await self._resolve_music_source(
                    url, lambda: discord.FFmpegPCMAudio(url, **p12_opts))
                await vc._mixer_play_music(
                    device, fresh,
                    still_active=still_active, volume_attr="stream_volume",
                    preloaded=preloaded, started_at=started_future,
                )

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

    def _user_song_insert_index(self, queue: list[dict]) -> int:
        """使用者自選曲的插入位置：排在所有既有使用者曲之後、第一首 Marvin 自動曲之前。

        爆音修（2026-08-27）：autopilot 播放中，點歌不插隊到「下一首」。歌尾時
        queue[0] 常已被 DJ tail 點火預載進 _preload_music_cache；使用者曲插它前面
        → 換源 preload cache miss → 歌尾邊界冷解碼（yt-dlp + ffmpeg + loudnorm/BPM）
        撞 DJ crossfade，CPU/executor 爆量 → mixer underrun → 大量爆音。往後推一首：
        已排定的那首先播、點歌自然變第三首，並多拿一整首歌的時間在背景把自己預載好。
        """
        def _is_marvin(item) -> bool:
            return str((item or {}).get('requested_by') or '').startswith('Marvin')

        if self.stream_mode and queue:
            # slot 0（下一首）神聖：歌尾常已被 DJ tail 預載，插它前面 → 爆音。
            # 從 slot 1 起找「使用者曲連續段」的尾巴，新點歌接在那之後。
            idx = 1
            while idx < len(queue) and not _is_marvin(queue[idx]):
                idx += 1
            return idx
        # 非 autopilot：舊行為——插在第一首 Marvin 自動曲之前。
        for i, item in enumerate(queue):
            if _is_marvin(item):
                return i
        return len(queue)

    def _play_next_insert_index(self, queue: list[dict]) -> int:
        """play_next 專用插入位置：蓋過所有既有排隊（含其他人已經 play_next 插進去
        的歌），比 `_user_song_insert_index`（只排在既有使用者曲「之後」）更激進。

        仍尊重同一條爆音教訓（slot 0 神聖，見 `_user_song_insert_index` 2026-08-27
        docstring）：stream_mode 中 queue 非空時，queue[0] 常已被 DJ tail 預載，
        插它前面會爆音，所以最前只到 index 1。

        FIFO among cut-ins：用 `info['_play_next']` 標記，從 index 1 起掃過已經是
        play_next 插入的連續段才落地——避免連續多次 play_next 變成 LIFO（先插播的
        反而排最後），2026-09-11 plan-eng-review outside voice round2 #4 抓到的問題。
        """
        if not (self.stream_mode and queue):
            return 0
        idx = 1
        while idx < len(queue) and queue[idx].get('_play_next'):
            idx += 1
        return idx

    def _queue_user_song(self, info: dict, *, front: bool = False) -> None:
        """使用者自選曲入隊——一般點歌（FIFO，插在既有使用者曲之後）跟 play_next
        （`front=True`，蓋過既有排隊）共用同一個函式：dedup/ledger/tail bookkeeping/
        pending-stop 取消全部同一份程式碼，不會兩條路徑各自維護、彼此漂移
        （2026-09-11 PR2，plan-eng-review outside voice round2 #3/#5：play_next
        若走獨立插入路徑會繞過這裡的 30s 同人同曲去重跟 watchdog 抑制解除）。

        skip-override：手動點播蓋過先前 skip——記 played_again + 重置 consecutive-skip 計數。
        """
        # 🎙️ [使用者自選曲] 不快進：略過熱力圖精華起點與後續 LRC 前奏跳過，一律從頭播。
        info['highlight_start_s'] = None
        info['voice_request'] = True
        if front:
            info['_play_next'] = True

        # 🎵 [ReqDedup] 同人同曲 30s 去重：佇列去重只看佇列（第一發已 pop 去播時
        # 佇列空、第二發漏過，7/3-4 實錘）；ledger 與佇列狀態無關（唯一入隊點）
        _vid = extract_video_id(info.get('webpage_url') or '')
        _spk = info.get('requested_by') or ''
        if _vid:
            if self._req_ledger.is_dup(_spk, _vid, time.time()):
                logger.info(f"🎵 [ReqDedup] {_spk} 30s 內重複點 {_vid}，跳過入隊（誤觸/殘餘）")
                return
            self._req_ledger.mark(_spk, _vid, time.time())
        insert_idx = (self._play_next_insert_index(self.stream_queue) if front
                      else self._user_song_insert_index(self.stream_queue))
        self.stream_queue.insert(insert_idx, info)
        # 🧹 [clear_queue] 有人主動點歌了（不管一般 play 或 play_next）＝要它繼續，
        # 取消任何待生效的「播完就停」，並解除 watchdog 抑制（兩個旗標必須一起清，
        # 只清前者會讓 watchdog 在下次 loop 真的掛掉時永久拒絕自癒，2026-08-01
        # 事故重演——outside voice round2 #1 抓到的）。
        self._pending_stop_after_song = False
        self._stream_user_stopped = False
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
        # cookies 來源優先序：browser（永遠最新，見上方常數說明）→ file（使用者
        # 手動匯出，會過期）→ 無 cookies。單一選定，不做「這個來源丟例外就換下一個」
        # 的執行期重試——那樣會跟下面 _extract_with_retry 既有的 OSError errno=11
        # 專屬重試邏輯混在一起，讓任何跟 cookies 完全無關的例外也被重試多次
        # （2026-08-18 實測踩到：既有測試鎖住「非 errno=11 的 OSError 只該試一次」，
        # 加了 cookies 來源 cascade 後這個測試變成試 3 次才失敗，屬於不該有的行為
        # 改變）。cookies 來源本身壞掉時（例如 Keychain 授權失效）就讓例外照舊
        # 往上冒，走既有的錯誤處理/重抓路徑，不在這裡疊一層新的重試語意。
        if _YT_COOKIES_FROM_BROWSER:
            ydl_opts['cookiesfrombrowser'] = (_YT_COOKIES_FROM_BROWSER,)
            ydl_opts['remote_components'] = ['ejs:github']
        elif os.path.exists(_YT_COOKIES_FILE):
            ydl_opts['cookiefile'] = _YT_COOKIES_FILE
            ydl_opts['remote_components'] = ['ejs:github']
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
        res = await self._apply_itunes_cover(res, _orig_text_query)
        res = await self._apply_spotify_metadata(res, _orig_text_query)
        return _cache_put(res)

    async def _apply_itunes_cover(self, res, orig_query: str = None):
        """用 iTunes 方形專輯封面取代 YT 縮圖 + 補 artist/album（失敗/低信心一律
        只退回原縮圖，不補 artist/album）。單次 iTunes 查詢同時拿三者
        （itunes_cover.resolve_metadata()），不是各打一次 API。

        單一改點：res['thumbnail'] 是全站封面唯一源頭（音樂卡 PIL、embed、/now 顯示端），
        在此換掉即全部沿用；且解析在進快取前完成，ResolveCache 免費快取不重打 iTunes。
        res['artist']/res['album'] 則是 /car_now → AVRCP 車機顯示的來源
        （main_satellite.py::handle_car_now，沒配到就沿用 yt-dlp 的 uploader/空字串）。

        orig_query（使用者原始點歌文字，例如「周杰倫 晴天」）比 YT 解析完的標題乾淨
        （後者夾雜頻道名/Official MV/emoji 等上傳者自由格式雜訊），優先拿它去查；
        沒有時（例如直接點 URL 進來，沒有對應文字查詢）才退回舊路徑用 YT 標題+uploader。
        """
        if not res:
            return res
        try:
            import itunes_cover
            yt = res.get('thumbnail')
            if orig_query:
                meta = await itunes_cover.resolve_metadata(orig_query)
            else:
                meta = await itunes_cover.resolve_metadata(res.get('title', ''), res.get('uploader'))
            art = (meta or {}).get('cover') or yt
            if art and art != yt:
                res['yt_thumbnail'] = yt
                res['thumbnail'] = art
                logger.info(f"🎨 [Cover] iTunes 封面取代 YT 縮圖：{(res.get('title') or '?')[:30]}")
            if meta:
                if meta.get('artist'):
                    res['artist'] = meta['artist']
                if meta.get('album'):
                    res['album'] = meta['album']
        except Exception as e:
            logger.warning(f"⚠️ [Cover] iTunes 解析失敗，用原縮圖：{type(e).__name__}: {e}")
        # 從最終封面抽主色調色盤（給 vinyl splatter 用；失敗 → [] 不影響封面）
        try:
            import cover_palette
            res['palette'] = await cover_palette.extract_palette(res.get('thumbnail'), n=4)
        except Exception as e:
            logger.warning(f"⚠️ [Cover] 抽色失敗：{type(e).__name__}: {e}")
        return res

    async def _apply_spotify_metadata(self, res, orig_query: str = None):
        """新歌一入庫就該是乾淨的：查 Spotify 拿官方 track/artist/album/uri，寫進
        res['spotify_title'/'spotify_artist'/'spotify_album'/'spotify_uri']（新增
        欄位，不覆蓋既有 res['artist']/res['album']——那兩個是 iTunes 補的、給
        AVRCP 車機顯示用，語意不同）。record_play() 建新歌條目時原樣抄進
        music_memory，取代事後跑 scripts/spotify_clean_music_memory.py 批次清洗
        存量的做法（見 [[project_spotify_connect_personal_dj_design]]）。

        失敗/查不到/關 flag（MARVIN_SPOTIFY_METADATA=0）一律不寫欄位，絕不擋播放。
        """
        if not res:
            return res
        try:
            import spotify_metadata
            if orig_query:
                meta = await spotify_metadata.resolve_metadata(orig_query)
            else:
                meta = await spotify_metadata.resolve_metadata(res.get('title', ''), res.get('uploader'))
            if meta:
                res['spotify_title'] = meta.get('title')
                res['spotify_artist'] = meta.get('artist')
                res['spotify_album'] = meta.get('album')
                res['spotify_uri'] = meta.get('uri')
        except Exception as e:
            logger.warning(f"⚠️ [Spotify] metadata 解析失敗：{type(e).__name__}: {e}")
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

    async def _resolve_and_prepare(
        self, speaker: str, query: str, vc, *,
        on_query_resolved: Callable[[str, str, str], Awaitable[None]] | None = None,
    ) -> tuple[dict | None, str, str, str]:
        """點歌解析：抽搜尋字串 → STT 修正 → `_resolve_yt_query` → 組 info dict。
        `cmd=="play"` / `cmd=="play_next"` / audio-rescue 三路共用（2026-09-11
        PR2，plan-eng-review outside voice #9）。

        `vc` 由呼叫端傳入（`_handle_voice_music_command` 已經查過一次），不在這裡
        重新 `self._vc()`——同一次呼叫內查兩次可能不一致，是今天 `_stream_loop`
        拆解時踩過的同一類坑（見 memory extract_method_vc_snapshot_not_reparam）。

        不硬塞 Discord I/O 進來——`play` 分支既有的「🔍 正在搜尋」狀態訊息要在
        STT 修正完、`_resolve_yt_query` 網路呼叫**開始前**顯示（不然使用者在等待
        期間看不到任何回饋），純函式黑盒子做不到這件事，所以留一個可選 callback
        `on_query_resolved(raw_search, corrected_search, correction_note)`，
        resolve 前呼叫一次；`play_next`/audio-rescue 不需要這個中途回饋，傳 None。

        回傳 `(info | None, raw_search, corrected_search, correction_note)`。
        `search` 抽不出來（使用者沒講歌名）或 `_resolve_yt_query` 找不到 → info
        為 None，caller 自行決定怎麼 ack 失敗（兩種失敗用 `raw_search` 是否為空
        分辨）。
        """
        search = vc._extract_music_search_query(query) if vc else query
        if not search:
            return None, "", "", ""

        raw_search = search
        correction_note = ""
        wrong = None
        if hasattr(self.bot, 'music_memory') and self.bot.music_memory:
            corrected, wrong = self.bot.music_memory.apply_stt_correction(speaker, search)
            if wrong:
                search = corrected
                correction_note = f" *(語音修正：{wrong} → {corrected})*"

        if on_query_resolved is not None:
            await on_query_resolved(raw_search, search, correction_note)

        info = await self._resolve_yt_query(search)
        if not info:
            return None, raw_search, search, correction_note
        info['requested_by'] = speaker
        return info, raw_search, search, correction_note

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
            if not _can_play:
                if ch: await ch.send("❌ 我不在語音頻道中，先用 `/summon` 召喚我。")
                return

            status_msg = None

            async def _show_searching(raw: str, corrected: str, note: str) -> None:
                nonlocal status_msg
                if ch:
                    status_msg = await ch.send(f"🔍 **正在搜尋：** `{corrected}`...{note}")

            info, raw_search, search, correction_note = await self._resolve_and_prepare(
                speaker, query, vc, on_query_resolved=_show_searching)
            wrong = bool(correction_note)

            if not raw_search:
                if ch: await ch.send("🎵 要放什麼歌？你說了等於沒說。")
                return
            self._last_search[speaker] = {'query': raw_search, 'ts': time.time(), 'source': 'voice'}
            if info is None:
                if status_msg: await status_msg.edit(content=f"❌ 找不到 `{search}`，就跟意義一樣——不存在。")
                if vc: asyncio.create_task(vc._play_ack("music_fail", speaker=speaker))
                return
            # 🎙️ [語音點歌] 不快進：略過熱力圖精華起點與後續 LRC 前奏跳過，一律從頭播。
            info['highlight_start_s'] = None
            info['voice_request'] = True
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
            # 🎙️ [第三個Ack] 點播成功：唸出點了什麼歌，跟「收到」「找不到」兩個 Ack 分開，
            # 讓使用者確認聽到的字沒被 STT/搜尋誤解成別首歌。走跟 _play_ack 同款「直推 TTS
            # 層」路徑（play_dj_on_tts_layer），繞開 play_tts 的 Silence Gate/Interrupt Guard——
            # 這兩個 gate 是為長回應設計的，聊天室常有人持續講話，會把這句短報幾乎全擋掉。
            if vc:
                asyncio.create_task(self._speak_song_ack(vc, info['title']))
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

        elif cmd == "play_next":
            if not _can_play:
                if ch: await ch.send("❌ 我不在語音頻道中，先用 `/summon` 召喚我。")
                return
            info, raw_search, search, _note = await self._resolve_and_prepare(speaker, query, vc)
            if not raw_search:
                if ch: await ch.send("🎵 要插播什麼歌？你說了等於沒說。")
                return
            if info is None:
                if ch: await ch.send(f"❌ 找不到 `{search}`，插播失敗。")
                if vc: asyncio.create_task(vc._play_ack("music_fail", speaker=speaker))
                return
            info['highlight_start_s'] = None
            info['voice_request'] = True
            if vc:
                vc.stt_logger.info(
                    f"[插播-語音] 使用者={speaker} | 搜尋={raw_search} | 結果={info['title']}"
                )
            if self.radio_mode:
                await self.stop_radio(reason="語音音樂指令接管")
            self._queue_user_song(info, front=True)
            if vc:
                asyncio.create_task(self._speak_song_ack(vc, info['title']))
            self._ensure_stream_loop()
            if ch: await ch.send(f"⏭️ 「{info['title']}」插播到最前面了。")

        elif cmd == "clear_queue":
            if not self.stream_queue and not self.stream_mode:
                if ch: await ch.send("😑 待播本來就是空的。")
                return
            had_upcoming = bool(self.stream_queue)
            self.stream_queue.clear()
            # 🎲 併發清理：正在播的個人歌單 session 一併收掉，避免 loop 之後莫名
            # 復活繼續播（比照 stop_stream 既有行為）。
            self._personal_shuffle = None
            # [DJ Tail] 尾段 task 若已排、還沒點火 → 取消（保險；_run_tail_dj 自己
            # 點火時也會重查 stream_queue[0] 空而 no-op，這裡只是不留殘留 task）。
            if self._tail_dj_task is not None and not self._tail_dj_task.done():
                self._tail_dj_task.cancel()
                self._tail_dj_task = None
            self._republish_queue_snapshot()
            if self.stream_mode:
                self._pending_stop_after_song = True
                self._stream_user_stopped = True  # 主動清空 → watchdog 別自己復活
                reply = "🧹 好，待播清空了，這首放完就停。"
            else:
                reply = "🧹 待播清空了。" if had_upcoming else "😑 待播本來就是空的。"
            if ch: await ch.send(reply)
            if vc: vc.stt_logger.info(f"[音樂控制→{speaker}] 指令=clear_queue | bot={reply}")

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
