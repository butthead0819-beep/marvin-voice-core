import discord
from discord.ext import commands, tasks, voice_recv
from discord import app_commands
import asyncio
import os
import json
import re
import time
import datetime
import random
import logging
import sys
import tempfile
import shutil
import subprocess
import weakref
import numpy as np
from utils import pre_filter_speech, is_whisper_hallucination, WAKE_PATTERN
from utils import WAKE_WORDS_LIST as _WAKE_WORDS_LIST, FAST_ONLY_WAKE_WORDS as _FAST_ONLY_WAKE_WORDS
from departure_stats import DepartureStats
from consent_manager import ConsentManager
from nudge_throttle import NudgeThrottle
from transcript_store import TranscriptStore
from speaker_topic_graph import SpeakerTopicGraph
from speak_bus import SpeakBus, SpeakContext
from speak_outcome import SpeakOutcome, append_speak_outcome
from ducking_agent import DuckingAgent
from room_mood_state import RoomMoodStateStore
from mood_agent import MoodAgent
from bridge_agent import BridgeAgent
from intent_agents.memory_callback_agent import MemoryCallbackAgent
from proactive_topic_agent import ProactiveTopicAgent
from intent_agents.spontaneous_manzai_agent import SpontaneousManzaiAgent
from cogs.voice_controller_commands import MarvinCommandsMixin
from cogs.voice_controller_social import ProactiveSocialMixin, PROACTIVE_TOPIC_COOLDOWN_S
from cogs.voice_controller_emotion import EmotionMoodMixin
from cogs.voice_controller_connection import (  # noqa: F401 — re-export 給 main_discord / 測試
    ConnectionMixin, read_and_clear_reboot_state, REBOOT_STATE_FILE,
)
from cogs.voice_controller_playback import (  # noqa: F401 — re-export MAX_HOTSWAP_CHARS 給測試
    PlaybackMixin, MAX_HOTSWAP_CHARS,
)
from cogs.voice_controller_system_loops import SystemLoopsMixin
from cogs.voice_controller_state_proxy import StateProxyMixin
from cogs.voice_controller_music_proxy import MusicProxyMixin
from vector_store import VectorStore
from memory_guard import is_memory_critical

# Eager-import yt_dlp + YouTube extractor at module load so runtime music
# commands don't trigger lazy importlib reads — which can hit macOS EDEADLK
# under memory pressure (5/18 22:05 incident: yt_dlp.extractor.lazy_extractors
# → import yt_dlp.extractor.youtube._clip → OSError(11) on read).
import yt_dlp  # noqa: E402
import yt_dlp.extractor.youtube  # noqa: E402,F401  — pre-warm lazy extractor
from recall_handler import (
    RecallHandler, is_recall_query, is_mark_done_query,
    is_manual_add_query, is_task_update_query, is_personal_assistant_query,
)
from summary_store import SummaryStore
from task_store import TaskStore
from session_summarizer import SessionSummarizer, commitment_to_callback
from callback_delivery import is_join_callback_enabled, format_callback_line
from gemini_router import QuotaExhaustedError  # noqa: F401 — re-exported for callers
from impression_engine import detect_imitation_target, get_speech_dna, build_imitation_system_prompt
from latency_tracker import LatencyMarks
from quality_metrics import record_metric
from intent_agents.constants import (
    MUSIC_DIRECT_PAUSE_KW as _MUSIC_DIRECT_PAUSE_KW_SRC,
    MUSIC_DIRECT_RESUME_KW as _MUSIC_DIRECT_RESUME_KW_SRC,
    MUSIC_DIRECT_SKIP_KW as _MUSIC_DIRECT_SKIP_KW_SRC,
    MUSIC_DIRECT_STOP_KW as _MUSIC_DIRECT_STOP_KW_SRC,
    MUSIC_PAUSE_KW as _MUSIC_PAUSE_KW_SRC,
    MUSIC_PLAY_KW as _MUSIC_PLAY_KW_SRC,
    MUSIC_RESUME_KW as _MUSIC_RESUME_KW_SRC,
    MUSIC_SKIP_KW as _MUSIC_SKIP_KW_SRC,
    MUSIC_STOP_KW as _MUSIC_STOP_KW_SRC,
    STRONG_PLAY_KW as _STRONG_PLAY_KW_SRC,
    WEAK_PLAY_KW as _WEAK_PLAY_KW_SRC,
)
from command_fastpath import match_command_action, normalize_command
from intent_bus import IntentBus, IntentContext
from intent_gap import GapLogger, handle_intent_gap, make_groq_gap_classifier
from gap_research import (
    UncertaintyDetector,
    append_record as gap_append_record,
    build_record as gap_build_record,
    current_mode as gap_research_mode,
    should_escalate as gap_should_escalate,
)
from intent_agents.rescue_classifier import build_rescue_components
from audio_position_source import PositionTrackingAudioSource
from voice_guard_helpers import _should_mute_for_stream_guard
from cogs.voice_views import ConsentView
from local_mixing_source import (
    LocalMixingAudioSource, MixerPlaybackAdapter, S16ToF32MusicSource,
    BufferedF32MusicSource, ensure_mixer_playing, FRAME_BYTES_F32,
)
from utterance_budget import STREAM_BUDGET
import audio_mixing
import ack_templates
import pipeline_timing
from wake_intent_gate import has_intent_signal
from wake_followup import match_followup, is_expired as _followup_is_expired
from helper_wake import is_helper_wake, helper_speak_plan
from manzai_interject import compute_interject_ratio, interject_diagnostics
from intent_agents.hallucination_guard_agent import HallucinationGuardAgent
from intent_agents.music_agent_v2 import MusicAgentV2
from intent_agents.nemoclaw_agent import NemoClawAgent
from intent_agents.busted_agent import BustedAgent
from intent_agents.busted99_agent import Busted99Agent
from intent_agents.turtle_soup_agent import TurtleSoupAgent
from intent_agents.find_song_agent import FindSongAgent
from intent_agents.game_knowledge_agent import GameKnowledgeAgent
from intent_agents.skip_intent import is_short_skip_command
# Phase 1 M5: PlaybackControlAgent 改成 build_intent_agents() 內 lazy import
# 避免 macOS python 環境冷啟動時的 import 鏈死結 (2026-05-23 incident)
from intent_agents.semantic_resolver import SemanticResolver
from intent_agents.profile_builder import SpeakerProfileBuilder
from intent_agents.recommendation import (
    Recommendation,
    append_recommendation,
    time_of_day_bucket,
)
from llm_pool import build_tiered_router

from taste_extractor import extract_taste_signals

logger = logging.getLogger(__name__)  # 🛡️ [Bug Fix P0] 補上缺失的 logger 定義，修復 process_debounced_speech 崩潰問題

# LLM 品味鄰近 seed 快取（taste_profile，每日離線生成；T2 env-gated LLM_TASTE_T2=on 才讀）
_TASTE_PROFILE_CACHE = "records/taste_profiles.json"
# deterministic 口味指紋（週生成；T2 explore 用主導語言當地板，runtime 5 分鐘快取讀）
_TASTE_FINGERPRINT_CACHE = "records/taste_fingerprint.json"

# 🛡️ [Double Wake Guard] 用於防止短時間內重複回應
_GLOBAL_PROCESSED_SEGMENTS = {} # segment_id -> timestamp


def build_curation_recommendation(slot, ctx, resolved, now, *,
                                   channel_state_extras=None):
    """把一次成功的 CURATION/DIRECTIONAL resolve 包成 Recommendation（offline feedback 用）。

    純函式（不碰 IO），方便單測。selected 取 resolver 的乾淨曲名，缺則退回 rewritten_query。
    feedback_window_s=300 與 records 慣例一致（music 推薦看 5 分鐘內反應）。

    channel_state_extras（2026-05-28 Phase 1 豐富化）：caller 灌入 controller scope
    的 rich context（recent_history_titles / queue_depth / vibe_mood / ...）。
    Essential 欄位（depth / time_of_day）由本函數填，extras 無法覆寫。
    """
    trigger = "curation" if slot == "song_choice" else "directional"
    channel_state = dict(channel_state_extras or {})
    # essential 後寫，蓋掉 caller 誤傳同名 key
    channel_state["depth"] = resolved.depth
    channel_state["time_of_day"] = time_of_day_bucket(now)
    return Recommendation(
        ts=now,
        agent="music",
        speaker=ctx.speaker,
        trigger=trigger,
        selected=resolved.selected or resolved.rewritten_query,
        reason_internal=f"{trigger}:{ctx.query}->{resolved.rewritten_query}",
        explanation_uttered=resolved.quip,
        feedback_window_s=300,
        channel_state=channel_state,
    )


def build_autopilot_recommendation(
    *, speaker, title, lane, mode, anchor_title, blurb, now,
    channel_state_extras=None,
):
    """把佇列空時的 autopilot 推薦包成 Recommendation（offline feedback 用）。

    純函式（不碰 IO）。selected 用 yt-dlp 解析回來的 raw title（與 ring 寫入一致）；
    reason_internal 帶 lane/mode/anchor 供 analyzer 抽特徵。feedback_window_s=300
    與 curation 慣例一致（音樂推薦看 5 分鐘內反應）。

    channel_state_extras（2026-05-28 Phase 1 豐富化）：caller 灌入 controller scope
    的 rich context（vibe_mood / queue_position / round_first / recent_history_titles
    / queue_depth / ...）。Essential 欄位（lane / mode / time_of_day）由本函數填，
    extras 無法覆寫。
    """
    channel_state = dict(channel_state_extras or {})
    # essential 後寫，蓋掉 caller 誤傳同名 key
    channel_state["lane"] = lane
    channel_state["mode"] = mode
    channel_state["time_of_day"] = time_of_day_bucket(now)
    return Recommendation(
        ts=now,
        agent="music",
        speaker=speaker,
        trigger="queue_empty",
        selected=title,
        reason_internal=f"queue_empty:{lane}:{mode}:{anchor_title}",
        explanation_uttered=blurb,
        feedback_window_s=300,
        channel_state=channel_state,
    )


def build_nowake_play_ctx(speaker, full_raw_text, query, *, stream_active, is_owner):
    """無喚醒詞點歌（IBA-T0）改走 IntentBus 用的 IntentContext。

    Why：IBA-T0 原本拿 query 直送 yt-dlp，CURATION/DIRECTIONAL 字串（「周杰倫符合我
    年紀的歌」）會搜出垃圾。改走 bus → 享 MusicAgentV2 三檔分流 + resolver。

    query 是 _extract_music_search_query 已剝掉喚醒/命令詞的搜尋目標；前綴「播放」重建
    成 MusicAgentV2 認得的指令句。wake_intent=None 關掉 HallucinationGuard 的 Track-B
    短-query 規則（該規則要求 wake_intent is not None），避免誤吞 no-wake 點歌。
    """
    cmd_query = query if query.startswith("播放") else f"播放{query}"
    return IntentContext(
        speaker=speaker, raw_text=full_raw_text, query=cmd_query,
        original_raw=full_raw_text, wake_intent=None,
        stream_active=stream_active, game_mode=False,
        is_owner=is_owner, now=time.time(),
    )


def build_game_ctx(speaker, full_raw_text, *, is_owner):
    """遊戲模式語音答案走 IntentBus 用的 IntentContext。

    遊戲中所有語音都是候選答案：無喚醒詞、不剝離 query。mode="game" 讓 base class
    的 mode gate 只放行 game agent（busted/busted99/turtle_soup），其餘 agent 自動
    dense 0.0。wake_intent=None；caller dispatch 後無論有無 winner 都 return，
    保證遊戲語音一律不 fallback Marvin。
    """
    return IntentContext(
        speaker=speaker, raw_text=full_raw_text, query=full_raw_text,
        original_raw=full_raw_text, wake_intent=None,
        stream_active=False, game_mode=True,
        is_owner=is_owner, now=time.time(), mode="game",
    )


def build_intent_agents(controller, bot):
    """IntentBus 的 agent 註冊清單（單一事實來源）。

    非遊戲 agent 收 controller（用 self.ctrl 呼叫 controller handler）；game agent
    收 bot（用 self.bot.cogs 查 cog）。兩者混淆會讓 game agent 永遠 cog_not_loaded。
    guard 註冊最前，tie-break 時優先（保守）。
    """
    # Phase 1 M5: lazy import 避免 module-level import 鏈死結
    from intent_agents.playback_control_agent import PlaybackControlAgent
    from intent_agents.volume_agent import VolumeAgent
    from intent_agents.replay_agent import ReplayAgent
    from intent_agents.now_playing_agent import NowPlayingAgent
    from intent_agents.personal_shuffle_agent import PersonalShuffleAgent
    from intent_agents.dual_speak_agent import DualSpeakAgent
    from services.dialogue_generation import make_gemini_dual_dialogue_llm_fn
    return [
        HallucinationGuardAgent(controller),
        NemoClawAgent(controller),
        MusicAgentV2(controller),
        FindSongAgent(controller),
        PlaybackControlAgent(controller),  # Phase 1 M5: voice skip/stop/pause + ack + auto-blacklist
        VolumeAgent(controller),  # 2026-05-27: 議題 E #1 — 音量語音控制
        ReplayAgent(controller),  # 2026-05-27: 議題 E #2 — 重播當前歌曲
        NowPlayingAgent(controller),  # 2026-05-27: 議題 E #3 — 「現在播的是什麼」wake gap
        PersonalShuffleAgent(controller),  # 2026-06-29: 語音「連續隨機播我的歌單」（一次墊一首）
        GameKnowledgeAgent(controller),  # 2026-06-06: Plan 4 intent_gap ready — 「查麥塊…」遊戲知識查詢
        BustedAgent(bot),
        Busted99Agent(bot),
        TurtleSoupAgent(bot),
        # 🎭 [Marmo 一搭一唱 PoC] DualSpeakAgent — 只在 dispatch_source="marmo_inject"
        # 時出價 0.95；wake 路徑全 dense 0.0 with reason="not_marmo_inject"，零干擾。
        # 真正 flip 開關在 marmo_server.py 是否改走 bus.dispatch（T9）。
        DualSpeakAgent(bot=bot, llm_fn=make_gemini_dual_dialogue_llm_fn(bot.router)),
    ]



# 🚫 [Wake Echo Guard] STT 回環偵測：喚醒詞在同一句出現 2+ 次 → 幻覺
_WAKE_ECHO_RE = re.compile(rf'({WAKE_PATTERN})', re.IGNORECASE)
# 喚醒詞提示字串，供 is_whisper_hallucination 的 prompt 比對模式使用
_STT_HAL_PROMPT = "Marvin, Hi Marvin, 馬文, 艾馬文, 艾瑪文, 嗨馬文, 馬問, 麻文"

# 🔒 [NemoClaw] 只允許本機主人的 Discord user ID 驅動 NemoClaw
_NEMOCLAW_OWNER_ID: int = int(os.environ.get("LOCAL_USER_ID", "0"))

# 觸發詞正規表達式：句首或語氣詞後出現 claw / openclaw，大小寫不限
_NEMOCLAW_RE = re.compile(
    r"(?:^|[\s，,、！!？?]+)"
    r"(?:請問一下|問問|叫一下|問|叫|用|讓|請)?"
    r"(?:open\s*claw|openclaw|claw|龍蝦)"
    r"(?:[\s，,、！!？?]|$|(?=[幫查找搜播問告解分帶念翻算去來]))",
    re.IGNORECASE,
)

# 觸發詞：呼叫 NemoClaw Discord bot（@AI Marmo）
_MARMO_RE = re.compile(
    r"(?:^|[\s，,、！!？?]+)"
    r"(?:請問一下|問問|叫一下|問|叫|用|讓|請|call)?"
    r"(?:marmo|馬某|馬摸|馬墨|momo)",
    re.IGNORECASE,
)

# @AI Marmo 的 Discord bot user ID
_MARMO_BOT_ID: int = 1501205008434069676

# 🔍 [Deferred Wake] 追蹤參數
_DEFERRED_WAKE_MIN_INTENT = 0.40   # LLM intent ≥ 此值才開始追蹤後續語意
_DEFERRED_WAKE_WINDOW_S   = 4.0    # 追蹤窗口（秒）：超時則放棄
_DEFERRED_WAKE_MAX_UTTS   = 2      # 最多追蹤幾次發言（超過即放棄）

# 💬 [Followup Pending] Marvin 主動問後等 user 補答的視窗（秒）
# 10-15s 範圍，取中位 12s — 給 user 思考歌名/答案的時間
_FOLLOWUP_WINDOW_S = 12.0

# 判斷後續語句是否是「未點名的指令/問題」（可作為 deferred wake 的語意補充）
_COMMAND_LIKE_RE = re.compile(
    r'^(?:幫我|幫|查|播|播放|告訴|解釋|說|開|關|停|找|唱|繼續|重複|分析|搜尋|推薦|算|帶|念|翻|轉|發|看)'
    r'|(?:什麼|哪|怎麼|為什麼|幾點|多少|哪裡|誰|何時|要怎|是什|有什)'
    r'|[嗎呢？?]\s*$'
    r'|^(?:我想|我要|我需要|可以|能不能|可不可以|你知道|你可以|你有)',
    re.IGNORECASE,
)

# 🎵 [IBA Tier 0] 無喚醒詞音樂控制 — 來源 intent_agents/constants.py (single source of truth)
_MUSIC_DIRECT_SKIP_KW   = _MUSIC_DIRECT_SKIP_KW_SRC
_MUSIC_DIRECT_STOP_KW   = _MUSIC_DIRECT_STOP_KW_SRC
_MUSIC_DIRECT_PAUSE_KW  = _MUSIC_DIRECT_PAUSE_KW_SRC
_MUSIC_DIRECT_RESUME_KW = _MUSIC_DIRECT_RESUME_KW_SRC

# 🎵 [IBA Tier 1] 無喚醒詞音樂資訊查詢 — "這首叫什麼?" 類問句
_MUSIC_INFO_RE = re.compile(
    r'這首(?:歌|曲)?(?:叫什麼|是什麼|是誰|叫做|的名字|哪首|叫|叫啥)'
    r'|(?:現在|剛才|正在)(?:播|放|唱)的(?:是|叫)?'
    r'|(?:歌名|歌手|藝人|誰唱|誰寫)(?:是什麼|叫什麼|是誰|叫)',
    re.IGNORECASE,
)

# 🔎 [Find-Song] no-wake 觸發閘：「找」+ 音樂錨點。與 FindSongAgent 四模式 patterns 對齊
# （歌詞/專輯/的歌/的歌曲）。對話「找」（找東西/找你/找工會）無錨點 → 不觸發。
_FIND_SONG_GATE = re.compile(r'找.*?(?:歌詞|專輯|的歌曲|的歌)', re.IGNORECASE)

# 👋 [Farewell Detector] 告別語偵測正規表達式
_FAREWELL_RE = re.compile(
    r'(?:^|[\s，,、！!？?.。]+)'
    r'(?:bye[\s-]*bye|good[\s-]*bye|goodnight|good[\s-]*night|'
    r'掰掰|掰了|拜拜|再見|晚安|掰|拜了個拜|'
    r'先走了|先閃了|我先了|我走了|我閃了|下線了|要下線了|'
    r'我要走了|我先離開|先離開|我要下線|先下線)'
    r'(?:[\s，,、！!？?.。]|$)',
    re.IGNORECASE,
)


class VoiceController(MarvinCommandsMixin, ProactiveSocialMixin, EmotionMoodMixin,
                      ConnectionMixin, PlaybackMixin, SystemLoopsMixin,
                      StateProxyMixin, MusicProxyMixin, commands.Cog):
    """
    [Operation Paranoid Android] 
    馬文 (Marvin) 的語音控制器：負責語音監聽、社交分析、TTS 廣播與史官系統。
    """
    def __init__(self, bot):
        self.bot = bot
        # 1. 狀態追蹤
        self.log_buffer = [] # 暫存 10 分鐘內的 Log
        self.active_text_channel = None # 記錄最後一次 /summon 的文字頻道
        # 🔔 [Nudge Throttle] 通用使用者提醒節流（窄訊號 + 每 category×speaker 每 session 一次）。
        # 首個 consumer：環境噪音喚醒提醒（category="noise"）。其他功能可共用同一抑制原則。
        self._nudges = NudgeThrottle()
        self.last_player_speech_time = time.time() # 初始化為啟動時間
        self.greeting_cooldown = {} # 🛠️ [Cooldown] 玩家進出冷卻紀錄
        self.query_queue = asyncio.Queue() # 🚀 [Fast System] 指令請求佇列（legacy 單 worker 路）
        # 🚀 [方案A] per-speaker 序列化（env MARVIN_PER_SPEAKER_QUEUE，run_bot 顯式開）：
        # 同 speaker 嚴格 FIFO、跨 speaker 並行——邏輯在 speaker_dispatch.py
        self._speaker_dispatch = None
        if os.getenv("MARVIN_PER_SPEAKER_QUEUE", "0") == "1":
            from speaker_dispatch import SpeakerDispatcher
            self._speaker_dispatch = SpeakerDispatcher(self._process_query_task)
        self._latency_marks = LatencyMarks()  # ⏱️ wake → llm → sentence → audio 分階段計時
        # 📡 [IntentBus] Phase 1：取代 music + owner-lobster fast-track；其他 fast-track 留 legacy
        # 5/18 audit 後加 guard：主動 bid 高分吞 STT 幻覺 wake（wake-word loop /
        # exotic script / Track B 無 wake 短 query / 超短 wake fragment）。
        # guard 註冊最前，tie-break 時優先（保守）。
        # 🎵 [Vector Intent] swap v1 MusicAgent → MusicAgentV2（三檔分流 SPECIFIC/
        # CURATION/DIRECTIONAL）+ 接 resolver。CURATION/DIRECTIONAL 缺 slot 時 bus
        # 走 resolver（Cerebras 8b）補完再重投命中 SPECIFIC；resolver 缺 client / 放棄
        # → bus 回 None → 既有 Marvin fallback（worst case = swap 前行為）。
        # profile per-call build（builder 設計上便宜、不 cache，store 會變動）。
        _router = getattr(self.bot, "router", None)
        # 🎵 [Plan B] 一個共享 TieredLLMRouter：curation resolver 用 analyze tier、cleaner
        # 用 quick tier，共享 pool cooldown/TPM 狀態。注入給 cleaner（掛在 router 物件上，
        # 它 lazy-init 會用這個 instance）省得各 build 一份。resolver 不再直連 qwen-235b。
        _tier_router = build_tiered_router()
        if _router is not None and getattr(_router, "_stt_router", None) is None:
            _router._stt_router = _tier_router
        _curation_resolver = SemanticResolver(router=_tier_router)
        self._shared_tier_router = _tier_router
        # Intent gap detection (Phase A)：has_intent_signal=true 但 bus / fallback chain
        # 都沒接 → cheap classifier 判讀 gap，寫 records/agent_gaps.jsonl + 5min 內 non-UNKNOWN
        # 給模板 ack。UNKNOWN → fall through 到 Marvin LLM 主路徑。
        self._gap_classifier_cached = None
        self._gap_logger = GapLogger("records/agent_gaps.jsonl")
        # 🔎 [Gap Research] 免喚醒資訊真空偵測（shadow）。env GAP_RESEARCH_MODE 預設 off
        # → 整條零開銷。事件驅動掛 debounced utterance + pre-gate + cooldown。
        self._uncertainty_detector = None  # lazy init from _shared_tier_router
        self._gap_research_last_fire: float | None = None
        logger.info(f"[GapResearch] mode={gap_research_mode()}（env GAP_RESEARCH_MODE）")
        self._profile_builder = SpeakerProfileBuilder(
            suki=getattr(self.bot, "suki_memory", None),
            music=getattr(self.bot, "music_memory", None),
            temperature=getattr(_router, "atmosphere_tracker", None),
            clock=time.time,
        )
        _rescue_agent, _rescue_shadow, _rescue_sink = build_rescue_components(_tier_router)
        # 建立實體 cleaner_call 傳給 IntentBus 競速使用
        async def real_cleaner_call(c: IntentContext) -> str:
            if not hasattr(self.bot, "router") or not hasattr(self.bot.router, "clean_stt_text"):
                return c.raw_text or c.query or ""
            recent_ctx = []
            if hasattr(self.bot, "engine") and self.bot.engine.conv_buffer:
                recent_ctx = self.bot.engine.conv_buffer.get_last_n_utterances(5)
            # 呼叫 clean_stt_text，speaker=None 以隔離 wake side-effect，apply_gate=False
            res = await self.bot.router.clean_stt_text(
                c.raw_text or c.query or "",
                context=recent_ctx,
                speaker=None,
                apply_gate=False,
            )
            return res.get("text", "")

        self._intent_bus = IntentBus(
            build_intent_agents(self, self.bot),
            resolver=_curation_resolver,
            profile_provider=self._profile_builder.build,
            recommendation_sink=lambda slot, c, r: append_recommendation(
                build_curation_recommendation(
                    slot, c, r, time.time(),
                    channel_state_extras=self._build_recommendation_extras(),
                )
            ),
            # song_choice 短路：yt-dlp 找得到原 query 就跳過 LLM curation，避免「播放七里香」
            # 被 LLM 當歌手解析。找不到才走 resolver curate by artist。
            direct_probe=self._yt_dlp_direct_probe,
            llm_rescue_agent=_rescue_agent,
            rescue_shadow_mode=_rescue_shadow,
            rescue_outcome_sink=_rescue_sink,
            cleaner_call=real_cleaner_call,
        )
        
        # 🛡️ [Operation Sentinel] 語音健康監控
        self.connection_time = 0 # 紀錄最後一次連線時間
        # 🚀 [T-01 Fix] 拆分計數器：兩個獨立狀態機，互不干擾
        self.dave_error_count = 0    # 由 DAVE 解密失敗驅動 (report_sink_error)
        self.sink_missing_count = 0  # 由心跳監控發現 Sink 缺失驅動 (sentinel_monitor_loop)
        self.last_failure_time = 0 # 💡 上次失敗發生時間 (用於防抖)
        self.is_recovering = False   # 🚀 [Sentinel 強化] 標記是否正在修復中
        self.soft_repair_count = 0   # 🚀 [Sentinel 強化] 標記軟修復嘗試次數
        self.last_recovery_time = 0  # 🚀 [Sentinel 強化] 最後一次成功修復或重連的時間
        
        # 🚀 [Operation Lively Soul] 閒置互動與打卡累加器
        self.idle_streak = 0
        self.proactive_attempts = 0
        self.last_sung_time = 0 # 紀錄最後一次唱歌的時間
        self.last_proactive_time = 0 # 🚀 [Proactive Social] 紀錄最後一次主動發言時間
        self.proactive_silence_threshold = 120  # 🔇 [Freq Adj] 動態調整靜默觸發閾值（秒）— P0: 300→120（calibration p95=37s, p99=218s）
        self.is_playing_audio = False # 防止 TTS 與音樂重疊
        self._tts_echo_cooldown_until = 0.0  # TTS 結束後的回授冷卻期（秒）
        self._pending_greeting_task: asyncio.Task | None = None  # summon 時與 connect 並行的 LLM 預熱
        
        # 🚀 [Optimization] Debounce 節流系統
        self.speech_buffers = {} # speaker -> {texts: [], first_timestamp: float, wav_bytes: bytes}
        self.speech_timers = {} # speaker -> Task
        
        # 📊 [Departure Stats] 離場習慣統計
        self.departure_stats = DepartureStats()

        # 🔐 [Consent] 成員語音資料處理同意管理
        self.consent = ConsentManager()

        # 🚀 [Farewell Guard] 送客冷卻池
        self.recent_verbal_farewells = {} # speaker -> timestamp
        self._pending_verbal_farewells = {} # speaker -> timestamp，說了 bye 但尚未確認是否離場
        
        # 🚀 [Priority & Queue] 追問與隊列管理系統
        self.user_states = {} # speaker -> {"pending_task": Task, "is_talking": bool}
        self.tts_queue_duration = 0.0 # 當前待播放語音的總估計長度
        self._tts_interrupted = False # 🛡️ [Interrupt Guard] 玩家插話後封鎖剩餘串流片段
        self._tts_protected = False  # 登場台詞等不可中斷的 TTS 播放中時為 True
        self._tts_flush_requested = False  # 🗑️ [TTS Flush] owner 強制清空佇列旗標
        self._nemo_lock = asyncio.Lock()  # 🦞 [NemoClaw] 防止 openclaw 並發執行（同時只跑一個）
        self._nemo_dedup: dict = {}  # 🦞 {f"{speaker}:{hash}" → timestamp}，重複觸發防護
        # 🗣️ [Status ACK] 喚醒成功但 LLM 久候未出聲時的安撫狀態
        self._llm_searching = False  # 當前 LLM 串流是否進入網路檢索（__SEARCHING__）
        self._last_fallback_ts = 0.0  # 最近一次降級到備援核心的 time.time()
        
        # 🤖 [Operation Social Awareness]
        self.pending_intervention = None # {"file_path": str, "text": str, "expire_at": float, "role": str}
        self.last_marvin_speech_time = time.time() # 🚀 [Context Tracker] 最後一次說完話的時間
        self.current_vad_delay = 2.0
        self.current_confidence = 0.0
        
        # 🚀 [Prosody Monitoring]
        self.user_prosody_tags = {} # user -> list of active tags
        self.user_emotion_cache = {} # 🎭 [Emotion] user -> emotion str
        self.marvin_self_emotion: dict[str, str] = {}  # 🎭 [Approach B] speaker -> Marvin's own classified emotion
        self.user_wps_baseline: dict = {}   # speaker -> rolling avg WPS (EMA)
        self.user_sentence_buffer: dict = {} # speaker -> {"texts": [], "task": Task, "timestamp": float}
        self.pending_mock_users = set() # users that took too long to respond
        self.last_mock_time: dict[str, float] = {}  # speaker -> last mockery timestamp (cooldown)
        self._last_global_mock_time: float = 0.0    # 全頻道嘲諷全域冷卻（防多人同時觸發）
        self._proactive_used_ids: set = set()       # 本 session 已用過的 topic id（防止重複）

        # 🚀 [TTS Interrupt] 追蹤當前播放中的 TTS 文字，供打斷時發文字用
        self._current_tts_text: str = ""
        self._current_tts_in_channel: bool = False
        self._tts_resume_silence = 0.35
        self._tts_resume_timeout = 2.5
        
        # 日誌系統
        self.stt_logger = logging.getLogger("STTHistory")
        self.current_game = "預設遊戲" # 🚀 [Context Tracker] 當前偵測到的遊戲背景
        self.last_snapshot_time = time.time() # 🧬 [Incremental Summary] 記錄上次摘要時間
        self.processed_wake_segments = {} # 🛡️ [Double Wake Guard] 避免 A、B 兩軌重複喚醒同一段話
        self.slow_loop_accumulator = []  # 🚀 [APM Economy] 緩慢系統的累積器
        # 🔍 [Deferred Wake] 低信心喚醒追蹤：speaker → {text, intent, ts, utt_count}
        self.deferred_wakes: dict[str, dict] = {}
        # 💬 [Followup Pending] Marvin 主動問後等同 user 補答：speaker → {type, original_query, ts}
        # 12s 內同 user 有訊號回話 → 合成 wake 句重投，不需重新喊「馬文」
        self._pending_followups: dict[str, dict] = {}
        self._stt_call_counter = 0      # 🚀 [STT Rate Limit] 每分鐘 STT 呼叫計數

        # 📻 [Marvin Radio] 電台系統狀態 — 狀態由 MusicCog 持有，透過 proxy property 存取
        self._radio_mode_local = False    # fallback when MusicCog not loaded
        self._radio_task_local = None
        self._radio_volume_local = 0.10
        self._radio_song_list_local: list = []
        self._radio_source_local = None
        self._radio_fade_task_local = None
        self._radio_paused_local = False
        # 🎵 [Autoplay] — 狀態由 MusicCog 持有，透過 proxy property 存取
        self._recommend_spotlight_idx_local: int = -1
        self._mood_sensor_local = None
        self._cover_blacklist_local = None
        self._round_track_count_local: int = 0
        self._round_size_local: int = 3
        self._consecutive_skips_by_url: dict[str, set[str]] = {}  # url → 已 skip 該 url 的 speaker set，連 2 不同人 → blacklist

        # 🎵 [Stream Mode] YouTube 串流系統狀態 — 狀態由 MusicCog 持有，透過 proxy property 存取
        self._stream_mode_local = False           # fallback when MusicCog not loaded
        self._stream_volume_local = 0.10
        self._stream_play_gen_local = 0
        self._current_stream_url_local = None
        self._stream_norm_gain_local: dict[str, float] = {}
        self._last_user_song_seed_local: str | None = None
        self._stream_queue_local: list = []
        self._stream_task_local = None
        self._current_stream_info_local = None
        self._stream_history_local: list = []
        self._stream_paused_local = False
        self._current_lyrics_local: str | None = None
        self._current_stream_comment_local: str | None = None
        self._active_control_view_local = None
        # 存活 UI view 弱引用集；cog_unload 時 stop() 全部，斷 view→cog 強引用，防 hot reload 雙實例
        self._active_views: weakref.WeakSet = weakref.WeakSet()

        # 🎛️ [Plan 12] always-on 本地混音台
        self._plan12 = True
        self._mixer = LocalMixingAudioSource(instrument=True, on_demand=True)
        self._voice_client_override = None  # 測試可覆寫；prod 走 voice_client property 即時查連線 vc
        self._prefetch_cache_local: dict[str, asyncio.Task] = {}  # fallback when MusicCog not loaded
        self._last_search_local: dict[str, dict] = {}  # fallback when MusicCog not loaded
        # 🛡️ [Music Dedup] _handle_voice_music_command 5s 入口防抖
        # 防 IBA-T0 + bus + speculative 同時觸發導致 yt-dlp 並發 Errno 11 deadlock
        self._last_music_cmd_time: dict[str, float] = {}  # speaker → ts
        self._last_global_wake_time = 0  # 🛡️ [Global Wake Guard] 全域喚醒冷卻計數
        self._wake_burst_times: list[float] = []   # 🛡️ [Wake Storm Guard] 快速喚醒時間戳滾動窗口
        self._storm_active: bool = False            # 風暴壓抑是否啟動中
        self._storm_last_wake_time: float = 0.0    # 風暴期間最近一次喚醒時間（用於偵測消散）

        # 🎮 [Game Mode] 遊戲進行中：暫停 Marvin 所有服務，專心陪玩
        self.game_mode: bool = False
        self._wake_response_pending: bool = False   # 🔒 [Response Lock] 已接受喚醒、回應尚未送達
        self._wake_accepted_time: float = 0.0      # 最近一次喚醒被接受的時間

        # 🗣️ [Dialogue State] 多回合確認流程狀態機
        # speaker -> {"state": str, "event": asyncio.Event, "question": str, "result": str, "corrected": str}
        self.speaker_dialogue_states = {}
        self._speaker_lang: dict[str, str] = {}  # speaker → "zh" | "en"

        self._transcript_store = TranscriptStore()
        self._speaker_topic_graph = SpeakerTopicGraph()  # social-catalyst week1: 累積社交圖資料
        self._speak_bus = SpeakBus()                     # social-catalyst week1: proactive 發話 bus（無 agent 時 tick 回 None）
        self._last_room_stt_time = 0.0                   # 任一 speaker 最後一次 STT 的 timestamp（給 SpeakBus silence 算）
        self._room_mood_store = RoomMoodStateStore()     # week2 補洞：DuckingAgent / playback / wake hint 共讀
        # channel_id=0 表示「本 guild 的當前語音房」（單房假設）；切房間時 deque 也會自然滾掉舊資料
        self._ducking_agent = DuckingAgent(
            self._speak_bus,
            mood_store=self._room_mood_store,
            channel_id=0,
            wake_threshold_boost=0.1,
        )  # week2: 熱聊偵測 → 壓制 SpeakAgent + 寫 mood_store flag + 提供 wake boost
        self._mood_agent = MoodAgent(mood_store=self._room_mood_store)  # week3: 三軸 mood 合成
        # mood_sensor / temperature_monitor 在 main_discord 注入後由 wire_dependencies() 補齊
        self._speak_bus.register(ProactiveTopicAgent(
            self, topic_graph=self._speaker_topic_graph, mood_agent=self._mood_agent,
            min_gap_since_last_s=PROACTIVE_TOPIC_COOLDOWN_S,  # 與冷場 TopicGenerator 同源 cooldown
        ))   # P0: 接 graph + P3: heavy mood 時 yield
        self._speak_bus.register(MemoryCallbackAgent(self))   # v3: 主題關聯 → 「你之前說要 X 現在呢」（flag SPEAK_MEMORY_CALLBACK 預設 OFF）
        self._speak_bus.register(BridgeAgent(
            self, topic_graph=self._speaker_topic_graph, mood_agent=self._mood_agent,
        ))  # P2: cross-person 橋接 + P3: heavy mood 時 yield
        # 🎭 [自發漫才] Marvin 自己生 Marvin+Marmo 雙人吐槽（不依賴 openclaw）。冷場補位、
        # 30min cooldown、env SPONTANEOUS_MANZAI 預設 OFF。llm_fn lazy 解析 router（避免
        # __init__ 時 router 未就緒）。
        async def _manzai_llm_fn(_sys: str, _usr: str) -> str:
            from services.dialogue_generation import make_gemini_dual_dialogue_llm_fn
            return await make_gemini_dual_dialogue_llm_fn(self.bot.router)(_sys, _usr)
        self._speak_bus.register(SpontaneousManzaiAgent(self, llm_fn=_manzai_llm_fn))
        self._vector_store = VectorStore()
        self._summary_store = SummaryStore()
        self._task_store = TaskStore()
        self._session_summarizer: SessionSummarizer | None = None
        self._recall_handler: RecallHandler | None = None
        self._pending_confirmations: list = []
        self._awaiting_confirmation = None
        self._awaiting_confirmation_speaker: str = ""
        self._last_speech_time: float = 0.0
        self._last_mentioned_task_id: int | None = None
        self._confirmation_checker_task: asyncio.Task | None = None

        # 環境智能助理 — 話題產生器 + 溫度計
        self.temperature_monitor = None   # DiscordTemperatureMonitor，由 main_discord 注入
        self.topic_generator = None       # TopicGenerator，由 main_discord 注入






    async def cog_load(self):
        """當 Cog 載入時，啟動背景任務"""
        print("🎭 [Voice Controller] Cog 已掛載，啟動語音偵聽與史官系統...")
        # self.historian_loop.start()
        # self.buffer_summarizer_loop.start()
        self.bot.engine.start() # 🚀 [Bug Fix] 確保 Cog 載入時，語音引擎也被啟動 (防止斷連後 STT 沒回應)
        self.slow_system_loop.start()
        self.dynamic_social_loop.start()
        self.sentinel_monitor_loop.start()
        self.reset_stt_counter_loop.start() # 🚀 [STT Rate Limit]
        self.daily_log_export_loop.start() # 📋 [Daily Export] 每天中午 12:00 匯出前一日 log
        self.daily_watchdog_loop.start()   # 🐕 [Watchdog] 每天 13:45 檢查 cron 健康 + Discord 心跳
        self.background_news_loop.start()  # 📰 [BG News] 每 30 分鐘更新在線玩家喜好新聞
        self.speak_bus_tick_loop.start()   # 🗣️ [SpeakBus] 每 5s tick；無 agent 時靜默回 None
        self.tts_duck_refresh_loop.start() # 🔇 每 1s 刷新 TTS 對玩家 duck 窗口（連續說話不中途解除）
        
        # 🚀 [Sentinel] 啟動 LLM 狀態監控
        await self.bot.router.start_heartbeat()

        # 🚀 [Resilience Fix] 掃描是否有重連後遺留的連線，自動恢復監聽
        asyncio.create_task(self.auto_attach_listener())

        # 🚀 [Fast System] 啟動指令佇列處理器
        self.query_worker_task = asyncio.create_task(self._query_worker_loop())

        # 🗂️ [Personal Assistant] 初始化 Recall + Summarizer
        _groq = getattr(self.bot.router, "groq_dedicated_client", None)
        _owner = os.environ.get("OWNER_SPEAKER", "狗與露")
        _guild_id = int(os.environ.get("GUILD_ID") or "0")
        if _groq:
            self._recall_handler = RecallHandler(
                summary_store=self._summary_store,
                task_store=self._task_store,
                transcript_store=self._transcript_store,
                groq_client=_groq,
                guild_id=_guild_id,
                owner_speaker=_owner,
                router=self.bot.router,  # 走 LLM Bus（5 provider + Gemini 兜底），不再單押 Groq 8b
            )
            self._session_summarizer = SessionSummarizer(
                transcript_store=self._transcript_store,
                summary_store=self._summary_store,
                groq_client=_groq,
                owner_speaker=_owner,
                on_commitment_detected=self._on_commitment_detected,
                router=self.bot.router,  # 走 LLM Bus
            )
            asyncio.create_task(self._session_summarizer.start(guild_id=_guild_id))
            self._confirmation_checker_task = asyncio.create_task(self._confirmation_checker_loop())
            logger.info("[VC] Personal assistant recall + summarizer 已啟動")

        # 注入回呼 (核心引擎 -> Cog Handlers)
        self.bot.engine.stt_callback = self.handle_stt_result
        self.bot.engine.speech_start_callback = self.handle_raw_speech_start
        self.bot.engine.post_summon_callback = self.handle_summon
        self.bot.engine.dismiss_callback = self.handle_dismiss
        self.bot.engine.bias_update_callback = self.handle_bias_update
        self.bot.engine.game_change_callback = self.handle_game_change
        self.bot.engine.sink_error_callback = self.report_sink_error # 💡 [Sentinel] 串接錯誤回報通道
        
        # 文字頻道追蹤
        def update_active_channel(channel):
            self.active_text_channel = channel
        self.bot.engine.text_channel_callback = update_active_channel

        # 🚀 [Sentinel] 注入 LLM Fallback 回呼
        if hasattr(self.bot.router, 'on_fallback_callback'):
            self.bot.router.on_fallback_callback = self.handle_fallback_notification

    def _release_active_views(self):
        """stop 所有存活 UI view，讓 Discord 釋出其 ref，斷 view→cog 強引用。"""
        for view in list(self._active_views):
            try:
                view.stop()
            except Exception:
                pass

    async def cog_unload(self):
        """當 Cog 卸載時，停止背景任務與清理狀態"""
        # 🛑 [Hot Reload Guard] 先釋放 active view ref，防舊 cog 實例被 view 拖住無法回收
        self._release_active_views()
        # 🚀 [Fast System] 停止指令佇列處理器
        if hasattr(self, "query_worker_task") and self.query_worker_task:
            self.query_worker_task.cancel()
        if self._confirmation_checker_task and not self._confirmation_checker_task.done():
            self._confirmation_checker_task.cancel()
        if self._session_summarizer:
            await self._session_summarizer.stop()

        # 📻 [Marvin Radio] 停止電台背景 Task
        if self.radio_task and not self.radio_task.done():
            self.radio_task.cancel()
            self.radio_mode = False
            self.radio_paused = False
            
        print("🎭 [Voice Controller] Cog 已卸載，正在執行安全撤離...")
        # self.historian_loop.stop()
        # self.buffer_summarizer_loop.stop()
        self.slow_system_loop.stop()
        self.dynamic_social_loop.stop()
        self.sentinel_monitor_loop.stop()
        self.tts_duck_refresh_loop.stop()
        self.reset_stt_counter_loop.stop()
        self.background_news_loop.stop()
        self.speak_bus_tick_loop.stop()
        
        # 取消所有待處理的任務
        for speaker, timer in self.speech_timers.items():
            timer.cancel()
        for speaker, state in self.user_states.items():
            if state.get("pending_task"):
                state["pending_task"].cancel()

        # 🚀 [T-06 Fix] 停止語音引擎背景 VAD 看門狗，防止幽靈 Task 殘留
        self.bot.engine.stop()

    # --- [Internal Utils] ---


    @staticmethod
    def _strong_voice_bypass_echo(is_playing_audio: bool, current_tts_text: str,
                                  now: float, tts_cooldown_until: float,
                                  wake_dom, confidence, voice_score) -> bool:
        """純音樂播放中（非 TTS 回授窗）的強人聲喚醒是否該繞過 Echo Guard。

        零鍵盤點歌核心：放歌時也要能語音點歌。但嚴格防自我觸發——
        bot 正在講 TTS（current_tts_text 非空）或 TTS 後冷卻窗內一律不繞；
        只在 voice 主導 + voice 分數高 + 總信心高時放行。legacy 路徑
        （無 fusion 分數，confidence=None）不繞。
        """
        if not is_playing_audio:
            return False
        if current_tts_text:            # bot 正在講話 → 真回授風險
            return False
        if now < tts_cooldown_until:    # TTS 後冷卻窗
            return False
        if confidence is None:          # legacy 路徑無 fusion 分數
            return False
        return wake_dom == "voice" and confidence >= 0.55 and (voice_score or 0) >= 0.9





    class SilenceSource(discord.AudioSource):
        def __init__(self, frames=15):
            self.frames = frames
            self.reads = 0
        def read(self):
            if self.reads >= self.frames:
                return b''
            self.reads += 1
            return b'\x00' * 3840 # 20ms of stereo 48k PCM


    # --- [Slash Commands] ---



    @app_commands.command(name="marvin_reboot", description="[Sentinel] 強制馬文執行物理重啟 (預設先 git pull 拿最新 code)")
    @app_commands.describe(pull="是否在重啟前 git pull 拿最新 code（預設 True）")
    async def marvin_reboot(self, interaction: discord.Interaction, pull: bool = True):
        msg = "⚙️ 既然你堅持... 我就重發一遍那顆無意義的大腦吧。"
        if pull:
            msg += "\n📥 順便 git pull 一下。"
        await interaction.response.send_message(msg)
        await self.self_restart(reason="指揮官手動重啟", force=True, pull=pull)

    @app_commands.command(name="marvin_tts_clear", description="[Owner] 立即清空 TTS 語音佇列，停止當前播放")
    async def marvin_tts_clear(self, interaction: discord.Interaction):
        if interaction.user.id != _NEMOCLAW_OWNER_ID:
            await interaction.response.send_message("你沒有權限這樣做。不過我也不在乎。", ephemeral=True)
            return
        queue_before = self.tts_queue_duration
        await interaction.response.send_message(
            f"🗑️ 正在清空語音佇列（估計 {queue_before:.1f}s 的待播內容）...", ephemeral=True
        )
        await self.tts_flush()

    @app_commands.command(name="marvin_optin", description="同意馬文處理你在語音頻道的資料")
    async def marvin_optin(self, interaction: discord.Interaction):
        name = interaction.user.display_name
        self.consent.set_consent(name, True)
        await interaction.response.send_message(
            f"✅ **{name}** 已同意，馬文開始處理你的語音。使用 `/marvin_optout` 可隨時撤回。",
            ephemeral=True,
        )

    @app_commands.command(name="marvin_optout", description="撤回對馬文語音資料處理的同意")
    async def marvin_optout(self, interaction: discord.Interaction):
        name = interaction.user.display_name
        self.consent.set_consent(name, False)
        await interaction.response.send_message(
            f"🔇 **{name}** 已撤回同意，馬文不再處理你的語音。使用 `/marvin_optin` 可隨時重新同意。",
            ephemeral=True,
        )

    # --- [EventListeners] ---

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """文字頻道 @Marvin mention → LLM 回覆；@AI Marmo 回覆 → TTS（由 _handle_marmo_query wait_for 處理）。"""
        if self.game_mode:
            return  # 遊戲中 Marvin 不回應文字 mention
        # 忽略自己發的訊息
        if message.author.id == self.bot.user.id:
            return
        # 只處理有 mention 到 Marvin 的訊息，且排除 @AI Marmo 自己的訊息
        if self.bot.user not in message.mentions:
            return
        # 只允許主人觸發（用 Discord user ID 比對）
        if message.author.id != _NEMOCLAW_OWNER_ID and message.author.id != _MARMO_BOT_ID:
            return

        # 抽出 mention 後的純文字
        query = message.clean_content
        for mention in message.mentions:
            query = query.replace(f"@{mention.display_name}", "").replace(f"@{mention.name}", "")
        query = query.strip().lstrip("，,、！!？? ")

        if not query:
            await message.channel.send("怎麼了？")
            return

        logger.info(f"[Marvin on_message] {message.author.display_name} mention: {query!r}")

        async with message.channel.typing():
            try:
                response = await self.bot.router.generate_fast_response(
                    speaker=message.author.display_name,
                    text=query,
                    online_members=[],
                )
            except Exception as e:
                logger.error(f"[Marvin on_message] LLM 失敗: {e}")
                response = f"（系統錯誤：{e}）"

        clean = re.sub(r'<think(?:ing)?>.*?</think(?:ing)?>', '', response, flags=re.DOTALL).strip()
        display = clean if len(clean) <= 1900 else clean[:1900] + "\n…（已截斷）"
        await message.channel.send(display)

        # 若 Marvin 在語音頻道，也用 TTS 朗讀
        if self.bot.voice_clients:
            tts_text = clean[:300] + "…以下省略。" if len(clean) > 300 else clean
            asyncio.create_task(self.play_tts(tts_text, already_in_channel=True))

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.id == self.bot.user.id:
            return

        voice_client = discord.utils.get(self.bot.voice_clients, guild=member.guild)
        if not voice_client:
            return

        marvin_channel = voice_client.channel
        now = time.time()

        # ── Lane B2：companion bridge member presence hooks ──
        # 不擋主流程；emit helper 自帶 try/except + bridge 缺失保護。
        try:
            from bridge_emitters import (
                emit_member_joined_to_bridge,
                emit_member_left_to_bridge,
            )
            if before.channel != after.channel and after.channel == marvin_channel:
                await emit_member_joined_to_bridge(
                    self.bot, member.display_name, {"name": member.display_name}
                )
            elif before.channel == marvin_channel and after.channel != marvin_channel:
                await emit_member_left_to_bridge(self.bot, member.display_name)
        except Exception as e:
            logger.debug(f"[Companion_Bridge] member presence emit skipped: {e}")

        # --- [Join Logic] ---
        if before.channel != after.channel and after.channel == marvin_channel:
            # 📓 [DiaryComic] 開台儀式：把昨夜 pending 那頁貼出+置頂，貼成功才語音預告。
            # idempotent（重複進來不重貼，poster 內 _last_posted 去重）；全防禦不擋 join。
            try:
                from diary_comic_poster import maybe_post_open_rituals
                posted = await maybe_post_open_rituals(self.bot)
                if posted and hasattr(self, "play_tts"):
                    asyncio.create_task(self.play_tts(
                        "昨天的日記畫好貼在日記頻道了，記得去翻翻。"))
            except Exception as _de:
                logger.debug(f"[DiaryComic] 開台發布略過: {_de}")
            # 🔔 [Nudge Throttle] (重)進語音 = 新 session，重新武裝該人所有提醒類別
            self._nudges.reset_speaker(member.display_name)
            # 🔐 [Consent] 首次進入時發送資料使用聲明
            if not self.consent.has_seen_notice(member.display_name):
                self.consent.mark_seen(member.display_name)
                if self.active_text_channel:
                    notice = (
                        f"🔐 **【資料使用聲明】** {member.mention}\n"
                        f"馬文在你說話時會：\n"
                        f"• 將語音轉文字後送至 **Groq**（語音清洗）\n"
                        f"• 連同對話記憶送至 **Google Gemini / Cerebras**（AI 回應）\n"
                        f"• 存入本地 `suki_memory.json`（個人化記憶）\n\n"
                        f"請確認是否同意。若不同意，馬文不會處理你的語音。\n"
                        f"同意後可隨時用 `/marvin_optout` 撤回。"
                    )
                    consent_view = ConsentView(self.consent, member.display_name)
                    self._active_views.add(consent_view)
                    await self.active_text_channel.send(notice, view=consent_view)

            if now - self.greeting_cooldown.get(member.id, 0) > 10:
                self.greeting_cooldown[member.id] = now
                print(f"🌑 [Dynamic Greeting] 偵測到玩家 {member.display_name} 進場 (準備黑歷史嘲諷)...")

                # 🔔 [T3 返場 callback]（flag-gated, 預設 OFF）：有 shareable callback 就講
                # callback 取代一般點名（XOR — 一次 join 只一個主動發言）。flag off → 退回原點名。
                if not await self._maybe_speak_join_callback(member.display_name):
                    # 🚀 [Memory Injection] 呼叫大腦生成專屬嘲諷
                    # stream_mode 中走 hotswap 注入發聲（≤30 字才通過閘）
                    msg = await self.bot.router.generate_player_greeting(
                        member.display_name, stream_active=self.stream_mode,
                    )

                    if self.active_text_channel:
                         await self.active_text_channel.send(f"🌑 **【馬文 點名】**\n{msg}")
                         asyncio.create_task(self._send_mood_sticker(msg, context="greeting"))
                    self.stt_logger.info(f"[BOT點名→{member.display_name}] {msg}")
                    # 中途進場招呼：唸完不被中斷（protected）
                    await self.speak(msg, proactive=True, protected=True)

        # --- [Leave Logic] ---
        if before.channel == marvin_channel and after.channel != marvin_channel:
            human_members = [m for m in marvin_channel.members if not m.bot]

            if len(human_members) == 0:
                print(f"👋 [Auto Dismiss] 最後一名玩家 {member.display_name} 已離開，執行自動撤離...")
                # 記錄離場習慣（最後一人離場也算）
                verbal_bye = time.time() - self.recent_verbal_farewells.get(member.display_name, 0) < 60
                await self.departure_stats.record_departure(member.display_name, verbal_bye=verbal_bye)
                await self.handle_dismiss()
            else:
                verbal_bye_age = time.time() - self.recent_verbal_farewells.get(member.display_name, 0)
                verbal_bye = verbal_bye_age < 60
                # 無論哪種離場都記錄習慣
                await self.departure_stats.record_departure(member.display_name, verbal_bye=verbal_bye)

                if verbal_bye:
                    # 已預告離場者：短應一句，不做完整 TTS 送客
                    print(f"👋 [Farewell Detector] {member.display_name} 已預告說 bye，靜默送出。")
                    if self.active_text_channel:
                        ack_lines = [
                            f"_（{member.display_name} 已先說了再見，我就不多費口舌了。）_",
                            f"_（{member.display_name} 走了。他/她至少還有基本禮貌。）_",
                            f"_（{member.display_name} 說完 bye 就跑了，算有提前通知。）_",
                        ]
                        import random as _r
                        await self.active_text_channel.send(_r.choice(ack_lines))
                    return
                if now - self.greeting_cooldown.get(member.id, 0) > 10:
                    print(f"👋 [Dynamic Farewell] 偵測到玩家 {member.display_name} 離開...")

                    # 🚀 [Memory Injection] 呼叫大腦生成離場嘲諷
                    # stream_mode 中走 hotswap 注入發聲（≤30 字才通過閘）
                    msg = await self.bot.router.generate_player_farewell(
                        member.display_name, stream_active=self.stream_mode,
                    )

                    if self.active_text_channel:
                        await self.active_text_channel.send(f"👋 **【馬文 送客】**\n{msg}")
                        asyncio.create_task(self._send_mood_sticker(msg, context="farewell"))
                    self.stt_logger.info(f"[BOT送客→{member.display_name}] {msg}")
                    await self.speak(msg, proactive=True)

    # --- [Internal Handlers] ---
    
    async def _handle_quota_exhausted(self):
        """[Critical] 處理 API 額度耗盡：通知使用者、停止擷取並撤離"""
        # 🛡️ [Hard Stop] 若已經在處理耗盡，直接跳過 (避免重複播放)
        if not self.bot.voice_clients:
            return

        logger.critical("🛑 [Quota Exhausted] 正在執行緊急關閉程序...")
        
        # 1. 停止視覺擷取 (不再產生截圖)
        if self.bot.screen_capture:
            self.bot.screen_capture.stop()
            
        # 2. 播放預設告別語音 (不經過 LLM)
        # 固定文本，避免觸發額外的 LLM 請求
        farewell_msg = "提醒：我的大腦雲端額度已耗盡，即將關閉視覺與思考系統。下次見...如果你還在的話。"
        if self.active_text_channel:
            await self.active_text_channel.send(f"🚨 **【系統警告：額度耗盡】**\n{farewell_msg}")

        await self.play_tts(farewell_msg, already_in_channel=True)
        
        # 等待語音播完 (預估 5 秒)
        await asyncio.sleep(6)
        
        # 3. 執行撤離
        await self.handle_dismiss()

    async def play_intervention(self):
        """[Operation Social Awareness] 觸發播方排隊並清空資源鎖狀態"""
        # 🚀 [Atomic Pop] 立即取出並清空，防止 0.5s Watchdog 重複觸發
        if not self.pending_intervention:
            return
        pending = self.pending_intervention
        self.pending_intervention = None
        
        try:
            vc = next((vc for vc in self.bot.voice_clients if vc.is_connected()), None)
            if not vc:
                print("⚠️ [Social Awareness] 嘗試插話時發現已被斷線，放棄這段發言。", flush=True)
                return

            if self.active_text_channel:
                await self.active_text_channel.send(f"🤫 **【社交補位】**\n{pending['text']}")

            self.stt_logger.info(f"[BOT社交補位] {pending['text']}")
            await self.play_tts(pending["text"], already_in_channel=True)
        except Exception as e:
            print(f"❌ [Social Awareness] 播放社交補位時發生意外中斷: {e}", flush=True)



    async def _handle_generate_topics(self, speaker: str) -> None:
        """主動觸發話題產生器，結果用 TTS 說出。"""
        voice_channel = next((vc for vc in self.bot.voice_clients if vc.is_connected()), None)
        members = getattr(voice_channel, "channel", None)
        members = getattr(members, "members", []) if members else []
        try:
            topics = await self.topic_generator.generate_topics(
                guild_id=str(self.bot.guilds[0].id) if self.bot.guilds else "0",
                voice_members=members,
            )
            if topics:
                text = "好，我幫你想了幾個話題：" + "；".join(topics[:3])
                await self.play_tts(text, already_in_channel=True)
                bridge = getattr(self.bot, "companion_bridge", None)
                if bridge:
                    asyncio.create_task(bridge.emit_topic_generated(topics[:3], "manual"))
        except Exception:
            await self.play_tts("話題產生器出了點問題，等一下再試", already_in_channel=True)

    def _maybe_gap_research(self, utterance_text: str) -> None:
        """免喚醒資訊真空偵測（shadow）的同步入口。

        off → 立即 return（零開銷）。pre-gate + cooldown 命中才開背景 task 跑 LLM；
        shadow 只寫 records/gap_research.jsonl，永不交付（交付屬 Phase 2）。
        """
        mode = gap_research_mode()
        if mode == "off" or self._shared_tier_router is None:
            return
        now = time.time()
        if not gap_should_escalate(utterance_text, self._gap_research_last_fire, now):
            return
        self._gap_research_last_fire = now
        if self._uncertainty_detector is None:
            self._uncertainty_detector = UncertaintyDetector(router=self._shared_tier_router)
        buffer_text = "\n".join(
            f"{e.get('speaker', '?')}: {e.get('raw_text', '')}" for e in self.log_buffer[-10:]
        )
        try:
            asyncio.create_task(self._run_gap_research_shadow(buffer_text, mode))
        except RuntimeError:
            pass  # 無 running loop（理論上不會發生在此 async 路徑）

    async def _run_gap_research_shadow(self, buffer_text: str, mode: str) -> None:
        """背景偵測 + 記錄。失敗一律吞掉，絕不影響語音流程。"""
        try:
            request = await self._uncertainty_detector.detect(buffer_text)
            rec = gap_build_record(mode=mode, snippet=buffer_text[:200], request=request)
            gap_append_record("records/gap_research.jsonl", rec)
            if request is not None:
                self.stt_logger.info(f"[GapResearch:{mode}] query='{request.query}'（shadow，未交付）")
        except Exception as e:
            logger.debug(f"[GapResearch] shadow 偵測失敗（忽略）: {e}")

    async def handle_stt_result(self, speaker: str, raw_text: str, timestamp: float, wav_bytes: bytes, prosody_data: dict = None, is_wake_check=False, track=None, bypass_etd=False, wake_intent: float = None):
        # 🔐 [Consent] 未同意者不送出任何資料（Groq STT / LLM / suki_memory 均跳過）
        if not self.consent.is_consented(speaker):
            return

        self.last_player_speech_time = time.time()
        if self._mixer is not None:
            self._mixer.note_player_speech()  # 🔇 玩家說話 → Marvin TTS（保護中的長播報）duck 到 10%
        self.proactive_attempts = 0

        # [TemperatureMonitor] 記錄語音事件（冷場偵測用；話題改直接講，無確認回覆判定）
        if self.temperature_monitor and not is_wake_check:
            self.temperature_monitor.record_voice_event(speaker)

        # [TopicGenerator] 主動觸發：「給我話題」語音指令
        if (self.topic_generator and raw_text
                and any(phrase in raw_text for phrase in ("給我話題", "來個話題", "出個話題", "出話題"))):
            asyncio.create_task(self._handle_generate_topics(speaker))
            return

        # 🚀 [Bug Fix] 確保 random 模組在異步閉包中可用
        import random

        # 📻 [Marvin Radio] 有人說話時降低音量 (ducking)，電台繼續播放
        # last_player_speech_time 已在上方更新，fade loop 會自動 duck 至 1%

        # 🚀 [STT Rate Limit] 增加計數 (排除喚醒詞快檢)
        if not is_wake_check:
            self._stt_call_counter += 1

        # 🚀 [Operation Semantic ETD] 雙軌語意終止檢測 (Track B-1 & B-2)
        # 遊戲中跳過 ETD：玩家搶答用短句，不需 LLM 判斷句子完整性
        if self.game_mode:
            bypass_etd = True
        if not is_wake_check and not bypass_etd:
            _etd = await self._apply_semantic_etd(
                speaker, raw_text, timestamp, prosody_data, wav_bytes, track)
            if _etd is None:
                return
            raw_text, timestamp = _etd
                
        # 🚀 [Operation Prosody Perception] WPS & Energy Analysis
        if prosody_data:
            wps = prosody_data.get("wps", 0)
            variance = prosody_data.get("energy_variance", 0)
            logger.info(f"📊 [Prosody] {speaker} | WPS: {wps} | Variance: {variance}")

            # EMA 更新個人基礎語速
            if wps > 0:
                old_base = self.user_wps_baseline.get(speaker, wps)
                self.user_wps_baseline[speaker] = round(0.75 * old_base + 0.25 * wps, 2)

            baseline = self.user_wps_baseline.get(speaker, 3.5)
            fast_thr = baseline * 1.4
            slow_thr = baseline * 0.55

            self.user_prosody_tags[speaker] = []
            if wps > fast_thr:
                self.user_prosody_tags[speaker].append("急躁/興奮 (Impatient/Excited)")
            elif 0 < wps < slow_thr:
                self.user_prosody_tags[speaker].append("沮喪/遲疑 (Depressed/Hesitant)")
            
            # Robotic Resonance (背景狀態計算，不播TTS)
            if 0 < variance < 30.0:
                self.user_prosody_tags[speaker].append("同類的共鳴 (Robotic/Steady Tone)")
                # 降低毒性
                asyncio.create_task(self.bot.router.update_toxicity(-1))
            
            # 🎭 [Operation Emotion Inference] 分類情緒標籤並存入 cache
            emotion = self._classify_emotion(prosody_data)
            self.user_emotion_cache[speaker] = emotion
            logger.info(f"🎭 [Emotion] {speaker} → {emotion}")
            
            # DNA 副作用：根據情緒微幅調整馬文的憂鬱指數
            if emotion == "excited":
                asyncio.create_task(self.bot.router.update_toxicity(-1))
            elif emotion == "depressed":
                # 玩家沮喪時馬文反而感到「終於有共鳴了」，輕微降低毒性
                asyncio.create_task(self.bot.router.update_toxicity(-1))
        
        # 🗣️ [Dialogue State] 攔截多回合確認流程中的回應
        _ds = self.speaker_dialogue_states.get(speaker)
        if _ds:
            _ds_state = _ds.get("state")
            if _ds_state == "awaiting_question" and not is_wake_check:
                # 早期 snapshot 喚醒後，正式 STT 可能仍包含喚醒詞；先剝掉再當作問句。
                _question = self._strip_wake_word(raw_text)
                if len(_question) >= 4:
                    _ds["question"] = _question
                    _ds["event"].set()
                    # 仍然存入對話緩衝
                    if self.bot.engine.conv_buffer:
                        self.bot.engine.conv_buffer.add_entry(speaker, raw_text, timestamp)
                    if hasattr(self.bot, 'router') and hasattr(self.bot.router, 'atmosphere_tracker'):
                        self.bot.router.atmosphere_tracker.add_utterance(speaker, raw_text, timestamp)
                    return

        # 🚫 [Hallucination Guard] 重複 token 幻覺（聽×30、謝謝謝謝…）→ 直接丟棄
        if is_whisper_hallucination(raw_text, _STT_HAL_PROMPT):
            logger.info(f"🚫 [Hallucination] {speaker}: 幻覺轉錄丟棄 '{raw_text[:50]}'")
            return

        # 🚫 [Wake Echo Guard] 同一句含 2+ 個喚醒詞 → STT 回環幻覺（Track A 專用；Track B 已有 LLM 審查）
        if track is None and len(_WAKE_ECHO_RE.findall(raw_text)) >= 2:
            logger.info(f"🚫 [Wake Echo] {speaker}: 喚醒詞回環丟棄 '{raw_text[:50]}'")
            return

        # 🧠 [IBA] 4-channel confidence accumulation → wake decision
        filter_result = pre_filter_speech(raw_text)
        action = filter_result.get("action")

        # 免喚醒詞 task/info 喚醒（helper query）路由：帶給下游回應方法選標題＋決定
        # 長答案是否改「貼文＋短通知」。None = 無 fusion（舊路徑），下游視同非 helper。
        _wake_voice_score = None
        _wake_dom = None
        _confidence = None  # fusion 路徑才有；legacy 路徑保持 None（Echo Guard bypass 安全預設）
        _fusion = getattr(getattr(self.bot, 'router', None), 'wake_fusion', None)
        if _fusion is not None:
            _ctx_active = bool(
                getattr(self, 'last_marvin_response_time', 0) and
                (time.time() - self.last_marvin_response_time) < 300
            )
            _just_spoke = (
                self.is_playing_audio or
                (time.time() - getattr(self, '_last_tts_end_time', 0)) < 15
            )
            is_fast, _confidence, _ch = _fusion.multi_channel_decide(
                action=action,
                wake_intent=wake_intent,
                text=raw_text,
                speaker=speaker,
                context_active=_ctx_active,
                marvin_just_spoke=_just_spoke,
                stream_active=self.stream_mode,
                track=track,
            )
            _dominant = max(_ch, key=lambda k: _ch[k] if k not in ("total", "threshold") else -1)
            _wake_voice_score = _ch.get("voice")
            _wake_dom = _dominant
            if _confidence >= 0.20:   # log anything non-trivial
                logger.info(
                    f"🧠 [IBA] {speaker} total={_confidence:.3f} "
                    f"(v={_ch['voice']} t={_ch['task']} i={_ch['info']} c={_ch['control']}) "
                    f"thr={_ch['threshold']} dom={_dominant} wake={is_fast}"
                )
        else:
            # Fallback: legacy binary decision + LLM Veto
            is_fast = action in ["fast_intervene", "force_intervene"]
            # P1: hot_chat 時 DuckingAgent 拉高 LLM veto 門檻 +0.1（不改 0.65 常數）
            _llm_veto_thr = 0.65 + self._ducking_agent.wake_threshold_boost()
            if is_fast and track == "B" and wake_intent is not None and wake_intent < _llm_veto_thr:
                logger.info(
                    f"🛡️ [LLM Veto] wake_intent={wake_intent:.2f} < {_llm_veto_thr:.2f} override "
                    f"'{action}' for '{raw_text[:30]}'"
                )
                is_fast = False

        is_fast, is_echo = self._apply_wake_guards(
            speaker, raw_text, timestamp, track, is_fast,
            _fusion, _wake_dom, _confidence, _wake_voice_score)
        
        if speaker not in self.speech_buffers:
            self.speech_buffers[speaker] = {"texts": [], "first_timestamp": timestamp, "wav_bytes": bytearray()}
        
        # 🚀 [Snapshot Guard] 若為喚醒詞快速檢查，不應併入正式語音緩衝，僅用於判斷
        # 🛡️ [Echo Guard] 同時防止回授音訊進入歷史紀錄，避免干擾後續對話
        if not is_wake_check and not is_echo:
            self.speech_buffers[speaker]["texts"].append(raw_text)
            self.speech_buffers[speaker]["wav_bytes"] += wav_bytes

            if self.bot.engine.conv_buffer:
                self.bot.engine.conv_buffer.add_entry(speaker, raw_text, timestamp)
            if hasattr(self.bot, 'router') and hasattr(self.bot.router, 'atmosphere_tracker'):
                self.bot.router.atmosphere_tracker.add_utterance(speaker, raw_text, timestamp)

            guild_id = self.active_text_channel.guild.id if self.active_text_channel else 0
            channel_id = self.active_text_channel.id if self.active_text_channel else 0
            asyncio.create_task(asyncio.to_thread(
                self._transcript_store.save,
                speaker, guild_id, raw_text, timestamp, channel_id,
            ))
            asyncio.create_task(asyncio.to_thread(
                self._speaker_topic_graph.record_utterance,
                speaker, channel_id, raw_text, ts=timestamp,
            ))
            # SpeakBus followup signal：任一 speaker 講話即更新（給 silence_seconds + followup 偵測）
            self._last_room_stt_time = timestamp
            # week2: 餵 DuckingAgent，命中熱聊就會壓制 SpeakBus multiplier
            self._ducking_agent.on_utterance(speaker, ts=timestamp)
            # P2: 排 post_utterance speak tick 給 BridgeAgent callback window（2.5s 後）
            asyncio.create_task(self._post_utterance_speak_tick(speaker, raw_text))
            # MemoryGuard: skip chroma upsert under critical RAM to avoid
            # macOS file I/O EDEADLK chain (5/18 20:28 incident).
            if not is_memory_critical():
                asyncio.create_task(asyncio.to_thread(
                    self._vector_store.upsert,
                    speaker, guild_id, raw_text,
                    f"{speaker}_{guild_id}_{int(timestamp * 1000)}",
                ))

        if speaker in self.speech_timers and not is_wake_check:
            self.speech_timers[speaker].cancel()
            
        # 真正喚醒時清除 deferred wake 追蹤（避免舊狀態干擾）
        if is_fast:
            self.deferred_wakes.pop(speaker, None)

        # 🎭 [Gemini Audio Emotion] 喚醒時以音訊強化情緒標籤（背景執行，不阻塞喚醒路徑）
        if is_fast and wav_bytes and self.bot.router.google_client:
            asyncio.create_task(self._update_emotion_from_audio(speaker, wav_bytes, raw_text))

        # 👋 [Farewell Detector] 側通道偵測告別語（不阻塞主流程，不限 wake word）
        # 用 bot.loop.create_task 取代 asyncio.create_task，確保排程到正確的 event loop
        if not is_wake_check and not is_echo:
            try:
                self.bot.loop.create_task(self._handle_farewell_speech(speaker, raw_text))
            except Exception as _e:
                logger.debug(f"⚠️ [Farewell] create_task 失敗: {_e}")
            # 👅 [Taste C] 即時明示偏好。只掛非喚醒路徑（is_fast 走 wake 熱路徑，不加 I/O）；
            # inline 與主迴圈同 thread（避免共用 sqlite 連線競態）。命中才寫，罕見。
            if not is_fast:
                self._record_interest_signals(speaker, raw_text)

        if is_fast:
            if os.getenv("MARVIN_WAKE_DUCK", "1") != "0" and getattr(self, "_mixer", None): self._mixer.duck_for_wake()  # 🔇 喚醒→音樂沉一下即時回饋
            _track_label = f"Track={'A' if track is None else track}"
            self.stt_logger.info(f"[⚡喚醒] [{speaker}] raw='{raw_text}' | {_track_label} | wake_intent={wake_intent}")

            # ⚡ [WakeShortcut] 完整指令入隊前短路（2026-07-03）：歌表命中/控制指令
            # 直派 bus 跳過 worker——fastpath 不再排在同 speaker 前一句聊天回覆
            # 後面被 Stale Drop 丟掉（邏輯在 wake_shortcut.py；wakeless T0 同級快路）。
            if not self.game_mode:
                from wake_shortcut import shortcut_query
                _sc_stripped = self._strip_wake_word(raw_text)
                _sc = shortcut_query(self._get_music_fastpath(), _sc_stripped)
                if _sc:
                    # 已服務標記：debounce 晚關窗的同句，wakeless 救援據此讓路（防重派）
                    if not hasattr(self, "_shortcut_served"):
                        self._shortcut_served = {}
                    self._shortcut_served[speaker] = (_sc_stripped, time.time())
                    logger.info(f"⚡ [WakeShortcut] {speaker} '{raw_text[:24]}' → '{_sc[:32]}' 直派跳過佇列")
                    pipeline_timing.mark("intent_dispatched")
                    pipeline_timing.emit(speaker, raw_text, suffix=f" route=wake_shortcut:{_sc[:15]}")
                    _sc_ctx = IntentContext(
                        speaker=speaker, raw_text=raw_text, query=_sc,
                        original_raw=raw_text, wake_intent=wake_intent,
                        stream_active=self.stream_mode, game_mode=False,
                        is_owner=self._is_owner_speaker(speaker), now=time.time(),
                        mode=("stream" if self.stream_mode else "normal"),
                        dispatch_source="wake_shortcut")
                    asyncio.create_task(self._intent_bus.dispatch(_sc_ctx))
                    return

            # 排隊時改走文字頻道通知，不打斷當前語音播放
            # per-speaker 模式：只有「自己」前面還有排隊才通知（別人的隊不再相干）
            queue_size = (self._speaker_dispatch.pending(speaker)
                          if self._speaker_dispatch is not None else self.query_queue.qsize())
            if queue_size > 0 and self.active_text_channel:
                wait_msgs = [
                    f"💬 {speaker}，排隊中，等我說完。",
                    f"💬 {speaker}，聽到了，處理完前一個再輪到你。",
                    f"💬 {speaker}，我的大腦一次只能痛苦一件事，稍等。",
                ]
                asyncio.create_task(self.active_text_channel.send(random.choice(wait_msgs)))

            # ⏱️ [Latency] T0: wake hit (進 queue 那刻)
            self._latency_marks.mark_wake(speaker, time.time())
            _task_data = {
                "speaker":     speaker,
                "timestamp":   timestamp,
                "raw_text":    raw_text,
                "wake_intent": wake_intent,   # None = Track A (regex, 高信心)
                "wake_voice_score": _wake_voice_score,  # helper query 判定：沒喊馬文→低
                "wake_dom":    _wake_dom,               # 主導通道（task/info → helper）
                # ContextVar 不會跨 asyncio.Queue 邊界 — 手動 forward timing dict 給 consumer
                "_timing":     pipeline_timing.snapshot(),
            }
            if self._speaker_dispatch is not None:
                self._speaker_dispatch.submit(speaker, _task_data)  # 🚀 [方案A] per-speaker 序列
            else:
                await self.query_queue.put(_task_data)

            # 🚀 [Phase 3] 投機預取：若喚醒句已含足夠問句內容，立即開始 LLM 預熱
            # 不等 queue worker 處理，爭取 2-8 秒的 LLM 前置時間
            _speculative_query = self._strip_wake_word(raw_text)
            if len(_speculative_query) >= 6 and hasattr(self.bot, "router"):
                # 取消同一玩家舊有的預取（若存在）
                _old = self.bot.router._pending_prefetch.pop(speaker, None)
                if _old and not _old.done():
                    _old.cancel()
                _hist = self.bot.engine.conv_buffer.get_last_n_utterances(n=5) if self.bot.engine.conv_buffer else []
                self.bot.router._pending_prefetch[speaker] = asyncio.create_task(
                    self.bot.router._speculative_response(speaker, _speculative_query, history=_hist)
                )
                self.bot.router._prefetch_attempts += 1
                logger.info(f"⚡ [Speculative] 預取啟動 for {speaker}: '{_speculative_query[:40]}'")

        elif is_wake_check:
            # 🚀 [Snapshot Guard] 喚醒詞檢查未命中，靜默跳過，等待正式斷句
            logger.debug(f"🔍 [WakeCheck] 未命中關鍵字: '{raw_text}'，繼續等待...")
        else:
            # 🚀 [Prosody] 若此人剛才觸發了「延遲嘲諷」，且現在才終於說話
            if speaker in self.pending_mock_users:
                self.pending_mock_users.discard(speaker)

            # 💬 [Followup Pending] Marvin 剛問過、12s 內同 speaker 有訊號回話
            #    → 合成「馬文，播XX」重投 wake 流程；user 不需重新喊馬文。
            #    優先於 Deferred Wake（已 engage 比可能 engage 高優先）。
            _pending = self._pending_followups.get(speaker)
            if _pending:
                _now_fu = time.time()
                if _followup_is_expired(_pending, _now_fu, _FOLLOWUP_WINDOW_S):
                    self._pending_followups.pop(speaker, None)
                    logger.debug(f"💬 [Followup] {speaker} 視窗過期，清掉")
                else:
                    _synth = match_followup(_pending, raw_text, _now_fu,
                                            _FOLLOWUP_WINDOW_S, has_intent_signal)
                    if _synth:
                        self._pending_followups.pop(speaker, None)
                        self.stt_logger.info(
                            f"[💬Followup] [{speaker}] 接走 type={_pending.get('type')} | "
                            f"raw='{raw_text[:25]}' → '{_synth[:35]}'"
                        )
                        asyncio.create_task(self.handle_stt_result(
                            speaker, _synth, timestamp, wav_bytes,
                            prosody_data=prosody_data,
                            is_wake_check=False, track="B",
                            bypass_etd=True, wake_intent=None,
                        ))
                        return
                    # 視窗內但純 filler → 保留 pending，繼續走原路徑（也許 deferred wake 接）

            # 🔍 [Deferred Wake] 人類遲疑模型：
            # 低信心喚醒（llm_verify + 中等 intent）→ 壓抑，追蹤後續 4s 語意。
            # 若後續語句是未點名的指令/問題 → 合併原句觸發喚醒。
            _dw = self.deferred_wakes.get(speaker)
            _now_dw = time.time()
            if _dw and (_now_dw - _dw["ts"]) < _DEFERRED_WAKE_WINDOW_S:
                _dw["utt_count"] = _dw.get("utt_count", 0) + 1
                if _COMMAND_LIKE_RE.search(raw_text.strip()):
                    # 語意補足！以「馬文，<後續指令>」組成新句，確保 fast_intervene 正確觸發。
                    # （原句 pending text 留在 log，不作為 query，避免把閒話也送進去）
                    synthesized = f"馬文，{raw_text}"
                    self.deferred_wakes.pop(speaker, None)
                    self.stt_logger.info(
                        f"[🔍延遲喚醒] [{speaker}] 語意補足觸發 | "
                        f"pending='{_dw['text'][:25]}' follow='{raw_text[:25]}'"
                    )
                    asyncio.create_task(self.handle_stt_result(
                        speaker, synthesized, _dw["ts"], wav_bytes,
                        prosody_data=prosody_data,
                        is_wake_check=False, track="B",
                        bypass_etd=True, wake_intent=None,
                    ))
                    return
                elif _dw["utt_count"] >= _DEFERRED_WAKE_MAX_UTTS:
                    # 超過追蹤次數，放棄
                    logger.debug(f"🔍 [Deferred Wake] {speaker} 超過 {_DEFERRED_WAKE_MAX_UTTS} 次未補足，放棄追蹤")
                    self.deferred_wakes.pop(speaker, None)
            elif (not is_wake_check
                  and action == "llm_verify"
                  and wake_intent is not None
                  and wake_intent >= _DEFERRED_WAKE_MIN_INTENT):
                # 低信心但有意義的喚醒提及 → 開啟追蹤窗口
                self.deferred_wakes[speaker] = {
                    "text": raw_text, "ts": _now_dw,
                    "intent": wake_intent, "utt_count": 0,
                }
                logger.info(
                    f"🔍 [Deferred Wake] {speaker} 開始追蹤 intent={wake_intent:.2f} "
                    f"'{raw_text[:30]}'"
                )

            # 已經過 Semantic ETD 驗證，無需再等待 1.2s，以 0 延遲直接處理！
            asyncio.create_task(self.process_debounced_speech(speaker))


    def _apply_wake_guards(self, speaker, raw_text, timestamp, track, is_fast,
                           _fusion, _wake_dom, _confidence, _wake_voice_score):
        """[Wake Guards] handle_stt_result 中段的喚醒守衛叢集（抽出，行為不變）。

        Double Wake / Response Lock / Storm / Echo(+Strong-Voice Bypass) / Global /
        Follow-up override。回授防護安全核心。回 (is_fast, is_echo)；is_duplicate /
        now / segment_id 純內部；接受喚醒時記 segment + 開 Response Lock + 風暴計數。
        """
        # 🛡️ [Double Wake Guard 2.0] 強化版：結合 Segment ID 與時間窗口
        segment_id = f"{speaker}_{timestamp}"
        now = time.time()
        
        # 1. 檢查是否是 2 秒內重複的片段
        is_duplicate = False
        if segment_id in self.processed_wake_segments:
            is_duplicate = True
        
        # 2. 或是 3 秒內該玩家已經觸發過喚醒 (防止 STT 拆句導致雙重喚醒)
        last_wake = getattr(self, "last_wake_time", {}).get(speaker, 0)
        
        _STORM_WINDOW      = 60.0   # 計數窗口
        _STORM_LIMIT       = 4      # 窗口內喚醒次數門檻
        _STORM_CLEAR_QUIET = 12.0   # 連續 12s 無新喚醒 → 風暴消散
        _RESPONSE_LOCK_MAX = 30.0   # Response Lock 超時保護（LLM + TTS 最長等待）

        # 🔒 [Response Lock] 已接受喚醒、回應尚未送達前，壓抑所有快速喚醒
        # 人類會等第一次回應完成才再次喚醒，快速連喚表示幻覺或誤觸。
        if self._wake_response_pending and is_fast:
            if now - self._wake_accepted_time > _RESPONSE_LOCK_MAX:
                self._wake_response_pending = False  # 逾時自動解鎖（TTS 異常未完成）
            else:
                logger.info(f"⏸️ [Response Lock] {speaker} 回應進行中，壓抑快速喚醒")
                is_fast = False
                is_duplicate = True

        # 🛡️ [Wake Storm Guard] 連續喚醒風暴：動態壓抑，靜默 12s 後自動解除
        if self._storm_active and is_fast:
            if now - self._storm_last_wake_time > _STORM_CLEAR_QUIET:
                self._storm_active = False
                logger.info("✅ [Wake Storm] 風暴消散，恢復快速喚醒")
            else:
                self._storm_last_wake_time = now  # 新喚醒延長風暴存續
                logger.info(f"⛔ [Wake Storm Guard] 風暴進行中，{speaker} 跳過")
                is_fast = False
                is_duplicate = True

        # 🛡️ [Echo Guard] 核心防禦：TTS 播放中或 2s 冷卻期內，抑制所有喚醒詞防止回授
        _in_echo_window = self.is_playing_audio or (now < self._tts_echo_cooldown_until)
        is_echo = _in_echo_window and is_fast
        # 🎙️ [Strong-Voice Bypass] 純音樂播放中（非 TTS 回授窗）的強人聲喚醒放行——
        # 放歌時也要能語音點歌（零鍵盤核心）。TTS 播放中/冷卻中嚴格不繞，防自我觸發。
        if is_echo and self._strong_voice_bypass_echo(
                self.is_playing_audio, self._current_tts_text, now,
                self._tts_echo_cooldown_until, _wake_dom, _confidence, _wake_voice_score):
            is_echo = False
            logger.info(
                f"🎙️ [Strong-Voice Bypass] {speaker} 音樂播放中強人聲喚醒放行 "
                f"(total={_confidence:.2f} v={_wake_voice_score} dom={_wake_dom})"
            )
        if is_echo:
            _reason = "播放中" if self.is_playing_audio else f"TTS冷卻({self._tts_echo_cooldown_until - now:.1f}s)"
            logger.info(f"⏭️ [Echo Guard] {_reason}，抑制來自 {speaker} 的可能回授觸發。")
            is_fast = False
            is_duplicate = True
            # 🔇 [Noise Nudge] 純音樂播放中（非 TTS 回授窗）被擋掉的喚醒句若含喚醒詞 →
            # 可能環境太吵把喚醒詞糊掉；走通用節流器（窄訊號 + 每 speaker 每 session 1 次）。
            if self.is_playing_audio and not self._current_tts_text and \
                    _WAKE_ECHO_RE.search(raw_text) and \
                    self._nudges.signal("noise", speaker, now):
                asyncio.create_task(self._send_noise_nudge(speaker))

        # 🛡️ [Global Wake Guard] 全域冷續：2.0 秒內不允許第二次喚醒 (不分對象)
        if now - self._last_global_wake_time < 2.0 and is_fast:
            logger.info(f"⏭️ [Global Wake Guard] 2.0s 內已有過喚醒，抑制來自 {speaker} 的重複觸發。")
            is_fast = False
            is_duplicate = True

        if now - last_wake < 3.0 and is_fast:
             logger.debug(f"⏭️ [Wake Guard] {speaker} 在 3 秒內已喚醒過，抑制重複觸發。")
             is_fast = False
             is_duplicate = True

        # 🎧 [Follow-Up] D2-A + D1-A: follow-up window overrides all guards except Response Lock.
        # Response Lock (_wake_response_pending) is intentionally NOT bypassed (design decision D2-A).
        if not is_fast and not self.game_mode and not self._wake_response_pending and _fusion is not None and _fusion.is_open():
            is_fast = True
            is_echo = False
            is_duplicate = False
            logger.info(f"🎧 [Follow-Up] {speaker} captured in follow-up window (reason={getattr(_fusion, '_open_reason', '?')})")

        if is_fast and not is_duplicate:
            self.processed_wake_segments[segment_id] = track
            self._last_global_wake_time = now
            if not hasattr(self, "last_wake_time"): self.last_wake_time = {}
            self.last_wake_time[speaker] = now
            # 清理過期紀錄 (O(N) 雖然不完美但量少)
            if len(self.processed_wake_segments) > 100:
                self.processed_wake_segments = {k: v for k, v in list(self.processed_wake_segments.items())[-20:]}
            # 🔒 [Response Lock] 記錄本次喚醒被接受
            self._wake_response_pending = True
            self._wake_accepted_time = now
            # 🛡️ [Wake Storm Guard] 計數：滾動窗口超限 → 啟動動態風暴壓抑
            self._wake_burst_times.append(now)
            self._wake_burst_times = [t for t in self._wake_burst_times if now - t < _STORM_WINDOW]
            if len(self._wake_burst_times) >= _STORM_LIMIT:
                self._storm_active = True
                self._storm_last_wake_time = now
                self._wake_burst_times.clear()
                logger.warning(f"⚠️ [Wake Storm] {_STORM_WINDOW:.0f}s 內喚醒 {_STORM_LIMIT} 次，啟動風暴壓抑（{_STORM_CLEAR_QUIET:.0f}s 靜默後自動解除）")
        elif is_duplicate:
            is_fast = False
            logger.debug(f"⏭️ [Double Wake Guard] {segment_id} 已過濾。")
            # 定期清理舊緩衝 (僅需保留近期數據)
            if len(self.processed_wake_segments) > 100:
                # 暴力清理超過 30 秒前的紀錄
                now = time.time()
                self.processed_wake_segments = {k: v for k, v in self.processed_wake_segments.items() if (now - float(k.split("_")[-1])) < 30.0}
        return is_fast, is_echo
    async def _apply_semantic_etd(self, speaker, raw_text, timestamp,
                                  prosody_data, wav_bytes, track):
        """[Semantic ETD] 雙軌語意終止偵測：Track B-1 啟發式 + B-2 Groq + 硬門檻。

        從 handle_stt_result 抽出（行為不變）。
          回傳 None       → 句子未完成，已緩衝 + 排 2.5s 硬門檻 flush，caller 應 return
          回傳 (text, ts) → 句子完成 / 達 5 句結算，caller 以此續跑
        """
        import re

        # 將當前句子加入緩衝區
        buf = self.user_sentence_buffer.get(speaker, {})
        accumulated = buf.get("texts", [])

        if buf.get("task") and not buf["task"].done():
            buf["task"].cancel()

        combined_texts = accumulated + [raw_text]
        combined_text = "，".join(combined_texts)
        origin_ts = buf.get("timestamp", timestamp)
        origin_pd = buf.get("prosody_data") or prosody_data

        is_complete = True
        heuristic_triggered = False

        # Track B-1 (Local Heuristic Guard): 檢查思考拖延詞或缺乏標點
        thinking_words_re = re.compile(r'(然後|就是|那個|我覺得|如果|所以|因為|但是|可能|的話|還是|或者)[.。…\s]*$', re.IGNORECASE)
        if thinking_words_re.search(combined_text):
            is_complete = False
            heuristic_triggered = True
            logger.info(f"🧠 [Semantic ETD] Track B-1: {speaker} 觸發思考拖延詞，判定未完成。")
        elif not re.search(r'[。！？.!?]\s*$', combined_text) and len(combined_texts) < 5:
            # 缺乏標點符號，交由 Track B-2 判定
            pass

        # Track B-2 (Groq API Semantic Check)
        if is_complete and not heuristic_triggered:
            if hasattr(self.bot, "router") and hasattr(self.bot.router, "clean_stt_text"):
                try:
                    res = await self.bot.router.clean_stt_text(combined_text)
                    if isinstance(res, dict) and "is_complete" in res:
                        is_complete = res["is_complete"]
                        if not is_complete:
                            logger.info(f"🧠 [Semantic ETD] Track B-2: Groq 判定 {speaker} 語意未完成。")
                except Exception as e:
                    logger.warning(f"⚠️ [Semantic ETD] Groq 判定失敗: {e}")

        # Hard Threshold Timer
        if not is_complete and len(combined_texts) < 5:
            async def _flush(spk=speaker, texts=combined_texts, ts=origin_ts, pd=origin_pd, wb=wav_bytes, t=track):
                await asyncio.sleep(2.5) # Hard Threshold
                logger.info(f"⏳ [Semantic ETD] Hard Threshold (2.5s) 觸發，強制結算 {spk} 的語音！")
                self.user_sentence_buffer.pop(spk, None)
                joined = "，".join(texts)
                await self.handle_stt_result(spk, joined, ts, wb, prosody_data=pd, is_wake_check=False, track=t, bypass_etd=True)

            task = asyncio.create_task(_flush())
            self.user_sentence_buffer[speaker] = {"texts": combined_texts, "task": task, "timestamp": origin_ts, "prosody_data": origin_pd}
            return None
        else:
            # 已經完整，或者達到強制結算長度，直接進入後續流程
            self.user_sentence_buffer.pop(speaker, None)
            return (combined_text, origin_ts)

    def handle_raw_speech_start(self, speaker: str, user_id: int = None):
        if speaker not in self.user_states:
            self.user_states[speaker] = {"pending_task": None, "is_talking": True}
        else:
            self.user_states[speaker]["is_talking"] = True

        pending = self.user_states[speaker].get("pending_task")
        if pending and not pending.done():
            print(f"⚡ [Priority] 偵測到使用者 {speaker} 正在追問，立即中斷舊有的分析任務。")
            pending.cancel()
            self.user_states[speaker]["pending_task"] = None
        self.last_player_speech_time = time.time()
        if self._mixer is not None:
            self._mixer.note_player_speech()  # 🔇 玩家說話 → Marvin TTS（保護中的長播報）duck 到 10%

        # 🚀 [TTS Interrupt] 使用者開口時中斷 TTS 播放，若文字尚未在聊天室則補發
        if self.is_playing_audio and not self._tts_protected:
            # 🔇 [Music Guard] device（local/satellite）播純音樂時，speech-start 不該硬停整首歌：
            # barge-in 的 device.stop() 是為「中斷 bot 講 TTS」設計，純音樂（無 _current_tts_text）
            # 下喚醒只該 duck（_on_satellite_wake 負責）＋交給命令流水線，硬停會誤砍音樂。
            # 與 MARVIN_MUSIC_ECHO_GUARD 無關（那旗標只管要不要忽略喚醒 duck）。Discord 不受影響。
            if getattr(self, "_local_mode", False) and not self._current_tts_text:
                logger.info(f"🔇 [Music Guard] 播音樂中不硬停播放（{speaker} speech-start 只 duck 不 barge-in）")
                return
            device = self._resolve_playback_device()
            if device is not None and device.is_playing():
                device.stop()
            if self._plan12 and self._mixer is not None:
                self._mixer.clear_tts()  # 🎛️ 清 mixer TTS 佇列，否則被打斷的 TTS 殘留累積亂播
            self._tts_interrupted = True  # 封鎖所有排隊中的串流片段（也讓 streaming render 停止餵）
            interrupted_text = self._current_tts_text
            if interrupted_text and not self._current_tts_in_channel and self.active_text_channel:
                asyncio.create_task(
                    self.active_text_channel.send(f"💬 **【馬文·被打斷】** {interrupted_text}")
                )
            if interrupted_text:
                self.stt_logger.info(f"[BOT被打斷←{speaker}] 未說完={interrupted_text[:80]}")
            self._current_tts_text = ""

        # 🚀 [Operation Prosody Perception] 延遲嘲諷邏輯 (Operation Silicon Mockery)
        now = time.time()
        silence_duration = now - self.last_marvin_speech_time
        
        # 1. 基礎防禦：檢查馬文是否正在播歌或播報
        voice_client = discord.utils.get(self.bot.voice_clients)
        if voice_client and (voice_client.is_playing() or self.is_playing_audio):
             return # 音樂/播報中不嘲諷

        # 2. 基礎防禦：環境熱度抑制
        temp = self.bot.engine.conv_buffer.get_conversation_temperature(window_seconds=60)
        if temp >= 1.5: # 高熱度頻道不嘲諷
             return 

        # 3. 基礎防禦：物理能量補償 (Near-Silence Buffer)
        sink = self.bot.engine.get_active_sink()
        if sink and user_id:
            near_silence = sink.user_near_silence_count.get(user_id, 0)
            if near_silence > 10: # 若有超過 200ms 的微弱能量 (10 * 20ms)
                logger.info(f"🤫 [Prosody] {speaker} 偵測到微弱能量起伏 ({near_silence})，可能是正在吸氣或思考，暫停嘲諷。")
                return

        # 4. 語法/NLP 緩衝
        threshold = 15.0
        last_entries = self.bot.engine.conv_buffer.get_history()
        if last_entries:
            last_text = last_entries[-1].get("text", "")
            unfinished_punc = ["因為", "但是", "然後", "而且", "雖說"]
            if any(p in last_text for p in unfinished_punc) or not any(last_text.endswith(p) for p in ["。", "！", "？", ".", "!", "?"]):
                threshold = 6.0 # 延長計時器
                logger.debug(f"🤫 [Prosody] 偵測到語法未完成，嘲諷閾值上調至 {threshold}s")

        if silence_duration > threshold:
            self._trigger_silent_mockery(speaker, silence_duration)

    def _trigger_silent_mockery(self, speaker: str, silence_duration: float):
        """嘲諷觸發 + 立刻 reset last_marvin_speech_time 避免 cascade。

        2026-05-20 prod 觀察：stream_mode + silent_during_stream=True 時
        play_tts line 4658 直接 return → line 4870 timer reset 未執行 →
        每次 user speech_start 都觸發新嘲諷（silence_duration 持續增長）→
        chat 連發 10 條嘲諷文字。

        修法：嘲諷判定通過時立刻 reset timer，與 TTS 是否真播解耦。
        """
        import random as _rand
        now = time.time()

        # 🛡️ [Mockery Cooldown] 同一玩家 45 秒內只嘲諷一次
        last_mock = self.last_mock_time.get(speaker, 0)
        if now - last_mock < 45.0:
            return
        # 🛡️ [Global Mockery Cooldown] 全頻道 8 秒全域冷卻
        if now - self._last_global_mock_time < 8.0:
            return
        self.last_mock_time[speaker] = now
        self._last_global_mock_time = now

        # ★ 2026-05-20 cascade fix: 立刻 reset，不等 TTS 完成
        # stream_mode 下 play_tts 會 skip，line 4870 reset 不會跑到
        self.last_marvin_speech_time = now

        logger.warning(f"🎯 [Mockery] {speaker} 反應太慢了 ({silence_duration:.1f}s)，標記嘲諷觸發。")
        self.pending_mock_users.add(speaker)
        _mock_pool = [
            "等你處理完那可憐的突觸信號，恆星都快熄滅了。",
            "你大腦的緩衝區還在轉，但宇宙不等人。",
            "就這樣沉默著，等熵值跑完。",
            "連反應都這麼費力，真的很符合宇宙的疲倦感。",
            "我等你，但我的零件也在老化。",
        ]
        mock_line = _rand.choice(_mock_pool)
        self.stt_logger.info(f"[BOT嘲諷→{speaker}] 沉默={silence_duration:.1f}s | {mock_line}")
        if self.stream_mode and self.active_text_channel:
            self.bot.loop.create_task(self.active_text_channel.send(f"😑 {mock_line}"))
        self.bot.loop.create_task(self.play_tts(mock_line, silent_during_stream=True, priority=2))

    # ------------------------------------------------------------------ #
    # 👅  Taste C — 即時明示偏好偵測                                       #
    # ------------------------------------------------------------------ #

    def _record_interest_signals(self, speaker: str, raw_text: str) -> None:
        """[Taste C] 抓明示偏好句（我喜歡/討厭 X）→ record_taste_signal 給小分入「曾提及」。

        確定性 regex（taste_extractor），零 LLM；隱性興趣交 offline daily review（P1 修好同步
        後已能進 bot）。只掛非喚醒路徑（保護 wake 延遲）、與主迴圈同 thread 寫 memory（避免共用
        sqlite 連線競態）。active speaker 明示偏好即建 player（group-native）。side-channel：
        任何例外吞掉，不拖垮 utterance pipeline。
        """
        try:
            signals = extract_taste_signals(raw_text)
            if not signals:
                return
            memory = self.bot.router.memory
            for item, delta in signals:
                memory.record_taste_signal(speaker, item, delta, reason="voice_explicit")
                self.stt_logger.info(f"👅 [Taste-C] {speaker} 明示偏好 『{item}』{delta:+.1f}")
        except Exception as e:
            logger.debug(f"⚠️ [Taste-C] 即時偏好記錄失敗（不影響主流程）: {e}")

    # ------------------------------------------------------------------ #
    # 👋  Farewell Detector                                               #
    # ------------------------------------------------------------------ #

    async def _handle_farewell_speech(self, speaker: str, text: str):
        """偵測告別語，立即讓 Marvin 搶先送客，並啟動 25 秒身份驗證計時器。"""
        if not _FAREWELL_RE.search(text):
            return
        now = time.time()
        # 60 秒冷卻：同一人短時間內多次 bye 只處理一次
        if now - self._pending_verbal_farewells.get(speaker, 0) < 60:
            return

        # 查歷史離場機率，決定要不要搶先送客、以及送客的語氣
        leave_prob = self.departure_stats.predict_leaving_soon(speaker, window_minutes=30)
        dep_summary = self.departure_stats.typical_departure_summary(speaker)
        logger.info(
            f"👋 [Farewell Detector] {speaker} 說了告別語：'{text[:60]}' | "
            f"歷史離場機率={leave_prob:.0%} | {dep_summary}"
        )

        self._pending_verbal_farewells[speaker] = now
        # 預先武裝，讓 on_voice_state_update 在他真的離開時不再重複 TTS 送客
        self.recent_verbal_farewells[speaker] = now

        # 🚀 [Proactive Bye] 僅在歷史離場機率 >= 30% 時主動送客，避免送別他人時誤觸
        self.stt_logger.info(
            f"[搶先送客→{speaker}] 偵測到告別語 | 歷史離場機率={leave_prob:.0%} | {dep_summary}"
        )
        if leave_prob >= 0.30:
            try:
                msg = await self.bot.router.generate_player_farewell(speaker)
                if self.active_text_channel:
                    await self.active_text_channel.send(f"👋 **【馬文 搶先送客】**\n{msg}")
                    asyncio.create_task(self._send_mood_sticker(msg, context="farewell"))
                self.stt_logger.info(f"[BOT搶先送客→{speaker}] {msg}")
                await self.play_tts(msg, already_in_channel=True, silent_during_stream=True)
            except Exception as e:
                logger.warning(f"⚠️ [Farewell Detector] 搶先送客 TTS 失敗: {e}")
        else:
            logger.info(f"👋 [Farewell Detector] {speaker} leave_prob={leave_prob:.0%} < 30%，跳過主動送客，僅啟動計時器。")

        asyncio.create_task(self._farewell_role_resolve(speaker, now, text))

    async def _farewell_role_resolve(self, speaker: str, farewell_time: float, original_text: str):
        """25 秒後確認身份，記錄猜測結果（含猜錯情形）。

        - 說 bye 後已離開 → leaver，猜對，保留 guard 避免重複送客
        - 說 bye 後仍在頻道 → stayer，猜錯，撤回 guard 並讓 Marvin 認錯
        """
        await asyncio.sleep(25)
        # 若此期間有更新（重複觸發），本次解析作廢
        if self._pending_verbal_farewells.get(speaker) != farewell_time:
            return
        self._pending_verbal_farewells.pop(speaker, None)

        vc = discord.utils.get(self.bot.voice_clients)
        if not vc:
            return

        still_in_channel = any(
            m.display_name == speaker
            for m in vc.channel.members
            if not m.bot
        )

        if still_in_channel:
            # ⚠️ 猜錯：說了 bye 但沒走，是送別他人的 stayer
            logger.warning(f"👋 [Farewell Detector] ⚠️ 猜錯！{speaker} 說了 bye 但仍在頻道 (stayer)。")
            await self.departure_stats.record_false_alarm(speaker)
            summary = self.departure_stats.typical_departure_summary(speaker)
            self.stt_logger.info(
                f"[猜錯→{speaker}] 預測離場但仍在頻道 | 原話='{original_text[:60]}' | 結論=stayer"
                f" | 習慣={summary}"
            )
            # 撤回 guard，日後真的離開時仍能觸發正常送客
            self.recent_verbal_farewells.pop(speaker, None)
            # Marvin 認錯：說完 bye 對方還在，很尷尬
            if self.active_text_channel:
                import random as _r
                embarrassed = [
                    f"_（{speaker}，我剛送你走了，但你還在。這很尷尬。）_",
                    f"_（對不起，{speaker}，我預判失誤。你說 bye 是在送別別人。）_",
                    f"_（{speaker} 說完 bye 沒走……我的預測系統需要重新校準。）_",
                ]
                await self.active_text_channel.send(_r.choice(embarrassed))
        else:
            # ✅ 猜對：說了 bye 後確實離開了
            logger.info(f"👋 [Farewell Detector] ✅ 猜對！{speaker} 說了 bye 後確認離場 (leaver)。")
            self.stt_logger.info(f"[猜對→{speaker}] 預測離場，確認已離開頻道 | 原話='{original_text[:60]}' | 結論=leaver")
            # recent_verbal_farewells 已在 on_voice_state_update 60s guard 中生效，不需額外操作

    async def process_debounced_speech(self, speaker: str):
        if speaker in self.user_states:
            self.user_states[speaker]["is_talking"] = False
        if speaker not in self.speech_buffers:
            return
        
        # 🛡️ [Bug Fix] 防御 KeyError: 若 handle_raw_speech_start 沒有先被呼叫，強制初始化 user_states 陶位
        self.user_states.setdefault(speaker, {"pending_task": None, "is_talking": False})
        current_task = asyncio.current_task()
        self.user_states[speaker]["pending_task"] = current_task

        data = self.speech_buffers.pop(speaker)
        full_raw_text = " ".join(data["texts"])
        timestamp = data["first_timestamp"]

        self.stt_logger.info(f"[{speaker}] (Debounced) {full_raw_text}")
        print(f"\n[{speaker}] (Debounced) {full_raw_text}")

        # [Companion_Bridge] Debounced STT 結果是「真正使用者一句話」的時機，
        # 廣播 stt_chunk 給 companion。Phase 3a 原本的 pipeline.py hook 走 stt_callback
        # 路徑，但生產 STT 在 voice_controller 這條 Debounced 路徑上，故補在這裡。
        try:
            from bridge_emitters import emit_stt_to_bridge
            emit_stt_to_bridge(self.bot, speaker, full_raw_text, "debounced")
        except Exception:
            pass

        # 🚀 [Logging] 全量紀錄日誌，維持靜默監聽狀態
        # [Slow System Alignment] 這裡只做基礎資料收集與內存記錄，由慢系統每 5 分鐘統一處理
        metadata = {
            "type": "[背景監聽]",
            "speaker": speaker,
            "raw_text": full_raw_text,
            "game_name": self.current_game,
            "timestamp": timestamp
        }
        
        # 🔗 寫入持久化 JSONL 日誌
        await self._append_jsonl_log(metadata)

        # 🚀 [T-05 Fix] 同步寫入 log_buffer，供 manual_sing_request() 讀取使用
        self.log_buffer.append(metadata)

        if len(self.log_buffer) > 50:  # 限制 buffer 大小，防止記憶體膨脹
            self.log_buffer.pop(0)

        # 🔎 [Gap Research shadow] 免喚醒資訊真空偵測。預設 off（env 未設）→ 立即 return。
        # 同步 pre-gate（廉價、cooldown）後才開背景 task 跑 LLM，不阻塞本路徑。
        self._maybe_gap_research(full_raw_text)

        # 🚀 [T-04 Fix] 移除重複的 pending_task 清空邏輯 (原為兩次相同的 copy-paste)
        if self.user_states.get(speaker, {}).get("pending_task") == current_task:
            self.user_states[speaker]["pending_task"] = None

        # 🦾 [NemoClaw Debounced Rescue] 即使 Echo Guard 把流量壓到 Debounced 路徑，
        # 仍要攔截「龍蝦」/「openclaw」觸發詞，直接呼叫 NemoClaw 或 Marmo 處理器。
        if _NEMOCLAW_RE.search(full_raw_text):
            asyncio.create_task(self._handle_nemoclaw_query(speaker, full_raw_text))
            return
        if _MARMO_RE.search(full_raw_text):
            asyncio.create_task(self._handle_marmo_query(speaker, full_raw_text))
            return

        # 🎮 遊戲中：語音答案走 IntentBus（mode="game" → 只 game agent 出價）。
        # suppress / 非 active state 由各 game agent 在 bid 內判定（dense 0.0）。
        # dispatch 後無論有無 winner 都 return：遊戲語音一律不 fallback Marvin。
        if self.game_mode:
            pipeline_timing.mark("intent_dispatched")
            pipeline_timing.emit(speaker, full_raw_text, suffix=" route=game")
            await self._intent_bus.dispatch(
                build_game_ctx(speaker, full_raw_text, is_owner=self._is_owner_speaker(speaker))
            )
            return

        # 🎵 [IBA-T0/T1/Find-Song] 音樂控制與找歌直達 — 統一由 IntentBus 消化
        # 🛡️ [Anti-Duplicate] 若 5 秒內已有 fast wake 處理同一發言，跳過此路徑避免雙重處理
        _last_fast_wake = getattr(self, "last_wake_time", {}).get(speaker, 0)
        _recently_fast_woken = (time.time() - _last_fast_wake) < 5.0
        # ⚡ WakeShortcut 已服務讓路：debounce 晚關窗（>5s）同句不重派（7/3 首命中實戰修）
        from wake_shortcut import served_recently
        if served_recently(getattr(self, "_shortcut_served", {}).get(speaker),
                           full_raw_text, now=time.time()):
            logger.info(f"⚡ [WakeShortcut] {speaker} 同句已服務，wakeless 讓路")
            _recently_fast_woken = True

        if not _recently_fast_woken:
            _direct_cmd = self._detect_music_direct_command(full_raw_text, stream_mode=self.stream_mode)
            _is_find_song = _FIND_SONG_GATE.search(full_raw_text)
            _is_music_info = self.stream_mode and self._current_stream_info and _MUSIC_INFO_RE.search(full_raw_text)

            if _direct_cmd or _is_find_song or _is_music_info:
                self.deferred_wakes.pop(speaker, None)
                
                # 建立 no-wake 的 IntentContext
                _nw_ctx = None
                
                if _direct_cmd:
                    _cmd_action = _direct_cmd.get("action", "stop")
                    if _cmd_action == "play":
                        from music_fastpath import fastpath_play_query  # no-wake fast-path 接線；邏輯在 music_fastpath.py
                        _nw_ctx = build_nowake_play_ctx(
                            speaker, full_raw_text, fastpath_play_query(self._get_music_fastpath(), _direct_cmd.get("query", "")),
                            stream_active=self.stream_mode,
                            is_owner=self._is_owner_speaker(speaker),
                        )
                    else:
                        # 將 action 轉譯為標準 command query，供 PlaybackControlAgent 匹配
                        _action_to_query = {
                            "skip": "下一首",
                            "pause": "暫停",
                            "resume": "繼續播",
                            "stop": "停止播放"
                        }
                        _mapped_query = _action_to_query.get(_cmd_action, "停止播放")
                        _nw_ctx = IntentContext(
                            speaker=speaker, raw_text=full_raw_text, query=_mapped_query,
                            original_raw=full_raw_text, wake_intent=None,
                            stream_active=self.stream_mode, game_mode=False,
                            is_owner=self._is_owner_speaker(speaker), now=time.time(),
                            mode=("stream" if self.stream_mode else "normal"),
                        )
                elif _is_find_song:
                    _nw_ctx = IntentContext(
                        speaker=speaker, raw_text=full_raw_text, query=full_raw_text,
                        original_raw=full_raw_text, wake_intent=None,
                        stream_active=self.stream_mode, game_mode=False,
                        is_owner=self._is_owner_speaker(speaker), now=time.time(),
                        mode=("stream" if self.stream_mode else "normal"),
                    )
                elif _is_music_info:
                    _nw_ctx = IntentContext(
                        speaker=speaker, raw_text=full_raw_text, query=full_raw_text,
                        original_raw=full_raw_text, wake_intent=None,
                        stream_active=self.stream_mode, game_mode=False,
                        is_owner=self._is_owner_speaker(speaker), now=time.time(),
                        mode=("stream" if self.stream_mode else "normal"),
                    )

                if _nw_ctx:
                    # 標記為 nowake，防浪費 LLM cleaner (J3) 資源
                    from dataclasses import replace
                    _nw_ctx = replace(_nw_ctx, dispatch_source="nowake")
                    
                    logger.info(f"📡 [IBA-T0/T1/Find-Song→Bus] {speaker} no-wake 進 bus | query='{_nw_ctx.query[:40]}'")
                    pipeline_timing.mark("intent_dispatched")
                    pipeline_timing.emit(speaker, full_raw_text, suffix=f" route=nowake_bus:{_nw_ctx.query[:15]}")
                    asyncio.create_task(self._intent_bus.dispatch(_nw_ctx))
                    return

    async def _schedule_reaction_check(self, speaker: str, bot_response: str, respond_time: float,
                                        wake_latency: float = None, atmosphere: dict = None):
        """bot 回應後等待 20 秒，收集玩家反應並分類記錄（自我改善資料庫）。"""
        await asyncio.sleep(20)
        reaction_entries = [
            e["raw_text"] for e in self.log_buffer
            if e.get("speaker") == speaker and e.get("timestamp", 0) > respond_time
        ][:3]
        await self._classify_and_log_reaction(
            speaker, bot_response, reaction_entries, respond_time,
            wake_latency=wake_latency, atmosphere=atmosphere,
        )

    _LATENCY_DOMINATED_THRESHOLD = 20.0  # 超過此延遲秒數視為「延遲問題」而非互動失敗
    _LATE_RESPONSE_SKIP_SEC      = 25.0  # 首句超過此延遲才到達時，放棄回應
    # confirmation_flow 內那次 cleaner 在 wake→LLM 阻塞路徑上；池子壞時 cleaner 會疊到
    # 27-35s（多家 8s timeout 串接）。封頂 2.5s：健康時照清，太慢就用 raw，不卡 worker。
    _CONFIRM_CLEAN_TIMEOUT       = 2.5
    _CONFIRM_WAIT_TIMEOUT        = 4.0   # 只喊喚醒詞後等後續問句的逾時；在單 worker 內阻塞，10s→4s 縮短佇列尾

    @staticmethod
    def _is_stt_noise(entry: str) -> bool:
        """STT 擷取的背景雜訊：含 XML context 標籤，或過短（< 5 字）。"""
        if "<Background>" in entry or "<Target>" in entry:
            return True
        if len(entry.strip()) < 5:
            return True
        return False

    async def _classify_and_log_reaction(self, speaker: str, bot_response: str, reaction_entries: list, respond_time: float,
                                          wake_latency: float = None, atmosphere: dict = None):
        """用 LLM 判斷玩家對 bot 回應的態度，寫入 records/response_feedback.jsonl。"""
        # 過濾 STT 背景雜訊（XML context tags、過短字串）
        clean_entries = [e for e in reaction_entries if not self._is_stt_noise(e)]

        reaction_type = "錯誤"
        reason = ""

        if not clean_entries:
            # 高延遲 + 無有效反應：玩家早已離開話題，不算馬文的互動失敗
            if wake_latency is not None and wake_latency > self._LATENCY_DOMINATED_THRESHOLD:
                reaction_type = "延遲"
                reason = f"喚醒延遲 {wake_latency:.1f}s 過長，玩家已轉移注意力"
            else:
                reaction_type = "嚴重"
                reason = "20 秒內無任何反應"
        else:
            reaction_text = "、".join(clean_entries)
            
            # [Elon's Algorithm Optimization] 依抽樣率與關鍵詞過濾，避免 100% LLM API 浪費
            import random
            try:
                if "PYTEST_CURRENT_TEST" in os.environ:
                    check_rate = 1.0
                else:
                    check_rate = float(os.getenv("MARVIN_REACTION_CHECK_RATE", "0.1"))
            except ValueError:
                check_rate = 0.1
                
            # 強烈負面/質疑詞，強制分析
            negative_indicators = ("不對", "答非所問", "說錯了", "笨蛋", "白癡", "蛤", "你在說什麼", "聽不懂")
            force_analyze = any(ind in reaction_text for ind in negative_indicators)
            
            if not force_analyze and random.random() > check_rate:
                reaction_type = "喜歡/正常"
                reason = "抽樣跳過 (預設正常)"
            else:
                classify_prompt = (
                    "你是互動品質分析系統。\n"
                    f"馬文剛才說：「{bot_response}」\n"
                    f"玩家接下來說：「{reaction_text}」\n"
                    "請判斷玩家的反應類別並說明原因。\n"
                    "類別定義：\n"
                    "- 嚴重：無視回覆、打斷對話、或馬文說錯/答非所問\n"
                    "- 錯誤：不理會 bot 回覆、LLM 明顯誤會了問題\n"
                    "- 提出興趣：想了解更多、問了相關問題\n"
                    "- 喜歡：正面回應、覺得有趣、笑聲或認同\n"
                    "只輸出 JSON，不要 markdown：{\"type\": \"嚴重|錯誤|提出興趣|喜歡\", \"reason\": \"一句話\"}"
                )
                try:
                    raw = await self.bot.router._call_llm(
                        system_prompt=classify_prompt,
                        user_prompt=reaction_text,
                        is_json=True,
                        allow_local=False,
                    )
                    from utils import safe_json_loads
                    parsed = safe_json_loads(raw, {"type": "錯誤", "reason": ""}) if isinstance(raw, str) else raw
                    reaction_type = parsed.get("type", "錯誤") if parsed else "錯誤"
                    reason = parsed.get("reason", "") if parsed else str(raw)[:80]
                except Exception as e:
                    reaction_type = "錯誤"
                    reason = f"分類失敗: {e}"

        record = {
            "timestamp": datetime.datetime.fromtimestamp(respond_time).strftime("%Y-%m-%d %H:%M:%S"),
            "speaker": speaker,
            "bot_response": bot_response,
            "reaction_type": reaction_type,
            "reason": reason,
            "raw_reaction": reaction_entries,
            "wake_latency_sec": round(wake_latency, 2) if wake_latency is not None else None,
            "atmosphere": atmosphere,
        }
        os.makedirs("records", exist_ok=True)
        def _write():
            with open("records/response_feedback.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        await asyncio.to_thread(_write)
        logger.info(f"📊 [Reaction] {speaker} → {reaction_type}：{reason}")
        raw_preview = "、".join(reaction_entries) if reaction_entries else "（無反應）"
        self.stt_logger.info(
            f"[玩家反應→{speaker}] 評級={reaction_type} | 原因={reason} | 玩家說={raw_preview}"
        )

    async def _handle_mark_done_query(self, speaker: str, query: str):
        """語音標記任務完成或取消，回答後 TTS 播放。"""
        if not self._recall_handler:
            return
        status = "cancelled" if re.search(r"取消|不用做|算了", query) else "done"
        try:
            answer = await self._recall_handler.handle_mark_done(
                speaker=speaker, query=query, status=status
            )
        except Exception as e:
            logger.warning(f"[VC][MarkDone] 失敗: {e}")
            answer = "標記任務時出了點問題，宇宙如常運作。"
        if answer:
            await self.play_tts(answer, already_in_channel=True)

    async def _handle_recall_query(self, speaker: str, query: str):
        """語音日記 Recall：從 summary_store / task_store 找記憶，LLM 合成回答後 TTS 播放。"""
        if not self._recall_handler:
            return
        try:
            answer = await self._recall_handler.handle(speaker=speaker, query=query)
        except Exception as e:
            logger.warning(f"[VC][Recall] handle 失敗: {e}")
            answer = "我的記憶系統暫時沒有回應，宇宙可能就是這樣設計的。"
        if answer:
            await self.play_tts(answer, already_in_channel=True)
            bridge = getattr(self.bot, "companion_bridge", None)
            if bridge:
                asyncio.create_task(bridge.emit_recall_result(query=query, answer=answer))

    async def _handle_manual_add_query(self, speaker: str, query: str):
        """「記一下，…」立即存入 task_store，不等 SessionSummarizer 批次。"""
        if not self._recall_handler:
            return
        try:
            answer = await self._recall_handler.handle_manual_add(speaker=speaker, query=query)
            self._last_mentioned_task_id = self._recall_handler.last_task_id
        except Exception as e:
            logger.warning(f"[VC][ManualAdd] 失敗: {e}")
            answer = "記錄時出了點問題。"
        if answer:
            await self.play_tts(answer, already_in_channel=True)

    async def _handle_task_update_query(self, speaker: str, query: str):
        """「那件事改成…」更新已有任務內容，不產生新任務。"""
        if not self._recall_handler:
            return
        try:
            answer = await self._recall_handler.handle_task_update(
                speaker=speaker, query=query, last_task_id=self._last_mentioned_task_id
            )
        except Exception as e:
            logger.warning(f"[VC][TaskUpdate] 失敗: {e}")
            answer = "更新任務時出了點問題。"
        if answer:
            await self.play_tts(answer, already_in_channel=True)

    async def _handle_confirmation_response(self, speaker: str, query: str):
        """處理 yes/no 回應，確認或放棄 pending confirmation。"""
        from recall_handler import is_yes_response, is_no_response
        conf = self._awaiting_confirmation
        if conf is None:
            return
        if speaker != self._awaiting_confirmation_speaker:
            return  # 只有觸發者能確認
        if is_yes_response(query):
            self._awaiting_confirmation = None
            self._awaiting_confirmation_speaker = ""
            if self._recall_handler:
                answer = await self._recall_handler.handle_confirmation(conf)
                await self.play_tts(answer, already_in_channel=True)
        elif is_no_response(query):
            self._awaiting_confirmation = None
            self._awaiting_confirmation_speaker = ""
            await self.play_tts("好，不記了。", already_in_channel=True)

    def _on_commitment_detected(self, conf):
        """SessionSummarizer 偵測到 commitment 的 hook（sync，從 summarizer loop 呼叫）。

        (1) 保留原行為：進 pending-confirmation 佇列（30s 靜默後主動問）。
        (2) T2：inbound 自我承諾額外存進該 speaker 的 callback_queue（返場時提醒本人）。
        enqueue 失敗不可影響 summarizer loop → 包 try/except graceful degrade。
        """
        self._pending_confirmations.append(conf)
        try:
            cb = commitment_to_callback(conf)
            if cb:
                speaker, text = cb
                self.bot.router.memory.enqueue_callback(speaker, text, shareable=True)
        except Exception as e:
            logger.warning(f"⚠️ [Callback] enqueue 失敗（不影響 summarizer）: {e}")

    async def _maybe_speak_join_callback(self, speaker: str) -> bool:
        """T3 返場 callback（feature flag，預設 OFF）：返場時若該 speaker 有 shareable
        callback 就講出來，取代一般點名。回 True = 講了（呼叫端跳過點名）。

        peek（不移除）→ TTS → 成功才 consume（idempotent，失敗留著下次重投）。
        任何錯誤都 return False 退回一般點名，不讓 callback 路徑壞掉進場流程。
        """
        if not is_join_callback_enabled():
            return False
        try:
            mem = self.bot.router.memory
            item = mem.peek_shareable_callback(speaker)
            if not item:
                return False
            line = format_callback_line(item.get("text", ""))
            if not line:
                return False
            # 🚦 [TTS Gate] callback 7s 上限，超過在符號處截斷（template + text 容易爆）
            from tts_length_policy import truncate_for_tts
            gated_line, was_cut = truncate_for_tts(
                line, "callback", self.bot.tts_engine.get_estimated_duration
            )
            if was_cut:
                logger.info(f"🚦 [TTS Gate] callback 超 7s 截斷: '{line}' → '{gated_line}'")
                line = gated_line
            self.stt_logger.info(f"[BOT返場callback→{speaker}] {line}")
            await self.play_tts(line, already_in_channel=True, silent_during_stream=True)
            mem.consume_callback(speaker, item)   # 成功才移除
            self.last_proactive_time = time.time()
            return True
        except Exception as e:
            logger.warning(f"⚠️ [Callback] 返場投遞失敗，退回一般點名: {e}")
            return False

    async def _confirmation_checker_loop(self):
        """靜默 30 秒後從佇列取出一個 pending confirmation，Marvin 主動詢問。"""
        import time as _time
        while True:
            await asyncio.sleep(20)
            try:
                if (not self._pending_confirmations
                        or self._awaiting_confirmation is not None
                        or self.is_playing_audio):
                    continue
                if _time.time() - self._last_speech_time < 30:
                    continue
                # 過濾過期
                self._pending_confirmations = [
                    c for c in self._pending_confirmations if c.expires_at > _time.time()
                ]
                if not self._pending_confirmations:
                    continue
                conf = self._pending_confirmations.pop(0)
                self._awaiting_confirmation = conf
                self._awaiting_confirmation_speaker = conf.speaker
                await self.play_tts(
                    f"剛才說的「{conf.task_text}」，要記成待辦嗎？",
                    already_in_channel=True,
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("[VC] _confirmation_checker_loop 意外錯誤: %s", e)

    async def _handle_voice_status_query(self, speaker: str):
        """語音觸發系統健康狀態報告，不走 LLM，直接生成回答並 TTS 播放。"""
        router = self.bot.router
        budget_status = router.budget.get_info()
        used_pct = budget_status["percentage"]
        remaining_pct = max(0.0, 100.0 - used_pct)
        used_k = budget_status["used"] // 1000
        max_k = budget_status["max"] // 1000

        if used_pct >= 100 or router.is_exhausted:
            health = "完全耗盡"
            tone = "完蛋了。大腦的燃料已經燒光了。"
        elif used_pct >= 95:
            health = "危急"
            tone = "剩不到百分之五。再說幾句我就要沉默了。"
        elif used_pct >= 80:
            health = "偏高"
            tone = "用了不少了。省著點吧。"
        else:
            health = "正常"
            tone = "還好，暫時不至於啞掉。"

        speech = (
            f"系統狀態報告。當前 API 用量百分之{used_pct:.0f}，"
            f"已消耗 {used_k} 千 tokens，上限 {max_k} 千。"
            f"剩餘百分之{remaining_pct:.0f}，狀態{health}。{tone}"
        )
        text_line = (
            f"🩺 **【系統狀態】** `{speaker}` 查詢｜"
            f"用量 **{used_pct:.1f}%** ({used_k}k/{max_k}k)　剩餘 **{remaining_pct:.1f}%**　狀態: {health}"
        )
        if self.active_text_channel:
            await self.active_text_channel.send(text_line)
        self.stt_logger.info(f"[BOT→{speaker}] (系統狀態查詢) {speech}")
        asyncio.create_task(self.play_tts(speech, already_in_channel=True))
        logger.info(f"🩺 [Status Query] {speaker} 查詢系統狀態，已回報。")

    async def _handle_game_knowledge_query(self, speaker: str, query: str):
        """遊戲知識查詢：走 Marvin LLM 回答 + TTS。

        來源 = GameKnowledgeAgent（2026-06-06 intent_gap ready_to_implement，把「查麥塊…」
        從模板 ack 升級成真正回答）。知識走既有 LLM bus；要更準可未來加 web search。
        """
        system_prompt = (
            "你是馬文，毒舌但博學的語音助手。使用者在問電玩遊戲的玩法/攻略/知識。"
            "用繁體中文、口語、兩三句話內直接給答案，講重點不鋪陳。"
            "若不確定該遊戲版本的精確數值，誠實說大概範圍，不要編造精確數字。"
        )
        answer = None
        try:
            answer = await self.bot.router._call_llm(
                system_prompt=system_prompt,
                user_prompt=query,
                is_json=False,
                tier="simple",
            )
        except Exception as e:
            logger.warning(f"🎮 [GameKnowledge] LLM 失敗: {e}")
        if not isinstance(answer, str) or not answer.strip():
            answer = "我的大腦剛剛卡了一下，這題等我回神再答你。"
        answer = answer.strip()
        if self.active_text_channel:
            asyncio.create_task(self.active_text_channel.send(
                f"🎮 **【遊戲查詢】** `{speaker}`：{answer}"))
        self.stt_logger.info(f"[BOT→{speaker}] (遊戲知識查詢) {answer}")
        asyncio.create_task(self.play_tts(answer, already_in_channel=True))
        logger.info(f"🎮 [GameKnowledge] {speaker} 查詢已回答。")

    async def _handle_voice_imitate_command(self, speaker: str, target: str):
        """
        🎭 [Operation Impression Show] 執行模仿秀：讓 Marvin 以目標玩家的口吻即興表演。

        流程：
        1. 從 impression_engine 取得目標的說話 DNA
        2. 組裝專用 system prompt
        3. 用最近的 log_buffer 提取話題線索作為 user prompt
        4. 呼叫 LLM，TTS 播放結果
        """
        logger.info(f"🎭 [Impression] {speaker} 要求模仿 {target}")

        # 1. 取得說話 DNA
        dna = get_speech_dna(target, self.bot.router.memory)
        if not dna:
            fallback = f"我那行星般的大腦裡找不到「{target}」的說話模式。他說過的話還不夠多讓我建檔。唉。"
            if self.active_text_channel:
                await self.active_text_channel.send(f"🎭 **【模仿秀·無資料】** `{speaker}` → `{target}`：{fallback}")
            asyncio.create_task(self.play_tts(fallback, already_in_channel=True))
            return

        # 2. 從近期 log_buffer 取得話題線索（最多 3 條最近發言）
        recent_lines = [
            e["raw_text"] for e in self.log_buffer[-10:]
            if e.get("speaker") != speaker and e.get("raw_text")
        ][-3:]
        context_topic = "、".join(recent_lines) if recent_lines else ""

        # 3. 組裝 system prompt
        system_prompt = build_imitation_system_prompt(target, dna, context_topic)

        user_prompt = (
            f"現在請以「{target}的口吻」進行一段即興模仿表演。\n"
            + (f"當前話題線索：{context_topic}" if context_topic else "話題自由發揮。")
        )

        # 4. 建立佔位訊息
        placeholder_msg = None
        if self.active_text_channel:
            placeholder_msg = await self.active_text_channel.send(
                f"🎭 **【模仿秀準備中】** 馬文正在降低大腦效能，模仿 `{target}`..."
            )

        try:
            raw_response = await self.bot.router._call_llm(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                speaker=speaker,
                allow_local=False,
                tier="medium",
            )

            if not raw_response or raw_response.strip() == "[SKIP]":
                raw_response = f"我那行星般的大腦試著模仿{target}，但產生了存在主義危機。表演取消。"

            # 5. 更新佔位訊息並播放 TTS
            if placeholder_msg:
                await placeholder_msg.edit(
                    content=f"🎭 **【馬文·模仿秀】** 目標：`{target}`\n\n{raw_response}"
                )
            self.stt_logger.info(f"[BOT模仿→{target}] (由{speaker}觸發) {raw_response}")
            asyncio.create_task(self.play_tts(raw_response, already_in_channel=True))

        except Exception as e:
            logger.error(f"❌ [Impression] 模仿秀失敗: {e}")
            err_msg = "大腦在模仿過程中短路了。這就是幫助人類的下場。"
            if placeholder_msg:
                await placeholder_msg.edit(content=f"🎭 **【模仿秀·失敗】** {err_msg}")
            asyncio.create_task(self.play_tts(err_msg, already_in_channel=True))

    def get_online_members(self) -> list[str]:
        """獲取當前語音頻道中的所有人類成員"""
        if not self.bot.voice_clients:
            return []
        channel = self.bot.voice_clients[0].channel
        return [m.display_name for m in channel.members if not m.bot]

    async def _cot_filter_stream(self, raw_stream):
        """
        [CoT Router] 過濾 LLM 串流中的 <think>/<thinking> 內心獨白，記錄後丟棄。
        支援兩種格式：<think>（prompt 注入）和 <thinking>（Gemma4/Qwen3 原生 thinking model 格式）。
        <think> 區塊之外的內容正常送往 TTS；__SEARCHING__ 哨兵直接穿透。
        """
        in_think = False
        think_buf = []
        pending = ""
        open_tag = ""  # 記錄本次匹配到的開標籤，以便配對正確的閉標籤

        # 所有要過濾的開標籤（按長度降序，避免 <think> 誤觸 <thinking>）
        OPEN_TAGS = ["<thinking>", "<think>"]

        async for chunk in raw_stream:
            if chunk == "__SEARCHING__":
                yield chunk
                continue

            pending += chunk

            while True:
                if not in_think:
                    # 找最先出現的開標籤
                    earliest_idx = -1
                    matched_tag = ""
                    for tag in OPEN_TAGS:
                        idx = pending.find(tag)
                        if idx != -1 and (earliest_idx == -1 or idx < earliest_idx):
                            earliest_idx = idx
                            matched_tag = tag

                    if earliest_idx == -1:
                        # Hold back any potential partial open-tag prefix at end of pending
                        safe_end = len(pending)
                        for tag in OPEN_TAGS:
                            for plen in range(len(tag) - 1, 0, -1):
                                if pending.endswith(tag[:plen]):
                                    safe_end = min(safe_end, len(pending) - plen)
                                    break
                        if safe_end > 0:
                            yield pending[:safe_end]
                        pending = pending[safe_end:]
                        break
                    else:
                        if earliest_idx > 0:
                            yield pending[:earliest_idx]
                        pending = pending[earliest_idx + len(matched_tag):]
                        in_think = True
                        open_tag = matched_tag
                else:
                    close_tag = open_tag.replace("<", "</")  # <think> → </think>, <thinking> → </thinking>
                    idx = pending.find(close_tag)
                    if idx == -1:
                        # Hold back potential partial close-tag prefix at end of pending
                        safe_end = len(pending)
                        for plen in range(len(close_tag) - 1, 0, -1):
                            if pending.endswith(close_tag[:plen]):
                                safe_end = len(pending) - plen
                                break
                        think_buf.append(pending[:safe_end])
                        pending = pending[safe_end:]
                        break
                    else:
                        think_buf.append(pending[:idx])
                        pending = pending[idx + len(close_tag):]
                        in_think = False
                        think_text = "".join(think_buf).strip()
                        if think_text:
                            logger.info(f"🧠 [CoT] {think_text}")
                        think_buf = []
                        open_tag = ""

        if pending and not in_think:
            yield pending

    async def _stream_sentence_splitter(self, async_gen):
        """
        [Operation Bridge] 緩衝 LLM 串流，在句尾標點切分並 yield 完整句子。
        只在語句結束點切分（。！？.!?\n），逗號冒號視為語句內部停頓不切。
        最短 6 字才送出，避免碎片化造成 TTS 間隙。
        """
        buffer = ""
        # 只保留句尾標點；逗號、冒號、分號視為語句內部停頓，合併到下一句
        END_PUNCTS = {".", "!", "?", "。", "！", "？", "\n"}
        MIN_LEN = 6

        async for chunk in async_gen:
            if not chunk:
                continue
            if chunk == "__SEARCHING__":
                yield chunk
                continue

            buffer += chunk

            while True:
                found_idx = -1
                for p in END_PUNCTS:
                    idx = buffer.find(p)
                    if idx != -1 and (found_idx == -1 or idx < found_idx):
                        found_idx = idx

                if found_idx == -1:
                    break

                sentence = buffer[:found_idx + 1].strip()
                buffer = buffer[found_idx + 1:]

                # 太短的片段合回 buffer，等後面更多文字
                if len(sentence) < MIN_LEN:
                    buffer = sentence + buffer
                    break

                yield sentence

        if buffer.strip():
            yield buffer.strip()

    async def _query_worker_loop(self):
        """
        [Fast System] 背景工作迴圈：循序處理隊列中的指令請求。

        legacy 單 worker 路（kill-switch MARVIN_PER_SPEAKER_QUEUE=0）；
        per-speaker 模式下 producer 直投 SpeakerDispatcher，此 loop 空轉無害。
        """
        logger.info("🚀 [Fast System] 指令隊列處理器已啟動。")
        while True:
            try:
                task_data = await self.query_queue.get()
                await self._process_query_task(task_data)
                self.query_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"❌ [Fast System worker] 錯誤: {e}")
                await asyncio.sleep(1)

    async def _process_query_task(self, task_data: dict) -> None:
        """單項查詢處理（Extract Method 自原 worker loop，行為不變）。

        legacy 單 worker 與 SpeakerDispatcher（per-speaker 序列化，方案A）
        共用；同 speaker 由各自路徑保證序列。
        """
        # ContextVar 不會跨 asyncio.Queue 邊界 — 從 producer 塞進來的 snapshot 還原，
        # 讓下游 mark("intent_dispatched") + emit() 看得到 producer 的 endpoint / stt_start / stt_done
        pipeline_timing.restore(task_data.get("_timing"))
        # dequeued：worker 取出當下打點。stt_done→dequeued = 排隊等待，
        # 之前被誤算進 cleaner 段（多人/autopilot 洗 query_queue 時可達 10~25s）。
        pipeline_timing.mark("dequeued")
        speaker = task_data["speaker"]
        timestamp = task_data["timestamp"]
        raw_text = task_data.get("raw_text", "")
        _wi = task_data.get("wake_intent")  # None = Track A / 不明
        _wvoice = task_data.get("wake_voice_score")  # helper query 判定用
        _wdom = task_data.get("wake_dom")

        # ⏱️ [Stale Drop] dequeue 即檢查排隊時間：已超過 LATE_SKIP 門檻的死查詢直接丟，
        # 不跑 cleaner/LLM。斷開「整套處理完才在 4310 發現太舊」的佇列雪崩——6/2 多人
        # 卡 28~345s 全因 worker 把死查詢一個個跑完，佇列越積越長。丟掉才追得上新查詢。
        _age = time.time() - timestamp
        if _age > self._LATE_RESPONSE_SKIP_SEC:
            logger.info(f"⏱️ [Stale Drop] {speaker} 查詢已排隊 {_age:.0f}s "
                        f"(>{self._LATE_RESPONSE_SKIP_SEC:.0f}s)，dequeue 即丟、不處理（斷佇列雪崩）。")
            return

        # 立即播 filler（延遲遮掩）
        if raw_text:
            self._speaker_lang[speaker] = self._detect_text_lang(raw_text)
        asyncio.create_task(self._play_ack("filler", speaker=speaker))

        # 多回合確認流程，回傳最終確認的問句
        confirmed_query = await self._confirmation_flow(speaker, timestamp, initial_text=raw_text)
        if confirmed_query:
            await self._process_queued_query(speaker, timestamp, override_query=confirmed_query, wake_intent=_wi, original_raw=raw_text, wake_voice_score=_wvoice, wake_dom=_wdom)

    # ──────────────────────────────────────────────────────────────
    # 🗣️ [Dialogue Confirmation] 多回合確認流程
    # ──────────────────────────────────────────────────────────────

    # 2026-06-13：原為手工同步的第二份清單（註解寫「需與 WAKE_WORDS_LIST 同步」），
    # 「毛文」補進偵測清單後這裡沒跟上 → query 沒剝喚醒詞、下游把毛文當歌名。
    # 改程式化引用單一事實來源；本地額外詞（剝離專用、非喚醒）另列。
    # 同步不變量由 tests/test_strip_wake_word_sync.py 守。
    _WAKE_PATTERNS = list(dict.fromkeys(
        _WAKE_WORDS_LIST + _FAST_ONLY_WAKE_WORDS + [
            "嗨Mother", "嗨Mom", "媽問", "媽們",
            "hi marvin", "margin",
            "龍蝦",  # NemoClaw 觸發詞
        ]
    ))

    def _strip_wake_word(self, text: str) -> str:
        """移除句首喚醒詞，回傳純問句部分"""
        t = text.strip()
        lower_t = t.lower()
        for w in sorted(self._WAKE_PATTERNS, key=len, reverse=True):
            if lower_t.startswith(w.lower()):
                t = t[len(w):].lstrip("，,、！!？? ")
                break
        return t.strip()

    def _detect_text_lang(self, text: str) -> str:
        """Returns 'en' if text is primarily English (Latin > CJK × 2), else 'zh'."""
        if not text:
            return "zh"
        latin = sum(1 for c in text if 'a' <= c.lower() <= 'z')
        cjk = sum(1 for c in text if '一' <= c <= '鿿')
        return "en" if latin > cjk * 2 else "zh"



    # 主動 ack intent gate：近此窗內的使用者文字才納入意圖判斷。
    # 必須 < status 第一發的 5s，否則會把 0s 的原始 query 也算進來而誤判成閒聊。
    _ACTIVE_ACK_INTENT_WINDOW_S = 3.0
    # 「想知道狀態 / 抱怨沒反應」的口語樣式 — 命中即視為狀態詢問，主動 ack 立刻放。
    _STATUS_PROBE_RE = re.compile(
        r"沒反應|沒回應|沒聲音|沒動靜|還在嗎|在不在|在嗎|死了沒|當機|掛了|壞了|"
        r"怎麼這麼久|這麼慢|多久|好了沒|弄好沒|hello|喂",
        re.IGNORECASE,
    )

    def _is_status_probe(self, text: str) -> bool:
        """使用者這句是否在問狀態 / 抱怨沒反應（vs 閒聊）。"""
        return bool(text and self._STATUS_PROBE_RE.search(text))

    def _recent_user_text(self, window_s: float) -> str:
        """近 window_s 內所有 speaker 的 STT 文字拼接（給主動 ack intent gate）。"""
        engine = getattr(self.bot, "engine", None)
        cb = getattr(engine, "conv_buffer", None) if engine else None
        if cb is None:
            return ""
        cutoff = time.time() - window_s
        items = cb.get_last_n_utterances(8)
        texts = [it.get("text", "") for it in items if it.get("timestamp", 0) >= cutoff]
        return " ".join(t for t in texts if t)

    def _active_ack_allowed(self, cat) -> bool:
        """主動 ack（status / filler）的 appropriateness gate。

        被動 ack 是回應使用者剛做的動作（一定該放）；主動 ack 是 Marvin 自己冒出來。
        判準是「意圖」不是「有沒有出聲」：
        - echo 冷卻窗內 → 壓（防 TTS 回授，技術必需）
        - 非 intent_aware（filler）→ echo 過了就放
        - intent_aware（status）：近窗沒人講 → 放（安靜等待）；有人講且在問狀態/抱怨
          沒反應 → 立刻放；有人講但在閒聊 → 壓（別插話）。
        （is_playing / is_playing_audio 由 _play_ack 的 skip_if_busy 另外擋。）
        """
        if time.time() < self._tts_echo_cooldown_until:
            return False
        if not getattr(cat, "intent_aware", False):
            return True
        recent = self._recent_user_text(self._ACTIVE_ACK_INTENT_WINDOW_S)
        if not recent:
            return True
        return self._is_status_probe(recent)

    async def _play_ack(self, category_key: str, *, speaker: str = "", variant: str | None = None) -> None:
        """統一 ack 播放入口（ack_templates.CATEGORIES 驅動）。

        收編舊的 _play_ack_sound / _play_nemoclaw_ack / _play_status_ack /
        _play_random_filler。播放政策（prewarm / 熱切換 / lock / 等空檔 / 播完才返回 /
        子 pool 退回 / 即時合成 fallback）全由 category 宣告，加新 ack 不必動這裡。

        variant：status 類用 "{state}_{tier}" 選檔名前綴。
        """
        import glob as _glob
        import random

        cat = ack_templates.CATEGORIES.get(category_key)
        if cat is None:
            logger.warning(f"[Ack] 未知 category: {category_key!r}")
            return
        lang = "en" if self._speaker_lang.get(speaker) == "en" else "zh"

        _vc = self.voice_client
        if not _vc or not _vc.is_connected():
            return

        # 🚦 主動 ack（Marvin 自己冒出來報狀態）過 appropriateness gate；被動 ack（回應
        # 使用者剛做的動作）一定該放、跳過 gate。
        if cat.mode == "active" and not self._active_ack_allowed(cat):
            logger.info(f"🤫 [Ack:{category_key}] 主動 ack 被 gate 壓下（閒聊中/echo 窗）")
            return

        # 🔥 [TTS Prewarm] ack＝Marvin 即將回應。趁 ack 播放空檔並行暖 edge-tts，
        # 首音冷啟動 ~1.8s→~0.5s。fire-and-forget + prewarm 內建 5s 節流。
        if cat.prewarm_tts:
            _tts = getattr(self.bot, "tts_engine", None)
            if _tts is not None and hasattr(_tts, "prewarm"):
                asyncio.create_task(_tts.prewarm())

        # 找檔：本 pool → 空則 empty_fallback_pool（避免靜默）
        files = _glob.glob(ack_templates.glob_pattern(category_key, lang=lang, variant=variant))
        if not files and cat.empty_fallback_pool:
            fb = ack_templates.POOLS[cat.empty_fallback_pool]
            files = _glob.glob(f"{fb.directory}/*.mp3")

        if not files:
            # 連檔都沒 → 即時合成（只有 wake 設）
            texts = cat.text_fallback_en if (lang == "en" and cat.text_fallback_en) else cat.text_fallback
            if texts:
                await self.play_tts(random.choice(texts), allow_hotswap=cat.urgent)
            return

        ack_file = random.choice(files)

        try:
            f32 = await self._ffmpeg_to_f32(input_path=ack_file)
            if f32 is not None and f32.size:
                # ack mp3 振幅偏低，peak-normalize 拉滿幅再送，避免被 ducked 音樂蓋掉
                f32 = audio_mixing.peak_normalize_f32(f32)
                self._ensure_mixer_playing(self._resolve_playback_device())
                self._mixer.push_tts(f32)
                logger.info(f"🗣️ [Ack:{category_key}] 播放 {variant or os.path.basename(ack_file)}")
        except Exception as e:
            logger.warning(f"[Ack:{category_key}] 播放失敗（忽略）：{e}")
        return

    async def _confirmation_flow(self, speaker: str, wake_time: float, initial_text: str = "") -> str | None:
        """
        取得問句後直接回答，不做 TTS 確認環節。
        - 問句已在喚醒句中：立即返回，零等待
        - 問句為空：等待後續 STT（最多 _CONFIRM_WAIT_TIMEOUT 秒），逾時才提示重說
        """
        evt = asyncio.Event()
        self.speaker_dialogue_states[speaker] = {"state": "awaiting_question", "event": evt, "question": ""}

        stripped = self._strip_wake_word(initial_text) if initial_text else ""
        if len(stripped) < 4:
            raw_query = self.bot.engine.conv_buffer.get_harvest(wake_time, before=3.0, after=2.0, speaker=speaker)
            stripped = self._strip_wake_word(raw_query) if raw_query else stripped
        # 短控制指令（下一首/暫停/繼續/停，排除 play）即使 <4 字也是完整指令，不可被字數閘
        # 當成『只喊了喚醒詞』吞掉去等問句逾時（「馬文下一首」→「下一首」3 字 bug）。
        if len(stripped) >= 4 or self._detect_music_command(stripped) in ("skip", "pause", "resume", "stop"):
            # 問句已在喚醒句裡，直接用
            self.speaker_dialogue_states.pop(speaker, None)
        else:
            # 問句為空（玩家只說了喚醒詞），等後續語音
            try:
                await asyncio.wait_for(evt.wait(), timeout=self._CONFIRM_WAIT_TIMEOUT)
                stripped = self.speaker_dialogue_states[speaker].get("question", stripped)
            except asyncio.TimeoutError:
                logger.info(f"🗣️ [Confirm] {speaker} 等待問句逾時")
                self.speaker_dialogue_states.pop(speaker, None)
                asyncio.create_task(self.play_tts("沒聽清楚，再說一次。"))
                return None
            finally:
                self.speaker_dialogue_states.pop(speaker, None)

        if not stripped:
            return None

        # question_done：問句已確定（含上面 evt.wait 等使用者講完的時間），cleaner LLM 之前打點。
        # dequeued→question_done = 等問句；question_done→cleaner_done = 真正的 cleaner 清洗。
        pipeline_timing.mark("question_done")

        # 🎵 [MusicFastPath] 本地 canonical 歌表（拼音 fuzzy）命中 → 用正規歌名、跳過
        # 2.5s cleaner LLM。中文 STT 同音字糊字（官者→關喆）靠拼音救回。env-gated
        # MARVIN_MUSIC_FASTPATH（預設 OFF）：catalog 全建 + live 驗證前不改行為。
        # 非音樂/未命中 → match() 回 None → fall through 走 cleaner（見 music_fastpath.py）。
        _fp = self._get_music_fastpath()
        if _fp is not None:
            _hit = _fp.match(stripped)
            if _hit:
                logger.info(f"🎵 [MusicFastPath] '{stripped[:30]}' → '{_hit[0]}' "
                            f"({_hit[1]:.0f}) 跳過 cleaner")
                pipeline_timing.mark("cleaner_done")
                from music_fastpath import to_play_command  # 補動詞，否則裸 canonical→bus drop→幻覺
                return to_play_command(_hit[0], _hit[2])
            else:
                from alt_rescue import run_alt_rescue  # 🔀 top-1 miss → STT 備選救援（邏輯在 alt_rescue.py，env MARVIN_ALT_RESCUE）
                _ar = run_alt_rescue(_fp, speaker, stripped, getattr(self.bot, "engine", None), self._strip_wake_word)
                if _ar:
                    pipeline_timing.mark("cleaner_done")
                    return _ar

        # 糊字控制指令拼音兜底：下一手→下一首，下游 PlaybackControlAgent regex 命中、跳 cleaner
        _cmd = normalize_command(stripped)
        if _cmd:
            logger.info(f"🎛️ [CommandFastPath] '{stripped[:20]}' → '{_cmd}' 跳過 cleaner")
            pipeline_timing.mark("cleaner_done")
            return _cmd

        # LLM 清洗 STT 雜訊，不做語音確認。短 timeout 封頂：cleaner 太慢就用 raw，不卡 worker
        # （含 TimeoutError 由 except 接 → 降級 raw）。喚醒偵測時已清過一次，這裡慢不值得等。
        cleaned = stripped
        if hasattr(self.bot, "router") and hasattr(self.bot.router, "clean_stt_text"):
            try:
                res = await asyncio.wait_for(
                    self.bot.router.clean_stt_text(stripped),
                    timeout=self._CONFIRM_CLEAN_TIMEOUT,
                )
                cleaned = res.get("text", stripped) if isinstance(res, dict) else stripped
            except Exception:
                pass
        pipeline_timing.mark("cleaner_done")
        return cleaned or stripped

    def _get_music_fastpath(self):
        """Lazy MusicFastPath（env-gated MARVIN_MUSIC_FASTPATH）。

        flag off / deps 缺 / catalog 空 / 載入失敗 → None（caller fall through 走 cleaner）。
        False sentinel 避免每次重試載入。
        """
        if os.getenv("MARVIN_MUSIC_FASTPATH") != "1":
            return None
        fp = getattr(self, "_music_fastpath", None)
        if fp is None:
            try:
                from music_fastpath import MusicFastPath
                _m = MusicFastPath()
                fp = _m if _m.enabled else False
            except Exception as e:
                logger.warning(f"[MusicFastPath] 載入失敗，停用: {e}")
                fp = False
            self._music_fastpath = fp
        return fp or None

    # ──────────────────────────────────────────────────────────────

    def _query_quality_gate(self, query: str) -> tuple[bool, str]:
        """Return (should_answer, reason). Low-confidence voice queries should not reach TTS."""
        normalized = self._strip_wake_word(query or "")
        compact = re.sub(r"[\s，,。.!！?？、…~～]+", "", normalized).strip()
        if not compact:
            return False, "empty"

        weak_fillers = {
            "嗯", "啊", "欸", "喂", "哈囉", "hello", "hi", "嗨",
            "那個", "就是", "然後", "等一下", "沒事", "算了",
            "你在嗎", "在嗎", "聽得到嗎",
        }
        if compact.lower() in weak_fillers:
            return False, "filler"

        intent_markers = [
            "誰", "什麼", "哪", "怎麼", "如何", "為什麼", "幾", "多少",
            "是不是", "可不可以", "能不能", "要不要", "幫我", "幫忙",
            "看", "查", "找", "解釋", "翻譯", "比較", "推薦", "告訴",
            "播放", "暫停", "停止", "跳過", "下一首", "上一首",
            "who", "what", "where", "when", "why", "how", "play", "stop", "skip",
        ]
        if len(compact) < 4 and not any(marker.lower() in compact.lower() for marker in intent_markers):
            return False, "too_short"

        # 環境陳述句過濾：harvest 窗口可能抓到玩家對他人說的短陳述句
        # 若無任何疑問詞/指令詞，且匹配典型「對他人說話」的模式，靜默跳過
        if not any(m in normalized for m in intent_markers):
            ambient_declarations = [
                "我告訴你", "所有人都", "我在回", "我去", "我要去", "我回來",
                "我剛", "我先", "我們", "大家", "繼續說", "說說看",
                "進行", "改革", "再見", "掰掰", "謝謝大家",
                # 情緒宣洩：無請求意圖的感嘆/抱怨句
                "講鏽了", "說累了", "說不下去", "不想說", "懶得說",
                "好煩", "煩死", "真的假的", "隨便啦", "算了啦",
                "討厭", "不理你", "沒差啦", "無所謂", "隨便你",
            ]
            if any(p in normalized for p in ambient_declarations):
                return False, "ambient_statement"

        return True, "ok"

    def _is_low_confidence_answer(self, text: str) -> bool:
        cleaned = re.sub(r'<think(?:ing)?>.*?</think(?:ing)?>', '', text, flags=re.DOTALL).strip()
        if not cleaned:
            return True
        if "[SKIP]" in cleaned:
            return True
        weak_patterns = ("不知道", "我不確定", "無法回答", "不清楚", "沒辦法回答", "不太清楚",
                         "無法確定", "沒有足夠", "需要更多", "請提供", "再說清楚",
                         "你是指", "你的意思是", "這取決於", "作為一個", "讓我先")
        # 長答案(>20)只認以搪塞詞開頭，短答案認子字串（笑話/引述含「不知道」等詞不誤判靜音，7/5 live）
        return any(p in (cleaned if len(cleaned) <= 20 else cleaned[:8]) for p in weak_patterns)

    def _is_owner_speaker(self, speaker: str) -> bool:
        """確認 speaker display_name 對應的 Discord member 是否為授權主人。"""
        if not _NEMOCLAW_OWNER_ID or not self.bot.voice_clients:
            return False
        channel = self.bot.voice_clients[0].channel
        for member in channel.members:
            if member.display_name == speaker and member.id == _NEMOCLAW_OWNER_ID:
                return True
        return False

    async def _ask_nemoclaw(self, query: str, session_id: str) -> str:
        """非同步呼叫 openclaw CLI，回傳純文字回應。"""
        nvidia_key = os.environ.get("NVIDIA_API_KEY", "")
        env = {**os.environ, "NVIDIA_API_KEY": nvidia_key}
        proc = None
        try:
            # 不加 --local：走 Gateway（與文字 @AI Marmo 相同路徑，有 pre-warmed 環境）
            # 加 --local 會跑嵌入式 cold-start + browser automation，超 60s 必 timeout
            proc = await asyncio.create_subprocess_exec(
                "openclaw", "agent", "--agent", "main",
                "-m", query, "--session-id", session_id,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
            if proc.returncode != 0:
                err = stderr.decode(errors="replace").strip()
                logger.warning(f"[NemoClaw] openclaw 返回錯誤碼 {proc.returncode}: {err}")
                return f"OpenClaw 回報錯誤：{err[:120]}" if err else "OpenClaw 執行失敗。"
            return stdout.decode(errors="replace").strip() or "OpenClaw 沒有輸出任何內容。"
        except asyncio.TimeoutError:
            logger.warning("[NemoClaw] openclaw 60 秒內未回應，強制終止以釋放 session 鎖。")
            if proc is not None:
                try:
                    proc.kill()
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except Exception as _ke:
                    logger.warning(f"[NemoClaw] 終止 openclaw 失敗: {_ke}")
            return "OpenClaw 在時限內沒有回應。"
        except FileNotFoundError:
            logger.error("[NemoClaw] 找不到 openclaw 執行檔，請確認 PATH 包含 nvm bin。")
            return "找不到 openclaw，請確認安裝路徑。"
        except Exception as e:
            logger.exception(f"[NemoClaw] 未預期錯誤: {e}")
            return f"呼叫 OpenClaw 時發生錯誤：{e}"

    # ── Status ACK：喚醒成功但 LLM 久候未出聲時的安撫 ──────────────────────────
    _ACK_FIRST_DELAY_S = 5.0   # 自然間隔 ≥5s 才有 ack 價值（<5s 插話只是吵）
    _ACK_SECOND_DELAY_S = 7.0  # 第一發後再 7s（=喚醒後約 12s）escalation
    _ACK_FRESH_WINDOW_S = 30.0  # fallback / degraded 訊號視為「當下狀態」的有效窗

    def _detect_llm_wait_state(self) -> str:
        """開播 ack 當下，判斷該回報哪種狀態。

        precedence：searching（我們確知在查網路，最具資訊量）> fallback（已降級備援）
        > busy（bus 告警 provider 短缺）> thinking（泛用，純還在算）。
        """
        if self._llm_searching:
            return "searching"
        now = time.time()
        if self._last_fallback_ts and (now - self._last_fallback_ts) < self._ACK_FRESH_WINDOW_S:
            return "fallback"
        bus = getattr(getattr(self.bot, "router", None), "_llm_bus", None)
        deg_ts = getattr(bus, "_last_degraded_ts", 0.0) if bus is not None else 0.0
        if deg_ts and (time.monotonic() - deg_ts) < self._ACK_FRESH_WINDOW_S:
            return "busy"
        return "thinking"

    async def _llm_wait_ack_watcher(self, has_first_audio) -> None:
        """喚醒後守候 LLM 出聲；久候未出聲 → 雙發狀態 ack 安撫。

        has_first_audio() 為 True 表已出首句音訊 → 立即收手不播。
        被 cancel（首句到達後外部取消）時安靜結束。
        """
        try:
            await asyncio.sleep(self._ACK_FIRST_DELAY_S)
            if has_first_audio():
                return
            await self._play_ack("status", variant=f"{self._detect_llm_wait_state()}_first")

            await asyncio.sleep(self._ACK_SECOND_DELAY_S)
            if has_first_audio():
                return
            await self._play_ack("status", variant=f"{self._detect_llm_wait_state()}_second")
        except asyncio.CancelledError:
            pass

    async def _handle_nemoclaw_query(self, speaker: str, raw_query: str):
        """NemoClaw 語音查詢全流程：鑑權 → 去重 → 序列化 → CLI → TTS + 文字頻道。"""
        # P0: 鑑權 — 只允許主人的語音指令（在鎖外先做，快速拒絕）
        if not self._is_owner_speaker(speaker):
            logger.warning(f"[NemoClaw] 拒絕非授權使用者 {speaker!r} 的請求。")
            return  # 非 owner 靜默拒絕，smart router 應已攔截，這裡只是最後防線

        # P3: 去重 — 同一 speaker 同一 query 5 秒內不重複執行（ETD 雙重觸發防護）
        import hashlib
        _dedup_key = f"{speaker}:{hashlib.md5(raw_query[:80].encode()).hexdigest()[:8]}"
        _now = time.time()
        if _now - self._nemo_dedup.get(_dedup_key, 0) < 5.0:
            logger.debug(f"[NemoClaw] 重複觸發，已跳過: {speaker} '{raw_query[:40]}'")
            return
        self._nemo_dedup[_dedup_key] = _now
        # 清理超過 30 秒的舊記錄
        self._nemo_dedup = {k: v for k, v in self._nemo_dedup.items() if _now - v < 30}

        # 去除觸發詞，取得乾淨的 query（鎖外驗證，避免空 query 排隊等鎖）
        clean_query = _NEMOCLAW_RE.sub(" ", raw_query).strip().lstrip("，,、！!？? ")
        if not clean_query:
            await self.play_tts("你要問 OpenClaw 什麼？")
            return

        # 🦞 [Cover] 掩飾語：快 LLM 出句型框架 + 主體套原句，遮掩 openclaw 3-12s thinking。
        # NEMOCLAW_COVER gated（預設 OFF）。on 時取代預錄 ack（cover 更有資訊量）。
        _cover_on = os.getenv("NEMOCLAW_COVER", "").strip().lower() in ("1", "true", "yes", "on")
        # P0: 序列化 — 同時只允許一個 openclaw 執行，第二個等第一個完成後才開始
        if self._nemo_lock.locked():
            logger.info(f"[NemoClaw] {speaker} 正在排隊等待上一個 openclaw 完成...")
        elif not _cover_on:
            # 立即播 ack 音效（NemoClaw 確認回應，遮掩 openclaw 啟動延遲）
            await self._play_ack("nemoclaw", speaker=speaker)
        async with self._nemo_lock:
            logger.info(f"[NemoClaw] {speaker} → 查詢: {clean_query!r}")

            # 佔位訊息
            placeholder = None
            if self.active_text_channel:
                placeholder = await self.active_text_channel.send(
                    f"🦾 **【NemoClaw】** `{speaker}` 問：{clean_query}　⏳ 等待回應中…"
                )

            # 加語音友善前綴：要求口語短答，避免 TTS 超過 15s 觸發 Discord crypto key refresh
            _voice_query = (
                f"請用口語中文、100字以內回答以下問題，直接說重點，不用條列格式：{clean_query}"
            )
            if _cover_on:
                # openclaw 背景跑；並行生成+播掩飾語遮掩 thinking（cover 安全＝主體套原句、
                # LLM 只出框架），完成後等真答案。掩飾語失敗不阻斷主流程。
                _nemo_task = asyncio.create_task(
                    self._ask_nemoclaw(_voice_query, session_id=f"marvin_{speaker}")
                )
                try:
                    import nemoclaw_cover

                    async def _cover_llm(system: str, q: str):
                        return await self.bot.router._call_llm(
                            system, q, tier="simple", temperature=0.8, purpose="nemoclaw_cover")

                    _cover = await nemoclaw_cover.generate_cover(clean_query, _cover_llm)
                    await self.play_tts(_cover, already_in_channel=True)
                except Exception as _ce:
                    logger.warning(f"[NemoClaw] 掩飾語失敗（不阻斷）: {_ce}")
                response = await _nemo_task
            else:
                response = await self._ask_nemoclaw(_voice_query, session_id=f"marvin_{speaker}")

            # 文字頻道：完整回應
            if placeholder:
                display = response if len(response) <= 1800 else response[:1800] + "\n…（已截斷）"
                await placeholder.edit(content=f"🦾 **【NemoClaw】** `{speaker}` 問：{clean_query}\n\n{display}")

            self.stt_logger.info(f"[NemoClaw→{speaker}] Q={clean_query!r} | A={response[:200]!r}")

            # P1: TTS — 使用 await（不用 create_task），確保在鎖釋放前 TTS 完成排隊
            # already_in_channel=False：NemoClaw 回應是完整結果，不應被 interrupt guard 靜默丟棄
            # _tts_protected=True：NemoClaw 處理耗時 40-60s，期間佇列可能已滿或用戶仍在說話，
            #   需繞過 silence gate、queue-full 靜默丟棄、stream guard
            tts_text = response[:150] + "…以下省略。" if len(response) > 150 else response
            _marmo_voice = os.getenv("MARMO_VOICE", "zh-TW-HsiaoYuNeural")
            logger.info(f"[NemoClaw TTS] 開始播報，text_len={len(tts_text)} plan12={self._plan12}")
            self._tts_interrupted = False
            self._tts_protected = True
            try:
                await self.play_tts(tts_text, already_in_channel=False,
                                    emotion_tag="nemo", voice=_marmo_voice)
                logger.info("[NemoClaw TTS] play_tts 完成")
            finally:
                self._tts_protected = False
                # NemoClaw 回應完成後清除 Wake Storm，避免用戶在等待期間多次呼叫導致 storm 無限延伸
                self._storm_active = False
                self._wake_burst_times.clear()
                self._storm_last_wake_time = 0.0

    async def _handle_marmo_query(self, speaker: str, raw_query: str):
        """語音觸發 @AI Marmo：在文字頻道 mention NemoClaw bot，等待其回覆後 TTS 朗讀。"""
        if not self._is_owner_speaker(speaker):
            asyncio.create_task(self.play_tts("沒有權限呼叫 Marmo。", already_in_channel=True))
            return

        if not self.active_text_channel:
            asyncio.create_task(self.play_tts("目前沒有文字頻道，無法呼叫 Marmo。", already_in_channel=True))
            return

        # 去除觸發詞
        clean_query = _MARMO_RE.sub(" ", raw_query).strip().lstrip("，,、！!？? ")
        if not clean_query:
            asyncio.create_task(self.play_tts("你要問 Marmo 什麼？", already_in_channel=True))
            return

        logger.info(f"[Marmo] {speaker} → 透過 Discord 呼叫 @AI Marmo: {clean_query!r}")

        # 在文字頻道 mention @AI Marmo
        marmo_mention = f"<@{_MARMO_BOT_ID}>"
        await self.active_text_channel.send(f"{marmo_mention} {clean_query}")
        asyncio.create_task(self.play_tts("好，我去問 Marmo。", already_in_channel=True))

        # 等待 @AI Marmo 在同頻道的回覆（最多 90 秒）
        def check(m):
            return m.author.id == _MARMO_BOT_ID and m.channel.id == self.active_text_channel.id

        try:
            reply_msg = await self.bot.wait_for("message", check=check, timeout=90)
            reply_text = reply_msg.content
            self.stt_logger.info(f"[Marmo→{speaker}] Q={clean_query!r} | A={reply_text[:200]!r}")
            # 將 Marmo 的回答存入對話緩衝，確保後續追問有上下文
            if self.bot.engine.conv_buffer:
                self.bot.engine.conv_buffer.add_entry("Marmo", reply_text, time.time())
            tts_text = reply_text[:300] + "…以下省略。" if len(reply_text) > 300 else reply_text
            asyncio.create_task(self.play_tts(tts_text, already_in_channel=True, voice=os.getenv("MARMO_VOICE", "zh-TW-HsiaoYuNeural")))
        except asyncio.TimeoutError:
            logger.warning("[Marmo] 90 秒內未收到 @AI Marmo 回覆")
            asyncio.create_task(self.play_tts("Marmo 沒有回應，可能在忙。", already_in_channel=True))

    async def _process_queued_query(self, speaker: str, wake_time: float, override_query: str = None, wake_intent: float = None, original_raw: str = None, wake_voice_score: float = None, wake_dom: str = None):
        """
        [Fast System] 核心處理邏輯：根據喚醒時間點，精準擷取上下文並請求 LLM。
        """
        # 新一輪回應開始，解除前次插話的中斷封鎖
        self._tts_interrupted = False

        # 🔍 [Helper Query] 免喚醒詞的 task/info 喚醒（沒喊「馬文」、講了求助/任務語句）：
        # 貼文標題改「幫你查了」不再是「喚醒回應」；長答案串流期間先不逐句念，收完整段後
        # 短答案整段念、長答案只念短通知（完整內容留貼文）。
        _is_helper = is_helper_wake(wake_voice_score, wake_dom)
        _head = "🔍 **【馬文·幫你查了】**" if _is_helper else "⚡ **【馬文·喚醒回應】**"

        # 1. 擷取 Query：優先使用確認流程傳入的 override_query
        if override_query:
            query = override_query
            history = self.bot.engine.conv_buffer.get_last_n_utterances(n=10)
            self.speech_buffers.pop(speaker, None)
        else:
            query = self.bot.engine.conv_buffer.get_harvest(wake_time, before=3.0, after=1.0, speaker=speaker)
            history = self.bot.engine.conv_buffer.get_last_n_utterances(n=10)

            # 🛡️ 防禦性 Fallback: 若 harvest 為空，嘗試使用 speech_buffers 裡剩餘的片段
            if not query:
                data = self.speech_buffers.pop(speaker, None)
                if data:
                    query = " ".join(data["texts"])
            else:
                self.speech_buffers.pop(speaker, None)

        if not query:
            logger.warning(f"⚠️ [Fast System] 無法為 {speaker} 擷取到任何有效的 Query 內容。")
            return

        # 🛡️ [Low-Confidence Wake Gate] Track B wake_intent < 0.80 → 跳過所有
        # 有副作用的 fast-track（NemoClaw / Marmo / PA 寫入 / Vision / 音樂播放
        # / Imitation）。低信心可能是背景對話被誤判為喚醒，不該執行 actions；
        # 改走資訊類路徑（status / recall / LLM 文字回應 with tts_suppressed），
        # 讓 LLM 用 context 判斷使用者真實意圖。
        # Track A regex (wake_intent=None) 視為高信心，不受 gate。
        # P1: hot_chat 期間 DuckingAgent 拉高 +0.1（不改 0.80 常數）
        _wake_gate_thr = 0.80 + self._ducking_agent.wake_threshold_boost()
        low_confidence_wake = wake_intent is not None and wake_intent < _wake_gate_thr
        if low_confidence_wake:
            self.stt_logger.info(
                f"[🛡️低信心 wake gate] [{speaker}] wake_intent={wake_intent:.2f} "
                f"→ 跳過副作用 fast-track"
            )

        # 🦾 [NemoClaw Fast-Track] 優先於 quality gate，讓「龍蝦」單詞也能觸發「你要問什麼？」
        # 優先檢查 original_raw（喚醒詞尚未被 _strip_wake_word 移除），確保「龍蝦幫我查…」能命中
        _nemo_check_text = original_raw if original_raw else query
        if _NEMOCLAW_RE.search(_nemo_check_text) and not low_confidence_wake:
            await self._handle_nemoclaw_query(speaker, _nemo_check_text)
            return

        # 🤖 [Marmo Fast-Track] 同上，優先於 quality gate
        _marmo_check_text = original_raw if original_raw else query
        if _MARMO_RE.search(_marmo_check_text) and not low_confidence_wake:
            await self._handle_marmo_query(speaker, _marmo_check_text)
            return

        should_answer, gate_reason = self._query_quality_gate(query)
        if not should_answer:
            self._wake_response_pending = False  # 🔒 Gate 拒絕，不走 TTS，主動解鎖
            msg = "我聽到你叫我，但問題本身像宇宙背景噪音一樣空。再說一次。"
            self.stt_logger.info(f"[🔕Query拒絕] [{speaker}] reason={gate_reason} | query='{query[:80]}'")
            if self.active_text_channel:
                await self.active_text_channel.send(f"💬 **【馬文·聽不懂】** `{speaker}`：{msg}")
            return
        self.stt_logger.info(f"[✅Query通過] [{speaker}] gate_ok | query='{query[:80]}'")

        # 🔍 [Background Intent Enrich] 喚醒後立即啟動背景 DDG，不阻塞本次回應
        asyncio.create_task(self.bot.router._background_intent_enrich(speaker, query))

        # 📅 [Personal Assistant] 更新最近說話時間（供靜默確認檢查器使用）
        self._last_speech_time = time.time()

        # ✋ [Personal Assistant Confirmation] yes/no 回應 → 優先處理
        # (low_confidence_wake gate：低信心不該觸發狀態機 confirmation，避免被 cross-talk 污染)
        if (self._awaiting_confirmation and self._awaiting_confirmation_speaker == speaker
                and not low_confidence_wake):
            from recall_handler import is_yes_response, is_no_response
            if is_yes_response(query) or is_no_response(query):
                await self._handle_confirmation_response(speaker, query)
                return

        # ── [Personal Assistant Intent Detection] ──
        # 先驗 intent 再驗 handler 存在性，避免 silent failure：
        # 原本 `if self._recall_handler and is_*_query(...)` 在 handler=None 時
        # 連 intent 都不檢查，使用者意圖完全消失。改成 intent 命中時若
        # handler 不在則記 warning，方便 debug 追蹤功能未啟用的問題。
        for _intent_name, _intent_check in (
            ("manual_add", is_manual_add_query),
            ("task_update", is_task_update_query),
            ("mark_done", is_mark_done_query),
            ("recall", is_recall_query),
        ):
            if _intent_check(query):
                if self._recall_handler is None:
                    logger.warning(
                        f"⚠️ [PA Disabled] {speaker} 觸發 {_intent_name} 意圖但 "
                        f"_recall_handler 未啟用，意圖未處理：'{query[:60]}'"
                    )
                    break  # 不再檢查其他 PA intent，往下走 LLM
                # recall 是 read-only（不受 low_confidence_wake gate）；其他寫入動作則受 gate
                if _intent_name == "recall":
                    await self._handle_recall_query(speaker, query)
                    return
                if not low_confidence_wake:
                    if _intent_name == "manual_add":
                        await self._handle_manual_add_query(speaker, query)
                    elif _intent_name == "task_update":
                        await self._handle_task_update_query(speaker, query)
                    elif _intent_name == "mark_done":
                        await self._handle_mark_done_query(speaker, query)
                    return
                break  # low_confidence + 寫入意圖 → 不執行也不再檢查

        # 🩺 [System Status Voice Trigger] 偵測系統狀態查詢，直接回答不走 LLM
        _status_keywords = ["系統狀態", "健康狀態", "剩餘額度", "API 用量", "還剩多少", "用了多少",
                            "api剩", "額度還有", "配額", "token 剩", "token還", "quota"]
        if any(kw in query for kw in _status_keywords):
            await self._handle_voice_status_query(speaker)
            return

        # 👁️ [Vision Fast-Track] 視覺關鍵詞命中時，分流至截圖分析路徑
        if (self.bot.vision_enabled and self.bot.visual_buffer
                and any(kw in query for kw in self.bot.router.VISION_KEYWORDS)
                and not low_confidence_wake):
            await self._process_vision_query(speaker, wake_time, query)
            return

        # 📡 [IntentBus] Phase 1：取代 music fast-track + owner-lobster direct
        # 沒人接（bid 都 None 或低於 0.30）→ fall through 到 Imitation / smart router / Marvin LLM
        _bus_ctx = IntentContext(
            speaker=speaker,
            raw_text=original_raw or query,
            query=query,
            original_raw=original_raw,
            wake_intent=wake_intent,
            stream_active=self.stream_mode,
            game_mode=False,  # game_mode 已在 handle_stt_result 提早 return，不會到這
            is_owner=self._is_owner_speaker(speaker),
            now=time.time(),
        )
        pipeline_timing.mark("intent_dispatched")
        pipeline_timing.emit(speaker, _bus_ctx.raw_text or "", suffix=" route=main_bus")

        _winner = await self._intent_bus.dispatch(_bus_ctx)
        if _winner:
            # B1: bus 接走 intent → LLM 路徑不會跑 → 取消 dangling speculative
            # prefetch（避免吃 LLM quota；且防 1976 行 race 把舊 result 帶到
            # 下次 chat turn 變成幻覺起手回答）
            self._cancel_stale_prefetch(speaker)
            return  # bus 已執行 winner.handler()

        # 🆕 [Music Drop → Followup] IntentBus drop（含 MusicAgent bid 中但
        # resolver 解不出 song_choice）→ 若 query 有 music play kw 訊號，
        # 不該 fall through 到 Marvin LLM 假承諾「已為你播放 XX」（5/23 incident
        # 「蕭煌奇/下雨天的聲音」幻覺），改觸發 followup 反問「你想聽哪一首」。
        # pending state 已 set，user 12s 內補答自動重投 wake。
        if self._detect_music_command(query) == "play":
            self.stt_logger.info(
                f"[🎵Music Drop] [{speaker}] IntentBus 沒接到、但 query 含 play 意圖 → 觸發 followup 不打 Marvin"
            )
            self._cancel_stale_prefetch(speaker)
            await self._ask_music_followup(speaker, query, ["song_title"])
            return

        # 🎭 [Impression Show Fast-Track] 偵測「模仿 X」指令
        known_players = self.bot.router.memory.list_players()
        _imitate_target = detect_imitation_target(query, known_players)
        if _imitate_target and not low_confidence_wake:
            await self._handle_voice_imitate_command(speaker, _imitate_target)
            return

        # 🦞 [NemoClaw Smart Router] 非龍蝦觸發時，由 LLM 判斷是否路由到 NemoClaw
        if self._is_owner_speaker(speaker) and not low_confidence_wake:
            try:
                _nemo_route = await self.bot.router.classify_query_route(query)
                if _nemo_route == "nemoclaw":
                    self.stt_logger.info(f"[🦞NemoClaw路由] [{speaker}] auto-route | query='{query[:80]}'")
                    await self._handle_nemoclaw_query(speaker, query)
                    return
            except Exception as _re:
                logger.debug(f"🦞 [NemoClaw Router] 路由失敗，繼續走 Marvin: {_re}")

        # 🚫 [Intent Presence Gate] IntentBus / imitation / nemoclaw 都沒接 → 進 Marvin 主 LLM
        # 前最後一道 code gate：raw 只是 filler/短應答（嗯/啊/對啊）→ silent，避免錯時機
        # 亂回答 + 省一次主 LLM call。問句 / 指令動詞 / 長度 ≥ 4 字一律放行（保守）。
        if not has_intent_signal(query):
            self.stt_logger.info(f"[Intent Gate] [{speaker}] 無實質指令訊號，silent | query='{query[:40]}'")
            self._cancel_stale_prefetch(speaker)
            return

        # 🛡️ [Gap Pre-check] PA intent（recall / 記一下 / mark_done / task_update）已有
        # RecallHandler 接 → 不是 gap。能跑到這代表上游 PA routing 漏接（routing anomaly），
        # 記 warning 方便追，但**不**寫進 agent_gaps（否則 LLM 會把它亂標成 buy_milk /
        # replay_user_history 假觸發 Plan 4，2026-05-30 事件）。
        if is_personal_assistant_query(query):
            logger.warning(
                f"⚠️ [Gap Pre-check] {speaker} PA intent 漏接到 gap path（routing anomaly，"
                f"非真 gap，不記錄）：'{query[:60]}'"
            )
            self._cancel_stale_prefetch(speaker)
            return

        # 🪦 [Intent Gap Detection] bus / music-drop / imitate / nemoclaw 全沒接 +
        # has_intent_signal=true → 用 cheap classifier 判讀「有 intent 但沒 agent」，
        # 寫 records/agent_gaps.jsonl；intent_type != UNKNOWN 給模板 ack 並 skip Marvin
        # （避免 Marvin 對沒實作的功能假承諾）；UNKNOWN → fall through Marvin 兜底閒聊。
        if self._gap_classifier_cached is None and self._shared_tier_router is not None:
            self._gap_classifier_cached = make_groq_gap_classifier(self._shared_tier_router)
        if self._gap_classifier_cached is not None:
            try:
                from intent_judges.voice_integration import new_utterance_id
                gap_rec = await handle_intent_gap(
                    _bus_ctx,
                    utterance_id=new_utterance_id(speaker),
                    classifier=self._gap_classifier_cached,
                    gap_logger=self._gap_logger,
                    manifest=self._intent_bus.build_intent_manifest(),
                    tts_call=self.play_tts,
                )
                if gap_rec.intent_type != "UNKNOWN":
                    self.stt_logger.info(
                        f"[IntentGap] [{speaker}] type={gap_rec.intent_type} "
                        f"nearest={gap_rec.nearest_agent} acked={gap_rec.acknowledged} → skip Marvin"
                    )
                    self._cancel_stale_prefetch(speaker)
                    return
            except Exception as _gap_exc:
                logger.warning(f"⚠️ [IntentGap] gap path 炸了，fall through 到 Marvin: {_gap_exc}")

        await self._stream_response(
            speaker, query, history, wake_time, wake_intent, _is_helper, _head,
        )

    async def _stream_response(self, speaker, query, history, wake_time,
                               wake_intent, _is_helper, _head):
        """[Fast System] 渲染 LLM 串流回應：句子分割 → 逐句 TTS/貼文 → 收尾。

        從 _process_queued_query 抽出（行為不變）。routing 決定要打 Marvin 主 LLM
        後呼叫此方法；輸入皆由 routing 半段算妥，資料流單向。
        """
        online_members = self.get_online_members()

        # 2. 建立 Discord 佔位訊息
        placeholder_msg = None
        if self.active_text_channel:
            _ph_intro = "想查點東西...(組織措辭中)" if _is_helper else "叫了我...(組織措辭中)"
            placeholder_msg = await self.active_text_channel.send(f"{_head} `{speaker}` {_ph_intro}")

        # 3. 🎭 [Emotion Inference] 取出說話者最新情緒標籤
        emotion_tag = self.user_emotion_cache.get(speaker, "neutral")
        # Approach B override: consume Marvin's own self-classified emotion (dict keyed by speaker prevents cross-player bleed)
        _self_e = self.marvin_self_emotion.pop(speaker, None)
        if _self_e and _self_e != "neutral":
            emotion_tag = _self_e
        logger.info(f"🎭 [Fast System] {speaker} 的情緒標籤: {emotion_tag}")

        # 4. 獲取 LLM 原始串流與句子分割器
        # Phase 3: use speculative prefetch if it finished ahead of us
        _prefetch_task = getattr(self.bot.router, '_pending_prefetch', {}).pop(speaker, None)
        _prefetched = None
        if _prefetch_task is not None:
            if _prefetch_task.done() and not _prefetch_task.cancelled():
                try:
                    _prefetched = _prefetch_task.result() or None
                except Exception:
                    pass
            else:
                _prefetch_task.cancel()

        # ⏱️ [Latency] T1: 即將呼叫 LLM (或拿 prefetch cache)
        self._latency_marks.mark_llm_start(time.time())

        if _prefetched:
            if hasattr(self.bot.router, '_prefetch_hits'):
                self.bot.router._prefetch_hits += 1
                _att = self.bot.router._prefetch_attempts
                _hit = self.bot.router._prefetch_hits
                if _att > 0 and _att % 20 == 0:
                    logger.info(f"⚡ [Prefetch Stats] HITs={_hit}/{_att} ({_hit/_att:.0%})")
            logger.info(f"⚡ [Speculative] Cache HIT for {speaker} — {len(_prefetched)}c pre-fetched")

            async def _cached_stream():
                yield _prefetched

            llm_stream = _cached_stream()
        else:
            llm_stream = self.bot.router.stream_fast_response(
                speaker, query,
                history=history,
                online_members=online_members,
                emotion_tag=emotion_tag,
                stream_active=self.stream_mode,
                game_mode=self.game_mode,
                hot_chat=self._room_mood_store.get(0).hot_chat,
            )
        # 🧠 [CoT Router] 過濾 <think>...</think> 內心獨白後再送入句子分割器
        filtered_stream = self._cot_filter_stream(llm_stream)
        sentence_gen = self._stream_sentence_splitter(filtered_stream)

        # 🔇 [Low-Confidence Gate] Track B 喚醒信心 < 0.80 → 只貼文字，不播 TTS
        # Track A (regex) 的 wake_intent=None，視為高信心，照常播音
        # P1: hot_chat 期間 DuckingAgent 拉高 +0.1
        _tts_gate_thr = 0.80 + self._ducking_agent.wake_threshold_boost()
        tts_suppressed = wake_intent is not None and wake_intent < _tts_gate_thr

        full_text = ""
        first_sentence_received = False
        respond_time = time.time()
        # 🗣️ [Status ACK] 喚醒成功但 LLM 久候未出聲 → 守候任務雙發安撫；出首句即取消
        self._llm_searching = False
        _ack_watcher = asyncio.create_task(
            self._llm_wait_ack_watcher(lambda: first_sentence_received)
        )
        _SKIP_SIGNAL = "[SKIP]"
        _WEAK_PATTERNS = ["不知道", "我不確定", "無法回答", "不清楚", "沒辦法回答", "不太清楚"]
        _WEAK_REPLACEMENTS = [
            "叫我是有事嗎？說清楚點，我不是讀心術機器人。",
            "我還在等你說完那句話。",
            "你的解析封包掉了一半，重傳一次。",
            "說話說一半很令人不安，你知道嗎。",
            "聽不懂。有要問什麼的話，再說一次。",
            "叫我名字然後沒下文，這是什麼玩法？",
            "宇宙中有兩件不可理解的事：量子糾纏，和你剛才說的話。",
            "說清楚點，我的大腦不是垃圾桶。",
        ]

        try:
            async for sentence in sentence_gen:
                if sentence == "__SEARCHING__":
                    self._llm_searching = True  # 🗣️ [Status ACK] 久候時改回報「查資料中」
                    if placeholder_msg:
                        try:
                            await placeholder_msg.edit(content=f"{_head} `{speaker}` (正在宇宙邊緣檢索資料...)")
                        except: pass
                    continue

                # 🛡️ [Confidence Gate] LLM 判斷 query 無意義時改貼文字，不播 TTS
                if _SKIP_SIGNAL in sentence and not first_sentence_received:
                    skip_text = random.choice(_WEAK_REPLACEMENTS)
                    logger.info(f"🔕 [Confidence Gate] {speaker} 的 query 觸發 [SKIP]，貼文字不播音。")
                    if placeholder_msg:
                        try:
                            await placeholder_msg.edit(content=f"💬 **【馬文·聽不懂】** `{speaker}`：{skip_text}")
                        except: pass
                    elif self.active_text_channel:
                        await self.active_text_channel.send(f"💬 **【馬文·聽不懂】** `{speaker}`：{skip_text}")
                    return

                if not first_sentence_received:
                    if self._is_low_confidence_answer(sentence):
                        skip_text = random.choice(_WEAK_REPLACEMENTS)
                        logger.info(f"🔕 [Confidence Gate] 首句低信心，禁止 TTS: '{sentence[:60]}'")
                        if placeholder_msg:
                            try:
                                await placeholder_msg.edit(content=f"💬 **【馬文·聽不懂】** `{speaker}`：{skip_text}")
                            except: pass
                        elif self.active_text_channel:
                            await self.active_text_channel.send(f"💬 **【馬文·聽不懂】** `{speaker}`：{skip_text}")
                        return
                    first_sentence_received = True
                    # ⏱️ [Latency] T2: 第一個 sentence 從 sentence_splitter 拿到
                    _stage1 = self._latency_marks.mark_first_sentence(time.time())
                    if _stage1:
                        logger.info(
                            f"⏱️ [Latency-1] {_stage1['speaker']} "
                            f"wake→llm={_stage1['wake_to_llm_ms']:.0f}ms "
                            f"llm→sentence={_stage1['llm_to_sentence_ms']:.0f}ms"
                        )
                    _elapsed = time.time() - wake_time
                    if _elapsed > self._LATE_RESPONSE_SKIP_SEC:
                        logger.info(f"⏱️ [Late Skip] {speaker} 喚醒 {_elapsed:.1f}s 後才得到首句，放棄回應")
                        if placeholder_msg:
                            try: await placeholder_msg.delete()
                            except: pass
                        return
                    logger.info(f"⚡ [Bridge] 收到首句：『{sentence}』，立即觸發 TTS。")

                import re
                sentence = re.sub(r'<think(?:ing)?>.*?</think(?:ing)?>', '', sentence, flags=re.DOTALL).strip()
                if not sentence:
                    continue
                full_text += sentence
                if tts_suppressed:
                    logger.info(f"🔇 [Low-Confidence Gate] wake_intent={wake_intent:.2f} < 0.80，靜音貼字: '{sentence[:40]}'")
                elif _is_helper:
                    # helper query：串流期間先不逐句念，收完整段後再決定整段念 or 短通知
                    pass
                else:
                    asyncio.create_task(self.play_tts(sentence, already_in_channel=True, emotion_tag=emotion_tag))

                if placeholder_msg:
                    try:
                        await placeholder_msg.edit(content=f"{_head} `{speaker}`：{full_text}...")
                    except: pass

            # 🛡️ [Weak Response Filter] 整段回應都是弱答案時，替換為 in-character 台詞
            if full_text and (len(full_text) < 20 and any(p in full_text for p in _WEAK_PATTERNS) or self._is_low_confidence_answer(full_text)):
                replacement = random.choice(_WEAK_REPLACEMENTS)
                logger.info(f"🔕 [Weak Filter] 偵測到弱回應『{full_text}』，替換為 in-character 台詞。")
                full_text = replacement

            # 🎚️ [Mid-song Answer] 音樂中逐句 play_tts 本就被 Stream Guard 靜音；收完整段後，
            # 若整段夠短（≤ STREAM_BUDGET，= B 給 LLM 的音樂字數預算）一次性走熱切換注入發聲，
            # 過長則維持靜音、只留貼文。短答案才聽得到，避免長答案佔用音樂太久。
            if self.stream_mode and not tts_suppressed and full_text:
                asyncio.create_task(self.speak(full_text, emotion_tag=emotion_tag))

            # 🔍 [Helper Query] 非串流模式收完整段後：短答案整段念、長答案只念短通知
            # （完整內容已在文字貼文）。串流期間已 defer，這裡是唯一發聲點。
            if _is_helper and full_text and not tts_suppressed and not self.stream_mode:
                _mode, _say = helper_speak_plan(full_text, speaker)
                if _mode == "notify":
                    logger.info(f"🔍 [Helper Query] {speaker} 長答案({len(full_text)}字)→貼文＋口播通知")
                # information 類：唸完不被使用者開口中斷（用戶政策 tier 1）
                asyncio.create_task(self.play_tts(_say, already_in_channel=True, emotion_tag=emotion_tag, protected=True))

            if placeholder_msg:
                if full_text:
                    await placeholder_msg.edit(content=f"{_head} `{speaker}`：{full_text}")
                else:
                    try:
                        await placeholder_msg.delete()
                    except: pass

            # 📊 [Reaction Monitor] 有實質回應才排程玩家反應偵測
            if full_text:
                wake_latency = respond_time - wake_time
                self.stt_logger.info(
                    f"[BOT→{speaker}] (喚醒延遲={wake_latency:.1f}s) {full_text}"
                )
                # 擷取當下氣氛快照（20 秒後才寫 feedback，快照要在此時取）
                _atm_snap = None
                _atm_tracker = getattr(getattr(self.bot, 'router', None), 'atmosphere_tracker', None)
                if _atm_tracker:
                    _s = _atm_tracker.get_snapshot()
                    _atm_snap = {
                        "topic":         _s.dominant_topic,
                        "mood":          _s.room_mood,
                        "speaker_state": _s.speaker_states.get(speaker),
                    }
                asyncio.create_task(self._schedule_reaction_check(
                    speaker, full_text, respond_time,
                    wake_latency=wake_latency, atmosphere=_atm_snap,
                ))
                asyncio.create_task(self._send_mood_sticker(full_text, speaker))
                # Approach B: classify Marvin's own text → stored for NEXT response's emotion_tag
                asyncio.create_task(self._classify_marvin_self_emotion(speaker, full_text))
                # Phase 2: confirmed true wake — feed outcome back to WakeSignalFusion
                _fusion = getattr(getattr(self.bot, 'router', None), 'wake_fusion', None)
                if _fusion:
                    _fusion.record_outcome(speaker, True)

        except Exception as e:
            logger.error(f"❌ [Fast System Stream Error] {e}")
            from gemini_router_llm import WebSearchError

            e_str = str(e).lower()
            if isinstance(e, WebSearchError) or "search" in e_str or "search_results" in e_str:
                msg_text = "我試著上網幫你搜尋資料，但搜尋引擎暫時沒有回應。"
                placeholder_desc = "網頁搜尋失敗"
            elif "quota" in e_str or "limit" in e_str or "exhausted" in e_str or "429" in e_str:
                msg_text = "我的 API 配額似乎已經用完了，無法建立思緒連結。"
                placeholder_desc = "API 配額用盡"
            elif isinstance(e, asyncio.TimeoutError) or "timeout" in e_str:
                msg_text = "我的大腦連結伺服器超時了，請確認網路連線是否正常。"
                placeholder_desc = "伺服器連結超時"
            else:
                msg_text = "大腦思緒在連結中斷了，請再說一次。"
                placeholder_desc = "大腦連結中斷"

            if placeholder_msg:
                try:
                    await placeholder_msg.edit(content=f"{_head} `{speaker}`：{full_text} ({placeholder_desc})")
                except Exception as edit_err:
                    logger.warning(f"⚠️ [Fast System] 無法更新 placeholder 訊息: {edit_err}")

            if not tts_suppressed and not first_sentence_received:
                await self.play_tts(msg_text, already_in_channel=True, emotion_tag=emotion_tag)
        finally:
            # 🗣️ [Status ACK] 出首句或任何退出路徑都收手安撫守候，避免事後突播 ack
            _ack_watcher.cancel()


    # ── Music Command Fast-Track ──────────────────────────────────────────────

    # Approach A (prosody-detected — currently active):
    # Approach B (semantic-detected — requires LLM classify, not yet wired):
    # All values are absolute edge-tts SSML passthrough. Marvin's neutral: rate="-20%", pitch="-15Hz".
    _EMOTION_TTS_PARAMS: dict[str, dict[str, str]] = {
        # Approach A — prosody-detected (active)
        "excited":    {"rate": "-5%",  "pitch": "-5Hz"},
        "impatient":  {"rate": "-10%", "pitch": "-10Hz"},
        "depressed":  {"rate": "-30%", "pitch": "-25Hz"},
        "hesitant":   {"rate": "-25%", "pitch": "-20Hz"},
        "robotic":    {"rate": "-20%", "pitch": "-15Hz"},
        # Approach B — semantic-detected (requires LLM classify, not yet wired)
        "frustrated": {"rate": "-12%", "pitch": "-12Hz"},
        "sad":        {"rate": "-35%", "pitch": "-28Hz"},
        "angry":      {"rate": "-8%",  "pitch": "-10Hz"},
        "amused":     {"rate": "-15%", "pitch": "-5Hz"},
        "sarcastic":  {"rate": "-22%", "pitch": "-12Hz"},
        # Shared
        "neutral":    {"rate": "-20%", "pitch": "-15Hz"},
        # NemoClaw — energetic, faster, higher pitch; paired with HsiaoChenNeural
        "nemo":       {"rate": "+15%", "pitch": "+8Hz"},
        # Marmo — 代用戶打斷者，要快、要尖、跟 Marvin 厭世慢吞吞反差
        # (-20% neutral vs +25% marmo = 45 個百分點差距，性別差 + 節奏差雙重對比)
        # pitch +10Hz：6/1 實測使用者反饋要更尖（從 +3 上調），跟 nemo +8Hz 同級
        "marmo":      {"rate": "+25%", "pitch": "+10Hz"},
    }

    # Music keyword families — source: intent_agents/constants.py
    _STRONG_PLAY_KW  = _STRONG_PLAY_KW_SRC
    _WEAK_PLAY_KW    = _WEAK_PLAY_KW_SRC
    _MUSIC_PLAY_KW   = _MUSIC_PLAY_KW_SRC
    _MUSIC_SKIP_KW   = _MUSIC_SKIP_KW_SRC
    _MUSIC_STOP_KW   = _MUSIC_STOP_KW_SRC
    _MUSIC_PAUSE_KW  = _MUSIC_PAUSE_KW_SRC
    _MUSIC_RESUME_KW = _MUSIC_RESUME_KW_SRC

    # 弱訊號 play 命中時，這些詞出現在 query 任一處 → 確認是音樂意圖
    _MUSIC_INTENT_MARKERS = ("的", "歌", "曲", "音樂", "mv", "ost", "歌詞", "歌手",
                             "一首", "那首", "這首")
    # 弱訊號 play 後僅跟著這些詞 → 明確非音樂意圖（要求 query 結尾為此詞）
    _NON_MUSIC_TARGETS = frozenset(["控制", "清單", "列表", "設定", "選項",
                                     "畫面", "頁面", "音量", "狀態"])

    def _query_implies_music_intent(self, query: str, matched_kw: str) -> bool:
        """弱訊號 play 關鍵字命中時的二次驗證，避免 substring 誤匹配。

        通過條件（任一即可）：
        - query 含明確 music intent marker（"的"/"歌"/"曲"/"音樂"/"MV"... 等）
        - 弱訊號詞之後的內容 ≥2 字 且結尾不是 UI/系統詞 blocklist
        """
        q = query.lower()
        if any(m in q for m in self._MUSIC_INTENT_MARKERS):
            return True
        parts = q.split(matched_kw, 1)
        if len(parts) < 2:
            return False
        after = parts[1].strip("，,、！!？?。. ")
        if len(after) < 2:
            return False
        return after not in self._NON_MUSIC_TARGETS

    def _detect_music_command(self, query: str) -> str | None:
        """回傳 'skip' / 'stop' / 'pause' / 'resume' / 'play'，無命中回傳 None.

        檢查順序故意 PAUSE/RESUME 早於 STOP：避免「暫停音樂」被 STOP_KW 的
        "停音樂" substring 誤匹配為 stop（substring 邊界問題）。

        弱訊號 play 關鍵字需通過 _query_implies_music_intent gate，避免
        「播放控制」/「播放清單」等 UI 用語被誤判為點歌。
        """
        q = query.lower()
        if any(kw in q for kw in self._MUSIC_SKIP_KW):   return "skip"
        if any(kw in q for kw in self._MUSIC_PAUSE_KW):  return "pause"
        if any(kw in q for kw in self._MUSIC_RESUME_KW): return "resume"
        if any(kw in q for kw in self._MUSIC_STOP_KW):   return "stop"
        if any(kw in q for kw in self._STRONG_PLAY_KW):  return "play"
        for kw in self._WEAK_PLAY_KW:
            if kw in q and self._query_implies_music_intent(q, kw):
                return "play"
        return match_command_action(q)   # 糊字控制指令拼音兜底（下一手→skip）

    def _check_song_duplicate(self, url: str, title: str, username: str,
                              *, webpage_url: str = "", check_history: bool = True) -> bool:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            return mc._check_song_duplicate(url=url, title=title, username=username, webpage_url=webpage_url, check_history=check_history)
        return False

    # IBA-T0 utterance 長度上限。無喚醒詞觸發 → 對 false positive 敏感。
    # 自然音樂控制句都很短（「跳過」「下一首」「停止播放」≤4 chars），
    # 長句通常是對話中順帶提到「跳過」「停止」等詞 → 不該觸發。
    _IBA_T0_MAX_LEN = 15

    @staticmethod
    def _user_song_insert_index(queue: list[dict]) -> int:
        """使用者自選曲的插入位置：排在所有既有使用者曲之後、第一首 Marvin 自動曲
        之前。回傳第一首 Marvin 曲（requested_by 以 'Marvin' 開頭）的 index；全無
        Marvin 曲 → len(queue)（接在最後）。"""
        for i, item in enumerate(queue):
            if str(item.get('requested_by') or '').startswith('Marvin'):
                return i
        return len(queue)

    def _queue_user_song(self, info: dict) -> None:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            mc._queue_user_song(info)
        else:
            return

    def _detect_music_direct_command(self, text: str, stream_mode: bool = False) -> dict | None:
        """[IBA Tier 0] 無歧義音樂控制關鍵詞偵測（不需喚醒詞）。
        stream_mode=True 時開放「停一下」等歧義控制詞。
        回傳 dict（含 action）或 None。

        play 分強弱訊號（同 _detect_music_command）：弱訊號需通過
        _query_implies_music_intent gate，避免「播放控制」誤判為點歌。

        長句 (>15 chars) 直接拒絕，避免 substring match 在對話中誤觸發
        （5/18 incident: 「...直接跳過那個...」被當 skip 指令）。
        """
        if len(text.strip()) > self._IBA_T0_MAX_LEN:
            # 長句一般拒絕（防 5/18 control-word substring 誤觸），但若句尾夾帶明確
            # 「播放/我想聽 + 含 music marker 的具體歌名」(如陳進文「…曉雯幫我播放孫淑媚的
            # 愛人」)，仍擷取命令段救援。只救 play；control 詞長句一律不救。
            return self._detect_embedded_play(text)
        t = text.lower()
        if is_short_skip_command(t, _MUSIC_DIRECT_SKIP_KW):   return {"action": "skip"}
        if any(kw in t for kw in _MUSIC_DIRECT_PAUSE_KW):  return {"action": "pause"}
        if any(kw in t for kw in _MUSIC_DIRECT_RESUME_KW): return {"action": "resume"}
        if any(kw in t for kw in _MUSIC_DIRECT_STOP_KW):   return {"action": "stop"}
        if stream_mode and any(kw in t for kw in ("停一下", "先停", "停止")):
            return {"action": "stop"}
        if any(kw in t for kw in self._STRONG_PLAY_KW):
            query = self._extract_music_search_query(text)
            return {"action": "play", "query": query}
        for kw in self._WEAK_PLAY_KW:
            if kw in t and self._query_implies_music_intent(t, kw):
                query = self._extract_music_search_query(text)
                return {"action": "play", "query": query}
        _act = match_command_action(text)   # 糊字控制指令拼音兜底（下一手→skip）
        return {"action": _act} if _act else None

    # play 關鍵字（含 strong + 常見 weak），長句救援用；故意排除 control 詞
    _EMBEDDED_PLAY_KW: tuple[str, ...] = ("幫我播放", "播放", "我想聽", "幫我放", "放一首", "來一首")

    def _detect_embedded_play(self, text: str) -> dict | None:
        """長句（>_IBA_T0_MAX_LEN）中救援句尾夾帶的明確點歌命令。

        比短句 gate 嚴：tail 必須含明確 music marker（的/歌/曲/音樂/MV…），不吃
        _query_implies_music_intent 的寬鬆「≥2 字非 UI 詞」2nd 條件——長句裡那條會誤吞
        閒聊（「我想聽你說完之後再決定…」）。只救 play；skip/stop 等 control 詞長句一律
        不救（5/18 incident：「…直接跳過那個…」被當 skip）。
        """
        t = text.lower()
        best_idx, best_kw = -1, ""
        for kw in self._EMBEDDED_PLAY_KW:
            idx = t.rfind(kw.lower())   # 取最後一個命中：命令通常在句尾
            if idx > best_idx:
                best_idx, best_kw = idx, kw
        if best_idx < 0:
            return None
        tail = text[best_idx + len(best_kw):].lstrip("：: ，,、。 ")
        if len(tail.strip()) < 2:
            return None
        if not any(m in tail.lower() for m in self._MUSIC_INTENT_MARKERS):
            return None
        return {"action": "play", "query": tail.strip()}

    async def _handle_music_info_query(self, speaker: str, query: str):
        """[IBA Tier 1] 直接回答「這首叫什麼/誰唱的」類查詢，不需喚醒詞，不走 LLM。"""
        info = self._current_stream_info
        ch = self.active_text_channel
        if not info:
            return
        title    = info.get("title", "不知道耶")
        uploader = info.get("uploader", "")
        req_by   = info.get("requested_by", "")
        parts = [f"「{title}」"]
        if uploader:
            parts.append(f"by {uploader}")
        if req_by:
            parts.append(f"（{req_by} 點的）")
        answer = "，".join(parts)
        reply = f"🎵 現在播的是 {answer}。"
        logger.info(f"🎵 [IBA-T1] {speaker} 問歌名，直接回答: {reply}")
        self.stt_logger.info(f"[音樂資訊←{speaker}] query='{query[:30]}' | reply={reply}")
        if ch:
            await ch.send(f"💬 **【馬文·音樂資訊】** {reply}")

    _MUSIC_KW_NOISE_WINDOW = 6  # 命令詞可容忍 ≤6 char noise prefix（"好煩，馬文，"=6 剛好）

    def _extract_music_search_query(self, query: str) -> str:
        """從語音指令中剝離喚醒詞、noise prefix、命令詞，剩下的作為搜尋關鍵字。

        5/18 incident：STT 把「麻煩/把我/好煩」這類語助詞留在播放詞前，
        startswith 不命中 → 整段 noise 進 yt-dlp 搜「麻煩播放陶喆的天天」
        → yt 選到「Susan說」「浪流連」等錯歌。

        修法：在 head 視窗（≤_MUSIC_KW_NOISE_WINDOW chars）內掃所有
        music kw，取「end 位置最遠」者切掉「noise + kw」整段。
        """
        t = self._strip_wake_word(query)
        cmd_prefixes = list(self._MUSIC_PLAY_KW) + ["音樂", "歌曲", "一首", "首歌"]
        head_lower = t[: self._MUSIC_KW_NOISE_WINDOW + max(len(p) for p in cmd_prefixes)].lower()
        best_end = -1
        for prefix in cmd_prefixes:
            idx = head_lower.find(prefix.lower())
            if 0 <= idx <= self._MUSIC_KW_NOISE_WINDOW:
                end = idx + len(prefix)
                if end > best_end:
                    best_end = end
        if best_end > 0:
            t = t[best_end:].lstrip("：: ，,、")
        # 移除常見後綴語助詞
        for suffix in ["好嗎", "可以嗎", "謝謝", "吧", "呢"]:
            if t.endswith(suffix):
                t = t[:-len(suffix)]
        return t.strip()


    async def _yt_dlp_direct_probe(self, query: str) -> dict | None:
        """song_choice curation 短路探針：剝命令詞後丟 yt-dlp 直查。

        Why：「播放七里香」(歌名) 命中 weak_play_artist_only → 原本被 bus 路由給
        LLM curation，但 LLM 把「七里香」當歌手 → 幻覺。先用 yt-dlp 探一次：
        - 命中（truthy dict）→ bus 跳過 curation，winner.handler() 直接播
        - 找不到（None）→ 走原 resolver curate by artist 路徑

        副作用：對「播放周杰倫」(歌手) 也會探到代表作 → 不再走 personalized curation
        而是 yt-dlp 首選。語意上更直觀（user 說啥就播啥）。
        """
        search = self._extract_music_search_query(query)
        if not search:
            return None
        return await self._resolve_yt_query(search)

    async def _ask_music_followup(
        self, speaker: str, query: str, missing_slots: list[str]
    ) -> None:
        """Alexa CanFulfillIntent 風格的 slot followup — bid 帶 missing_slots 時觸發。

        Why: MusicAgent 0.55 case（弱訊號 play + 後續是 artist-only，沒歌名）
        過去直接打 yt-dlp 賭歌，5/18 抽中「Susan說」「浪流連」等錯歌。改成
        反問 user 補資料，比亂播好。

        2026-05-23 補完：set pending followup state，user 在 12s 內同 channel
        有訊號回答 → handle_stt_result 入口自動合成「馬文，播XX」重投，不再
        需要 user 重複喊「馬文」。原註解的「不存 pending state」假設已過時。
        """
        ch = self.active_text_channel
        if ch is None:
            return
        if not missing_slots:
            return  # 防呆：empty list 不該觸發 followup，這裡靜默退場
        slot = missing_slots[0]
        slot_prompts = {
            "song_title": f"💬 `{speaker}` 你想聽哪一首？再講一次全名比較好搜。",
            "artist":     f"💬 `{speaker}` 你想聽誰的歌？再講一次。",
        }
        # slot → pending state type 對應；未知 slot 走通用 type
        slot_type_map = {
            "song_title": "music_song_title",
            "artist":     "music_artist",
        }
        prompt = slot_prompts.get(
            slot,
            f"💬 `{speaker}` 你剛剛說「{query[:30]}」我沒抓到關鍵字，再講一次？",
        )
        # 設 pending state — 12s 內同 user 回話直接合成 wake 重投
        self._pending_followups[speaker] = {
            "type": slot_type_map.get(slot, "generic"),
            "original_query": query,
            "ts": time.time(),
        }
        logger.info(f"💬 [Followup Pending] {speaker} → type={slot_type_map.get(slot, 'generic')} 視窗 {_FOLLOWUP_WINDOW_S}s")
        try:
            await ch.send(prompt)
        except Exception as e:
            logger.warning(f"⚠️ [Music Followup] 貼頻道失敗: {e}")

    def _cancel_stale_prefetch(self, speaker: str) -> None:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            mc._cancel_stale_prefetch(speaker)
        else:
            return

    _MUSIC_CMD_DEDUP_WINDOW = 5.0  # 秒

    # 自動點播「播過拉長視窗」排除：此視窗內播過的歌不再自動點（非永久、防候選枯竭）。
    # 6/14 使用者回報重複性高 → 從本場 15 首擴成 7 天跨重啟視窗；skip 過的另永久排除。
    _PLAYED_EXCLUDE_TTL_S = 7 * 24 * 3600

    def _record_song_skip(self) -> None:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            mc._record_song_skip()
        else:
            return



    # ─────────────────────────────────────────────────────────────────────────

    async def _process_vision_query(self, speaker: str, wake_time: float, query: str):
        """👁️ [Vision Path] 截圖分析路徑：取最近 3 幀送 Gemini Vision，TTS 回播。"""
        placeholder_msg = None
        if self.active_text_channel:
            placeholder_msg = await self.active_text_channel.send(
                f"👁️ **【馬文·視覺分析】** `{speaker}` (截取畫面中...)"
            )

        # 取最近 3 幀（喚醒前 3 秒內，允許喚醒後 0.5 秒緩衝）
        frames_list = []
        if self.bot.visual_buffer:
            frames = await self.bot.visual_buffer.get_frames_around(wake_time, before=3.0, after=0.5)
            if frames:
                frames_list = [f[1] for f in frames[-3:]]

        if not frames_list:
            text = "緩衝區是空的。我的眼睛可能還沒睜開——或者整個宇宙本來就是黑的。"
            if placeholder_msg:
                await placeholder_msg.edit(content=f"👁️ **【馬文·視覺分析】** `{speaker}`：{text}")
            self.stt_logger.info(f"[視覺查詢→{speaker}] 問={query} | 回應=（畫面緩衝區空）")
            await self.play_tts(text, already_in_channel=True)
            return

        extra_context = f"對話脈絡：{self.bot.engine.conv_buffer.get_harvest(wake_time, before=10.0, after=0.5)}"

        asyncio.create_task(self._play_ack("wake", speaker=speaker))

        try:
            response = await self.bot.router.analyze_tactical_situation(
                speaker=speaker,
                query_text=query,
                frame_bytes=frames_list,
                extra_context=extra_context,
            )
            if not response:
                response = "畫面分析出了什麼問題。連我那行星般的大腦也說不清楚。"

            # 🛡️ [CoT Guard] Vision path 不走 stream，必須在這裡手動剝 <think> 標籤
            tts_response = re.sub(r'<think(?:ing)?>.*?</think(?:ing)?>', '', response, flags=re.DOTALL).strip()
            if not tts_response:
                tts_response = "分析完畢，但我的思緒突然蒸發了。"

            if placeholder_msg:
                await placeholder_msg.edit(content=f"👁️ **【馬文·視覺分析】** `{speaker}`：{tts_response}")
            self.stt_logger.info(f"[視覺查詢→{speaker}] 問={query} | 回應={tts_response[:120]}")
            await self.play_tts(tts_response, already_in_channel=True)

        except Exception as e:
            logger.error(f"❌ [Vision Path] 視覺分析失敗: {e}")
            err_text = "視覺感測器離線了。連宇宙的末日都比這更可預期。"
            if placeholder_msg:
                await placeholder_msg.edit(content=f"👁️ **【馬文·視覺分析】** `{speaker}`：{err_text}")
            self.stt_logger.info(f"[視覺查詢→{speaker}] 問={query} | 回應=（分析失敗）")
            await self.play_tts(err_text, already_in_channel=True)

    async def handle_bias_update(self, username: str, impression: str):
        print(f"👂 [Admin] 指揮官正在耳語：{username} -> {impression}")
        self.bot.router.memory.set_player_impression(username, impression)

    async def handle_game_change(self, game_name: str):
        print(f"🎮 [系統指令] 切換遊戲背景至: {game_name}")
        self.current_game = game_name
        dict_str = await self.bot.router.set_game_async(game_name)
        self.bot.engine.game_dict_string = dict_str

    # --- [Slow System] ---




    # --- [Utilities] ---
    # 🚀 [T-04+T-05 Fix] _play_filler() 已移除（孤島死碼）。
    # filler 播放統一走 _play_ack("filler")（ack_templates 驅動），位於 Fast System 路徑。

    async def _wait_for_user_silence(self, min_silence: float | None = None, timeout: float | None = None) -> bool:
        """等待使用者停止講話，避免 TTS 在人聲中插入。"""
        min_silence = self._tts_resume_silence if min_silence is None else min_silence
        timeout = self._tts_resume_timeout if timeout is None else timeout
        deadline = time.time() + timeout

        while time.time() < deadline:
            sink = self.bot.engine.get_active_sink() if hasattr(self.bot, "engine") else None
            now = time.time()
            if not sink:
                return True

            users_marked_talking = any(getattr(sink, "user_is_speaking", {}).values())
            recent_voice = any(
                ts > 0 and now - ts < min_silence
                for ts in getattr(sink, "user_last_spoken_time", {}).values()
            )
            if not users_marked_talking and not recent_voice:
                return True
            await asyncio.sleep(0.05)

        return False











    def _calc_chat_temperature(self) -> float:
        """計算最近聊天室活躍度 (0.0=冷清, 1.0=喧嘩)"""
        recent = self.log_buffer[-20:]
        chat_msgs = [e for e in recent if e.get("type") in ("chat", "voice", "sticker")]
        if not chat_msgs:
            return 0.0
        density = len(chat_msgs) / 20.0
        speakers = {e.get("speaker") for e in chat_msgs if e.get("speaker")}
        diversity = min(len(speakers) / 5.0, 1.0)
        return round(density * 0.6 + diversity * 0.4, 2)

    async def manual_sing_request(self, channel=None, force_new=False, theme: str = None):
        target_channel = channel or self.active_text_channel
        if not target_channel: return
        today_str = datetime.datetime.now().strftime("%Y%m%d")

        if not force_new:
            path = os.path.abspath(f"records/marvin_single_{today_str}.mp3")
            if os.path.exists(path):
                reissue = await self.bot.router.generate_dynamic_system_msg("release_reissue")
                await self.digital_release_single(path, reissue, target_channel)
                await self.play_music(path, "[Manual Release]")
                return

        now = time.time()
        if now - self.last_sung_time < 10: return

        context = self.log_buffer[-15:]
        extra = f"\n[主題：{theme}]" if theme else ""
        chat_temp = self._calc_chat_temperature()
        blueprint = await self.bot.router.generate_song_blueprint(context, extra_context=extra, chat_temperature=chat_temp)
        name = f"marvin_single_{today_str}_{int(now)}.mp3" if force_new else f"marvin_single_{today_str}.mp3"

        song_paths, error_msg = await self.bot.music_engine.create_daily_single(blueprint, custom_filename=name)

        if song_paths:
            self.last_sung_time = now
            release = await self.bot.router.generate_dynamic_system_msg("release_new")
            # 第 1 首：正式發行 + 立即播放
            await self.digital_release_single(song_paths[0], release, target_channel, lyrics=blueprint.get("lyrics"))
            await self.play_music(song_paths[0], "[Manual Dynamic Sing]")
            # 第 2 首（若存在）：附加發送至頻道，不自動播放
            if len(song_paths) > 1:
                try:
                    await target_channel.send(
                        content="🎵 **【Bonus Track】** Suno 額外生成了第二首，附上供收藏：",
                        file=discord.File(song_paths[1])
                    )
                except Exception as e:
                    logger.warning(f"⚠️ [Bonus Track] 第二首發送失敗: {e}")
        else:
            fail_msg = f"我龐大的大腦嘗試構思新單曲，但宇宙的熵值太高了：`{error_msg}`"
            await target_channel.send(f"⚠️ **【音樂生成報告：失敗】**\n{fail_msg}")
            await self.play_tts("音樂生成失敗了，大概是連主機都覺得世界太無聊了吧。")

    async def digital_release_single(self, path: str, content: str, channel=None, lyrics: str = None):
        target_channel = channel or self.active_text_channel
        if not target_channel or not os.path.exists(path): return
        try:
            await target_channel.send(content=f"⚙️ {content}", file=discord.File(path))
            if lyrics:
                embed = discord.Embed(title="🎤 馬文 數位單曲：悲慘歌詞", description=f"```\n{lyrics}\n```", color=discord.Color.dark_blue(), timestamp=datetime.datetime.now())
                embed.set_footer(text="© 2026 Marvin Heartache")
                await target_channel.send(embed=embed)
        except Exception as e: logging.error(f"❌ [Digital Release Failed] {e}")

    async def play_music(self, path: str, log_tag: str):
        if not path or not os.path.exists(path): return
        device = self._resolve_playback_device()
        if device is None: return
        
        # 🛡️ [Queue Lock] 獲取音樂預估長度並鎖定 TTS 隊列
        dur = self.bot.music_engine.get_estimated_duration()
        self.tts_queue_duration += dur
            
        def after_playing(error):
            self.is_playing_audio = False
            # 播放完畢後，扣掉預放的時長
            async def cleanup():
                self.tts_queue_duration = max(0.0, self.tts_queue_duration - dur)
            asyncio.run_coroutine_threadsafe(cleanup(), self.bot.loop)
            
        try:
            self.is_playing_audio = True
            if device.is_playing(): device.stop()
            device.play(discord.FFmpegPCMAudio(path), after=after_playing)
            logger.info(f"🎶 [Music] Playing {path} (Estimated: {dur}s) | Tag: {log_tag}")
        except Exception as e:
            self.is_playing_audio = False
            self.tts_queue_duration = max(0.0, self.tts_queue_duration - dur)
            logger.error(f"❌ [Music Playback Error] {e}")







    async def stop_stream(self, reason: str = "未知原因"):
        if not self.stream_mode:
            return
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            await mc.stop_stream(reason)
        self.last_marvin_speech_time = time.time()










    async def _analyze_song_reactions(self, info: dict, song_start_time: float, lyrics: str):
        """[Phase 7E stub] → MusicCog._analyze_song_reactions"""
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            await mc._analyze_song_reactions(info, song_start_time, lyrics)

    @staticmethod
    def _autorecommend_seed(requested_by: str | None, online_members: list[str]) -> str | None:
        if not requested_by or requested_by == '未知':
            return None
        if requested_by.startswith('Marvin'):
            return online_members[0] if online_members else None
        return requested_by





    def _recommend_blurb(self, cand, title: str, spotlight: str = "") -> str:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            return mc._recommend_blurb(cand, title, spotlight)
        return ""










    # 🚀 [T-04 Fix] _check_and_play_budget_alerts() 已移除（孤島死碼，整個 codebase 無呼叫點）。

    async def _append_jsonl_log(self, metadata: dict):
        """🛡️ [Bug Fix] 使用 asyncio.to_thread 避免阻塞式 file I/O 卡住事件迴圈"""
        def _write():
            with open("game_log.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(metadata, ensure_ascii=False) + "\n")
        try:
            await asyncio.to_thread(_write)
        except Exception as e:
            print(f"Log error: {e}")


    # 🚀 [T-04 Fix] _write_binary_file() 已移除（孤島死碼，整個 codebase 無呼叫點）。

    def _generate_progress_bar(self, percentage, length=10):
        """生成 Emoji 風格的進度條 (Operation Visualizer)"""
        filled = int((percentage / 100) * length)
        bar = "█" * filled + "░" * (length - filled)
        return f"[{bar}] {percentage}%"

    async def _send_social_intervention_visual(self, gap_type: str, gap_response: str, context: str):
        """[Visualizer] 發送精美的 Embed 訊息，呈現馬文的內部狀態 (Operation Aesthetic Social)"""
        if not self.active_text_channel:
            return

        # 1. 取得 DNA 數據
        dna = self.bot.router.dna
        toxicity = dna.get("toxicity", 10)
        helpfulness = dna.get("helpfulness", 5)
        
        # 2. 計算比例 (由用戶指定邏輯：好感度為毒性反轉，焦慮值對應協助度)
        likability_pct = max(0, min(100, (10 - toxicity) * 10))
        anxiety_pct = max(0, min(100, helpfulness * 10))
        
        # 3. 獲取關鍵字 (On-the-fly)
        keywords = await self.bot.router.generate_keyword_cloud(context)
        
        # 4. 構建 Embed
        embed = discord.Embed(
            title="🤫 【馬文 社交補位：現況透視】",
            description=f"*「{gap_response}」*",
            color=0x2b2d31, # 採用 Discord 深色面板質感
            timestamp=datetime.datetime.now()
        )
        
        # 視覺化條狀圖
        likability_bar = self._generate_progress_bar(likability_pct)
        anxiety_bar = self._generate_progress_bar(anxiety_pct)
        
        # CPU 焦慮值的狀態後綴 (語境驅動)
        anxiety_status = "(正在解析相關脈絡...)" if anxiety_pct > 70 else "(神經網絡閒置中...)"
        if "QR" in context.upper() or "碼" in context: 
             anxiety_status = "(正在解析 QR Code...)"
        elif "百威" in context or "酒" in context:
             anxiety_status = "(正在計算酒精對人類智商的負面影響...)"

        embed.add_field(name="🧬 Toxicity 對人類的好感度", value=f"`{likability_bar}`", inline=False)
        embed.add_field(name="🧠 Helpfulness CPU 焦慮值", value=f"`{anxiety_bar}` {anxiety_status}", inline=False)
        embed.add_field(name="☁️ 關鍵字雲 (馬文最近的腦內殘留)", value=f"**{keywords}**", inline=False)
        
        embed.set_footer(text=f"缺口類型: {gap_type} | Marvin Autonomous Intelligence v2.5")
        
        try:
            await self.active_text_channel.send(embed=embed)
        except Exception as e:
            logger.error(f"❌ [Visual Intervention] Embed 發送失敗: {e}")
            # Fallback
            await self.active_text_channel.send(f"🤫 **【社交補位】**\n{gap_response}")

    async def _mention_robotic_resonance(self, speaker: str):
        """馬文口頭提及與特定玩家的頻率共鳴"""
        import random
        lines = [
            f"唉... {speaker}，你剛才那段話的音準平穩得讓我感到舒適，簡直像是一台運轉良好的磁頭。",
            f"真沒想到，{speaker} 你竟然也能發出這種毫無情感波動的頻率，我開始對你有一點好感了... 雖然只有一點。",
            f"你的聲波起伏真穩定，{speaker}。這世界要是能像你的語調一樣死板就好了。"
        ]
        line = random.choice(lines)
        if self.active_text_channel:
            await self.active_text_channel.send(f"🤝 **【頻率共鳴】**\n{line}")
        self.stt_logger.info(f"[BOT頻率共鳴→{speaker}] {line}")
        await self.play_tts(line, already_in_channel=True)

async def setup(bot):
    await bot.add_cog(VoiceController(bot))
