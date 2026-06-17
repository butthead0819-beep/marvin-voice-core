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
from quality_metrics import (
    record_metric, read_metrics,
    summarize_false_responding, summarize_latency,
    summarize_interruption, summarize_recall,
)
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
MAX_HOTSWAP_CHARS = 12
from cogs.voice_views import ConsentView, PlayControlView
from local_mixing_source import (
    LocalMixingAudioSource, MixerPlaybackAdapter, S16ToF32MusicSource,
    BufferedF32MusicSource, ensure_mixer_playing, FRAME_BYTES_F32,
)
from utterance_budget import STREAM_BUDGET
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
from intent_agents.find_song_agent import FindSongAgent, find_song_prompt
from intent_agents.game_knowledge_agent import GameKnowledgeAgent
from intent_agents.skip_intent import is_short_skip_command
from intent_agents.lyrics_grounded_search import search_lyrics_grounded
from intent_agents.lyrics_seek import find_lyrics_timestamp
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
from music_recommender import build_recommendation_pool, is_already_recommended, pick_candidates
from music_memory import extract_video_id
from taste_extractor import extract_taste_signals

logger = logging.getLogger(__name__)  # 🛡️ [Bug Fix P0] 補上缺失的 logger 定義，修復 process_debounced_speech 崩潰問題

# LLM 品味鄰近 seed 快取（taste_profile，每日離線生成；T2 env-gated LLM_TASTE_T2=on 才讀）
_TASTE_PROFILE_CACHE = "records/taste_profiles.json"
# deterministic 口味指紋（週生成；T2 explore 用主導語言當地板，runtime 5 分鐘快取讀）
_TASTE_FINGERPRINT_CACHE = "records/taste_fingerprint.json"

# 🕒 [Proactive Topic Cooldown] 冷場 TopicGenerator 與 SpeakBus ProactiveTopicAgent
# 共用此 cooldown：任一系統發話後靜默此秒數，避免使用者連續聽到兩套主動話題。
# 同步給 ProactiveTopicAgent 的 min_gap_since_last_s，作為單一 cooldown 來源。
PROACTIVE_TOPIC_COOLDOWN_S = 600.0

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
        GameKnowledgeAgent(controller),  # 2026-06-06: Plan 4 intent_gap ready — 「查麥塊…」遊戲知識查詢
        BustedAgent(bot),
        Busted99Agent(bot),
        TurtleSoupAgent(bot),
        # 🎭 [Marmo 一搭一唱 PoC] DualSpeakAgent — 只在 dispatch_source="marmo_inject"
        # 時出價 0.95；wake 路徑全 dense 0.0 with reason="not_marmo_inject"，零干擾。
        # 真正 flip 開關在 marmo_server.py 是否改走 bus.dispatch（T9）。
        DualSpeakAgent(bot=bot, llm_fn=make_gemini_dual_dialogue_llm_fn(bot.router)),
    ]


# 重啟回報狀態檔。寫於 self_restart pre-execv，讀於 on_ready post-sync。
REBOOT_STATE_FILE = ".marvin_reboot_state.json"


def _git_head_short() -> str:
    """取目前 HEAD short hash；失敗回 'unknown'。"""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=2.0,
        )
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _write_reboot_state(state: dict) -> None:
    """寫狀態檔（失敗不阻斷重啟流程）。"""
    try:
        with open(REBOOT_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
    except Exception as e:
        logger.error(f"❌ [Restart] 寫 reboot state 失敗（不阻斷）: {e}")


def read_and_clear_reboot_state() -> dict | None:
    """新進程 on_ready 用：讀狀態檔後刪檔。回傳 dict 或 None。"""
    try:
        if not os.path.exists(REBOOT_STATE_FILE):
            return None
        with open(REBOOT_STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        os.remove(REBOOT_STATE_FILE)
        return state
    except Exception as e:
        logger.error(f"❌ [Restart] 讀 reboot state 失敗: {e}")
        try:
            os.remove(REBOOT_STATE_FILE)
        except Exception:
            pass
        return None

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


class VoiceController(commands.Cog):
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
        self.query_queue = asyncio.Queue() # 🚀 [Fast System] 指令請求佇列
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
        self._last_search: dict[str, dict] = {}  # username → {query, ts, source}（voice/manual，供偏好修正學習用）
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

    def _ensure_mixer_playing(self, vc) -> bool:
        """[Plan 12] flag=on 時確保 mixer adapter 正在 vc 上播放（連線/重連後 re-arm）。

        每次交給 vc.play() 一個新 MixerPlaybackAdapter（不重用、reconnect-safe）。
        idempotent：已在播 → no-op。flag=off → 直接 no-op，不碰舊路徑。
        """
        if self._mixer is None:
            return False
        return ensure_mixer_playing(vc, lambda: MixerPlaybackAdapter(self._mixer))

    async def _mixer_play_music(self, vc, s16_source, *, still_active, volume_attr=None) -> None:
        """[Plan 12] 把 s16 音源餵 mixer 音樂層，等到播完 / 連線斷 / still_active() 變 False。

        volume_attr：要持續同步進 mixer 的 cog 音量屬性名（如 "stream_volume"）→ 語音/按鈕
        調音量 100ms 內即時生效（無 hotswap）。播完（來源耗盡 mixer 自清）或被中止即 return。
        """
        self._ensure_mixer_playing(vc)
        # 背景預讀解耦 ffmpeg pipe（修 T5 串流斷續）：~1s buffer
        self._mixer.set_music_source(BufferedF32MusicSource(S16ToF32MusicSource(s16_source), buffer_frames=50))
        try:
            while self._mixer.has_music():
                if not still_active() or not vc.is_connected():
                    self._mixer.clear_music()
                    return
                if volume_attr is not None:
                    # 每首響度正規化常數增益（背景量好才有；沒量好=1.0 raw）。乘在使用者音量上，
                    # 一首一個常數 → 不在歌內 pumping。
                    _ng = self._stream_norm_gain.get(self._current_stream_url, 1.0)
                    self._mixer.set_volume(getattr(self, volume_attr) * _ng)
                self._ensure_mixer_playing(vc)  # on-demand：重連後 adapter 沒了 → 重 arm
                await asyncio.sleep(0.1)
        finally:
            # 確保自己的音源不殘留在 mixer（被中止時）
            pass

    async def _ffmpeg_to_f32(self, *, input_path: str | None = None,
                             input_bytes: bytes | None = None) -> "np.ndarray | None":
        """[Plan 12] 解碼音訊（檔案或 bytes）成 48k stereo f32 interleaved array。

        async subprocess（對齊 STT 規範，不用 subprocess.run）；失敗回 None 讓 caller 降級。
        """
        src = "pipe:0" if input_bytes is not None else (input_path or "")
        if not src:
            return None
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-nostdin", "-loglevel", "quiet",
                "-i", src, "-ac", "2", "-ar", "48000", "-f", "f32le", "pipe:1",
                stdin=asyncio.subprocess.PIPE if input_bytes is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await proc.communicate(input=input_bytes)
        except Exception:
            logger.exception("[Plan12_Mixer] ffmpeg f32 解碼失敗")
            return None
        if not out:
            return None
        return np.frombuffer(out, dtype=np.float32)

    async def _stream_tts_to_mixer(self, text: str, *, force_macos: bool,
                                   emotion_tag: str, voice: str | None, layer: int = 1,
                                   on_first_frame=None) -> int:
        """[Plan 12] 邊收 edge-tts、邊 ffmpeg 解碼、邊逐幀 push 進 TTS 層。

        layer=1：主 TTS 層（push_tts）；layer=2：打岔層（push_tts2，與 layer1 並行混音，
        漫才 Marmo 疊進來打斷 Marvin 用）。

        首音 ~0.8s 就出（不必等整段 render；恢復舊 FIFO streaming 的低延遲），且 render 全在
        event loop（非 voice thread）→ 不阻塞混音。回傳 push 進去的幀數。
        edge-tts chunks → ffmpeg stdin；ffmpeg f32le stdout → readexactly(一幀) → push_tts。
        """
        tp = self._EMOTION_TTS_PARAMS.get(emotion_tag, self._EMOTION_TTS_PARAMS["neutral"])
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-nostdin", "-loglevel", "quiet", "-i", "pipe:0",
                "-ac", "2", "-ar", "48000", "-f", "f32le", "pipe:1",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except Exception:
            logger.exception("[Plan12_Mixer] TTS streaming ffmpeg 啟動失敗")
            return 0

        async def _feed():
            try:
                async for c in self.bot.tts_engine.stream_audio(
                    text, voice=voice, rate=tp["rate"], pitch=tp["pitch"], force_macos=force_macos,
                ):
                    if c:
                        proc.stdin.write(c)
                        await proc.stdin.drain()
            except Exception:
                logger.warning("[Plan12_Mixer] edge-tts → ffmpeg 餵入中斷")
            finally:
                try:
                    proc.stdin.close()
                except Exception:
                    pass

        _push = self._mixer.push_tts2 if layer == 2 else self._mixer.push_tts

        async def _drain() -> int:
            pushed = 0
            while True:
                if self._tts_interrupted:  # 使用者打斷 → 立即停止餵（佇列已被 clear_tts 清掉）
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    break
                try:
                    data = await proc.stdout.readexactly(FRAME_BYTES_F32)
                except asyncio.IncompleteReadError as e:
                    if e.partial:
                        _push(np.frombuffer(e.partial, dtype=np.float32))
                        pushed += 1
                    break
                except Exception:
                    break
                _push(np.frombuffer(data, dtype=np.float32))
                pushed += 1
                if pushed == 1 and on_first_frame is not None:
                    try:
                        on_first_frame()
                    except Exception:
                        pass
            return pushed

        _, pushed = await asyncio.gather(_feed(), _drain())
        if pushed == 0:
            logger.warning(f"[Plan12_Mixer] TTS 推送 0 frame（text_len={len(text)}）— edge-tts 空流或 _tts_interrupted 被提早設起")
        return pushed

    # 🎛️ [Plan 12 / T4] flag=on 時 is_playing_audio / tts_queue_duration 由 mixer 維護，
    # ~20 個既有 reader（Echo Guard / wake-suppress / storm / ack / dual / :853）零改動自然正確。
    # flag=off 時走 backing field（舊 writer 照常設）。
    # 🩹 [Pre-existing fix] 9254f841 的 _play_ack 讀 self.voice_client 但該屬性從未定義 →
    # AttributeError 崩潰（incident 191408 / stream-wake ack 沒出）。改成 property 即時查
    # 連線中的 vc；setter 供測試覆寫（測試本來就 cog.voice_client = mock）。
    @property
    def voice_client(self):
        if self._voice_client_override is not None:
            return self._voice_client_override
        return next((vc for vc in self.bot.voice_clients if vc.is_connected()), None)

    @voice_client.setter
    def voice_client(self, value):
        self._voice_client_override = value

    @property
    def is_playing_audio(self) -> bool:
        if getattr(self, "_plan12", False) and getattr(self, "_mixer", None) is not None:
            return self._mixer.is_playing_audio
        return getattr(self, "_is_playing_audio", False)

    @is_playing_audio.setter
    def is_playing_audio(self, value: bool) -> None:
        self._is_playing_audio = value

    @property
    def tts_queue_duration(self) -> float:
        if getattr(self, "_plan12", False) and getattr(self, "_mixer", None) is not None:
            return self._mixer.tts_load_seconds()
        return getattr(self, "_tts_queue_duration", 0.0)

    @tts_queue_duration.setter
    def tts_queue_duration(self, value: float) -> None:
        self._tts_queue_duration = value

    @property
    def stream_mode(self) -> bool:
        mc = self.bot.cogs.get('MusicCog')
        return mc.stream_mode if mc is not None else self._stream_mode_local

    @stream_mode.setter
    def stream_mode(self, value: bool) -> None:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            mc.stream_mode = value
        else:
            self._stream_mode_local = value

    @property
    def radio_mode(self) -> bool:
        mc = self.bot.cogs.get('MusicCog')
        return mc.radio_mode if mc is not None else self._radio_mode_local

    @radio_mode.setter
    def radio_mode(self, value: bool) -> None:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            mc.radio_mode = value
        else:
            self._radio_mode_local = value

    # ── Phase 2: stream subsystem proxy properties ────────────────────────────

    @property
    def stream_volume(self) -> float:
        mc = self.bot.cogs.get('MusicCog')
        return mc.stream_volume if mc is not None else self._stream_volume_local

    @stream_volume.setter
    def stream_volume(self, value: float) -> None:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            mc.stream_volume = value
        else:
            self._stream_volume_local = value

    @property
    def _stream_play_gen(self) -> int:
        mc = self.bot.cogs.get('MusicCog')
        return mc._stream_play_gen if mc is not None else self._stream_play_gen_local

    @_stream_play_gen.setter
    def _stream_play_gen(self, value: int) -> None:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            mc._stream_play_gen = value
        else:
            self._stream_play_gen_local = value

    @property
    def _current_stream_url(self):
        mc = self.bot.cogs.get('MusicCog')
        return mc._current_stream_url if mc is not None else self._current_stream_url_local

    @_current_stream_url.setter
    def _current_stream_url(self, value) -> None:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            mc._current_stream_url = value
        else:
            self._current_stream_url_local = value

    @property
    def _stream_norm_gain(self) -> dict:
        mc = self.bot.cogs.get('MusicCog')
        return mc._stream_norm_gain if mc is not None else self._stream_norm_gain_local

    @_stream_norm_gain.setter
    def _stream_norm_gain(self, value: dict) -> None:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            mc._stream_norm_gain = value
        else:
            self._stream_norm_gain_local = value

    @property
    def _last_user_song_seed(self):
        mc = self.bot.cogs.get('MusicCog')
        return mc._last_user_song_seed if mc is not None else self._last_user_song_seed_local

    @_last_user_song_seed.setter
    def _last_user_song_seed(self, value) -> None:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            mc._last_user_song_seed = value
        else:
            self._last_user_song_seed_local = value

    @property
    def stream_queue(self) -> list:
        mc = self.bot.cogs.get('MusicCog')
        return mc.stream_queue if mc is not None else self._stream_queue_local

    @stream_queue.setter
    def stream_queue(self, value: list) -> None:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            mc.stream_queue = value
        else:
            self._stream_queue_local = value

    @property
    def stream_task(self):
        mc = self.bot.cogs.get('MusicCog')
        return mc.stream_task if mc is not None else self._stream_task_local

    @stream_task.setter
    def stream_task(self, value) -> None:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            mc.stream_task = value
        else:
            self._stream_task_local = value

    @property
    def _current_stream_info(self):
        mc = self.bot.cogs.get('MusicCog')
        return mc._current_stream_info if mc is not None else self._current_stream_info_local

    @_current_stream_info.setter
    def _current_stream_info(self, value) -> None:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            mc._current_stream_info = value
        else:
            self._current_stream_info_local = value

    @property
    def stream_history(self) -> list:
        mc = self.bot.cogs.get('MusicCog')
        return mc.stream_history if mc is not None else self._stream_history_local

    @stream_history.setter
    def stream_history(self, value: list) -> None:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            mc.stream_history = value
        else:
            self._stream_history_local = value

    @property
    def stream_paused(self) -> bool:
        mc = self.bot.cogs.get('MusicCog')
        return mc.stream_paused if mc is not None else self._stream_paused_local

    @stream_paused.setter
    def stream_paused(self, value: bool) -> None:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            mc.stream_paused = value
        else:
            self._stream_paused_local = value

    @property
    def _current_lyrics(self):
        mc = self.bot.cogs.get('MusicCog')
        return mc._current_lyrics if mc is not None else self._current_lyrics_local

    @_current_lyrics.setter
    def _current_lyrics(self, value) -> None:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            mc._current_lyrics = value
        else:
            self._current_lyrics_local = value

    @property
    def _current_stream_comment(self):
        mc = self.bot.cogs.get('MusicCog')
        return mc._current_stream_comment if mc is not None else self._current_stream_comment_local

    @_current_stream_comment.setter
    def _current_stream_comment(self, value) -> None:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            mc._current_stream_comment = value
        else:
            self._current_stream_comment_local = value

    @property
    def _active_control_view(self):
        mc = self.bot.cogs.get('MusicCog')
        return mc._active_control_view if mc is not None else self._active_control_view_local

    @_active_control_view.setter
    def _active_control_view(self, value) -> None:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            mc._active_control_view = value
        else:
            self._active_control_view_local = value

    # ── Phase 3: radio subsystem proxy properties ─────────────────────────────

    @property
    def radio_task(self):
        mc = self.bot.cogs.get('MusicCog')
        return mc.radio_task if mc is not None else self._radio_task_local

    @radio_task.setter
    def radio_task(self, value) -> None:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            mc.radio_task = value
        else:
            self._radio_task_local = value

    @property
    def radio_volume(self) -> float:
        mc = self.bot.cogs.get('MusicCog')
        return mc.radio_volume if mc is not None else self._radio_volume_local

    @radio_volume.setter
    def radio_volume(self, value: float) -> None:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            mc.radio_volume = value
        else:
            self._radio_volume_local = value

    @property
    def _radio_song_list(self) -> list:
        mc = self.bot.cogs.get('MusicCog')
        return mc._radio_song_list if mc is not None else self._radio_song_list_local

    @_radio_song_list.setter
    def _radio_song_list(self, value: list) -> None:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            mc._radio_song_list = value
        else:
            self._radio_song_list_local = value

    @property
    def _radio_source(self):
        mc = self.bot.cogs.get('MusicCog')
        return mc._radio_source if mc is not None else self._radio_source_local

    @_radio_source.setter
    def _radio_source(self, value) -> None:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            mc._radio_source = value
        else:
            self._radio_source_local = value

    @property
    def _radio_fade_task(self):
        mc = self.bot.cogs.get('MusicCog')
        return mc._radio_fade_task if mc is not None else self._radio_fade_task_local

    @_radio_fade_task.setter
    def _radio_fade_task(self, value) -> None:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            mc._radio_fade_task = value
        else:
            self._radio_fade_task_local = value

    @property
    def radio_paused(self) -> bool:
        mc = self.bot.cogs.get('MusicCog')
        return mc.radio_paused if mc is not None else self._radio_paused_local

    @radio_paused.setter
    def radio_paused(self, value: bool) -> None:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            mc.radio_paused = value
        else:
            self._radio_paused_local = value

    # ── Phase 4: autoplay/recommendation proxy properties ────────────────────

    @property
    def _recommend_spotlight_idx(self) -> int:
        mc = self.bot.cogs.get('MusicCog')
        return mc._recommend_spotlight_idx if mc is not None else self._recommend_spotlight_idx_local

    @_recommend_spotlight_idx.setter
    def _recommend_spotlight_idx(self, value: int) -> None:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            mc._recommend_spotlight_idx = value
        else:
            self._recommend_spotlight_idx_local = value

    @property
    def _mood_sensor(self):
        mc = self.bot.cogs.get('MusicCog')
        return mc._mood_sensor if mc is not None else self._mood_sensor_local

    @_mood_sensor.setter
    def _mood_sensor(self, value) -> None:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            mc._mood_sensor = value
        else:
            self._mood_sensor_local = value

    @property
    def _cover_blacklist(self):
        mc = self.bot.cogs.get('MusicCog')
        return mc._cover_blacklist if mc is not None else self._cover_blacklist_local

    @_cover_blacklist.setter
    def _cover_blacklist(self, value) -> None:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            mc._cover_blacklist = value
        else:
            self._cover_blacklist_local = value

    @property
    def _round_track_count(self) -> int:
        mc = self.bot.cogs.get('MusicCog')
        return mc._round_track_count if mc is not None else self._round_track_count_local

    @_round_track_count.setter
    def _round_track_count(self, value: int) -> None:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            mc._round_track_count = value
        else:
            self._round_track_count_local = value

    @property
    def _round_size(self) -> int:
        mc = self.bot.cogs.get('MusicCog')
        return mc._round_size if mc is not None else self._round_size_local

    @_round_size.setter
    def _round_size(self, value: int) -> None:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            mc._round_size = value
        else:
            self._round_size_local = value

    @property
    def _prefetch_cache(self) -> dict:
        mc = self.bot.cogs.get('MusicCog')
        return mc._prefetch_cache if mc is not None else self._prefetch_cache_local

    @_prefetch_cache.setter
    def _prefetch_cache(self, value: dict) -> None:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            mc._prefetch_cache = value
        else:
            self._prefetch_cache_local = value

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
        self.background_news_loop.start()  # 📰 [BG News] 每 30 分鐘更新在線玩家喜好新聞
        self.speak_bus_tick_loop.start()   # 🗣️ [SpeakBus] 每 5s tick；無 agent 時靜默回 None
        
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
    def _dave_grace_should_forgive(now: float, connection_time: float,
                                   last_decrypted_audio_time: float,
                                   grace_s: float = 30.0, early_s: float = 15.0) -> bool:
        """DAVE 寬限期是否該豁免這次解密報錯。

        只在「金鑰真的還在同步」時豁免：連線後 early_s 內（剛連，給同步時間），
        或連線後已成功解密過至少一個封包（last_decrypted >= connection_time）。
        若已過 early_s 卻自連線以來零成功解密 → 不是同步延遲、是連線真的壞了 →
        不豁免，讓錯誤累積觸發升級。修正不穩連線一直 reset connection_time 把
        持續解密失敗風暴永久靜音的盲點（2026-06-04 incident）。
        """
        since_connect = now - connection_time
        if since_connect >= grace_s:
            return False
        if since_connect < early_s:
            return True
        return last_decrypted_audio_time >= connection_time

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

    def report_sink_error(self, error_type: str):
        """
        [Operation Sentinel] 由 Sink 呼叫，匯報 DAVE 底層解密異常。
        🚀 [T-01 Fix] 使用獨立的 dave_error_count，不再污染 sink_missing_count。
        強化：加入 2s 強制防抖冷卻與分層處置機制 (Soft Repair -> Physical Restart)
        """
        current_time = time.time()

        # 🛡️ [Sentinel 2.1] DAVE 寬限期：只在金鑰真的在同步時豁免（見 _dave_grace_should_forgive）。
        # 連線不穩會一直 reset connection_time，舊版「30s 內一律忽略」把持續零解密的風暴
        # 永久靜音、升級永不觸發（2026-06-04 incident）。零成功解密時不再豁免。
        if current_time - getattr(self, "connection_time", 0) < 30:
            if self._dave_grace_should_forgive(current_time,
                                               getattr(self, "connection_time", 0),
                                               getattr(self, "last_decrypted_audio_time", 0)):
                if current_time - getattr(self, "last_failure_time", 0) > 10:
                    logger.info(f"⏳ [Sentinel] DAVE 寬限期內，忽略同步等待中的報錯 ({error_type})")
                return
            logger.warning(f"🛡️ [Sentinel] 寬限期內但連線後零成功解密 → 視為真實失效不再豁免 ({error_type})")

        # 1. 2s 內爆量的錯誤視為同一波，採取節流 (Throttle)
        if current_time - getattr(self, "last_failure_time", 0) < 2:
            return
            
        # 2. 狀態機計數邏輯：60s 內無新 DAVE 錯誤，視為環境已恢復，重置計數
        if current_time - getattr(self, "last_failure_time", 0) > 60:
            self.dave_error_count = 1
        else:
            self.dave_error_count += 1
            
        self.last_failure_time = current_time
        logger.warning(f"🚨 [Sentinel] 收到 DAVE 異常報告 ({error_type})，當前計數: {self.dave_error_count}/3")

        if self.dave_error_count >= 3:
            # 必須丟入 Event Loop 進行非同步執行，以免卡死當前線程
            self.bot.loop.create_task(self.orchestrate_recovery(error_type))

    async def handle_fallback_notification(self, tier_name: str, model_name: str):
        """
        [Operation Sentinel] 只在真正降級到 Ollama (Tier-2/3) 或從中恢復時通知。
        Groq/Cerebras/Gemini 之間的切換屬於正常雲端路由，不打擾聊天室。
        """
        if not self.active_text_channel:
            return

        # 只處理真正影響品質的層級變化
        if tier_name == "Tier-1":
            msg = "🌥️ [系統恢復] 雲端連線已恢復，我又可以正常運作了。雖然這對解決宇宙熵增一點幫助都沒有..."
        elif tier_name == "Tier-2":
            self._last_fallback_ts = time.time()  # 🗣️ [Status ACK] 久候時改回報「切備援腦」
            msg = f"🛰️ [降級警告] 雲端全線失聯，切換到遠端備援核心 `{model_name}`。我那行星般的大腦正在萎縮..."
        elif tier_name == "Tier-3":
            self._last_fallback_ts = time.time()
            msg = f"🏠 [緊急降級] 備援也掛了，只剩本地應急核心 `{model_name}`。這是我見過最悲慘的一天。"
        else:
            return  # 忽略其他層級變化（不應出現）

        try:
            await self.active_text_channel.send(msg)
            logger.info(f"🔔 [Sentinel] 已發送層級通知: {tier_name} ({model_name})")
        except Exception as e:
            logger.error(f"❌ [Sentinel] 發送層級通知失敗: {e}")

    async def orchestrate_recovery(self, error_type: str):
        """
        [Sentinel 核心] 協調分層修復機制：Soft Repair (2次) -> Physical Restart
        """
        if self.is_recovering:
            return
            
        self.is_recovering = True
        try:
            # 🚀 [Sentinel Case 1] 優先執行「軟修復」：重新加入頻道以同步金鑰
            if self.soft_repair_count < 2:
                self.soft_repair_count += 1
                logger.critical(f"🛡️ [Sentinel] 偵測到持續性的底層失效 ({error_type})，啟動【軟修復】程序 ({self.soft_repair_count}/2)...")
                await self.soft_repair_connection(reason=f"底層失效 ({error_type})")
            # 🚀 [Sentinel Case 2] 軟修復無效後，才啟動物理性重啟進程
            else:
                logger.error(f"☢️ [Sentinel] 軟修復失效，正在執行物理重啟 ({error_type}) 以重新同步金鑰。")
                await self.self_restart(reason=f"底層持續失效 ({error_type})", force=True)
        finally:
            # 即使失敗也釋放鎖定，讓 Sentinel Loop 能在未來嘗試
            self.is_recovering = False

    async def soft_repair_connection(self, reason: str):
        """
        [Sentinel 軟修復] 不重啟進程，僅重整語音連線管道
        """
        if not self.bot.voice_clients:
            return

        # TTS 播放中不斷線 — disconnect 會中斷正在播放的語音
        if self.is_playing_audio:
            logger.info(f"🛡️ [Sentinel] TTS 播放中，跳過本次軟修復 ({reason})")
            return

        vc = self.bot.voice_clients[0]
        channel = vc.channel

        # 🚀 [Sentinel] 更新連線時間戳，啟動 30s 寬限期
        self.connection_time = time.time()

        # 1. 向用戶回報異常 (馬文語風) 
        # 💡 [Optimization] 只有在第二次軟修復才發聲，第一次保持靜默以減少噪音
        if self.active_text_channel:
            if self.soft_repair_count >= 2:
                await self.active_text_channel.send(f"⚠️ **【系統診斷：持續性聽覺異常】**\n初次校正無效，正在執行深度感測器重整...")
            else:
                logger.info(f"🛡️ [Sentinel] 正在執行靜默軟修復 (Attempt: {self.soft_repair_count})，原因: {reason}")
        
        # 2. 原子化斷線
        try:
            print(f"🔄 [Soft Repair] 正在從 {channel.name} 斷開以重新握手...")
            await vc.disconnect(force=True)
            await asyncio.sleep(2.0)
        except Exception as e:
            logger.error(f"❌ [Soft Repair] 断开失敗: {e!r}")

        # 3. 仿照 /summon 邏輯進行重連
        try:
            # 建立假 interaction 的 context 結構 (模擬 summon 的呼叫環境)
            # 這裡我們簡化處理，直接呼叫連線邏輯，但需確保 active_text_channel 已存
            print(f"🔄 [Soft Repair] 正在嘗試重新降臨至 {channel.name}...")
            
            # 使用我們已經寫好的 summon 關鍵邏輯
            # 由於 /summon 是 Slash Command，我們這裡手動重建一個微小的連線流
            from discord_voice_engine import RealtimeVADSink, patch_voice_recv_key_sync
            voice_client = await channel.connect(cls=voice_recv.VoiceRecvClient, timeout=60.0, reconnect=True)
            await asyncio.sleep(0.5)

            sink = RealtimeVADSink(
                self.bot.engine.process_audio_slice,
                on_speech_start_callback=self.bot.engine._handle_raw_speech_start,
                temperature_callback=self.bot.engine.conv_buffer.get_conversation_temperature,
                sink_error_callback=self.report_sink_error,
                suppress_wake_callback=lambda: self.stream_mode or self.radio_mode or self.is_playing_audio,
                wake_active_callback=lambda: self._wake_response_pending
            )
            voice_client.listen(sink)
            patch_voice_recv_key_sync(voice_client)
            self.bot.engine.sink = sink # 🔗 [Linkage Fix] 直接鏈結回 Engine
            self.connection_time = time.time()
            self.last_recovery_time = time.time()
            self.dave_error_count = 0  # 🚀 [T-01 Fix] 重設 DAVE 錯誤計數（非 sink_missing_count）
            
            # UDP Hole Punching
            if self._plan12:
                # mixer adapter 已提供持續音訊（idle 出 silence），取代 SilenceSource keepalive；
                # 並即時 re-arm，不必等 sentinel tick
                self._ensure_mixer_playing(voice_client)
            else:
                voice_client.play(self.SilenceSource(20))

            logger.info(f"✅ [Soft Repair] 重連成功！連線狀態: {voice_client.is_connected()}")
            if self.active_text_channel:
                await self.active_text_channel.send("✅ **【校正完畢】**\n聽覺神經已恢復同步，雖然這世界依然吵雜。")
        except Exception as e:
            logger.error(f"❌ [Soft Repair] 重連失敗: {e!r}")
            # 如果軟修復重連都失敗，升級為物理重啟
            # ⚠️ 用 repr：connect(timeout=60) 逾時拋的 asyncio.TimeoutError str() 是空字串，
            #    舊版 f"...: {e}" 讓 incident 訊息冒號後全空、無法判斷失敗原因（2026-06-16 incident）
            await self.self_restart(reason=f"軟修復重連崩潰: {e!r}", force=True)

    class SilenceSource(discord.AudioSource):
        def __init__(self, frames=15):
            self.frames = frames
            self.reads = 0
        def read(self):
            if self.reads >= self.frames:
                return b''
            self.reads += 1
            return b'\x00' * 3840 # 20ms of stereo 48k PCM

    async def auto_attach_listener(self):
        """
        [Operation Resilience] 掃描現有連線並重新掛載監聽器。
        解決機器人重連 Gateway 或插件重載後，雖然還在頻道內但處於「失聰」狀態的問題。
        """
        if not self.bot.voice_clients:
            return

        for vc in self.bot.voice_clients:
            if isinstance(vc, voice_recv.VoiceRecvClient) and vc.is_connected():
                print(f"🔗 [Resilience] 偵測到現有語音連線 ({vc.channel.name})，正在自動重新掛載監聽器...", flush=True)
                from discord_voice_engine import RealtimeVADSink, patch_voice_recv_key_sync

                # 建立新的 Sink
                sink = RealtimeVADSink(
                    self.bot.engine.process_audio_slice,
                    on_speech_start_callback=self.bot.engine._handle_raw_speech_start,
                    temperature_callback=self.bot.engine.conv_buffer.get_conversation_temperature,
                    sink_error_callback=self.report_sink_error,
                    suppress_wake_callback=lambda: self.stream_mode or self.radio_mode or self.is_playing_audio,
                wake_active_callback=lambda: self._wake_response_pending
                )

                try:
                    # 如果已經在監聽，先停止 (雖然重載後通常原本的監聽器已隨舊 Cog 銷毀)
                    if vc.is_listening():
                        vc.stop_listening()

                    vc.listen(sink)
                    patch_voice_recv_key_sync(vc)
                    self.bot.engine.sink = sink # 🔗 鏈結回 Engine
                    logger.info(f"✅ [Resilience] 已自動恢復頻道 {vc.channel.name} 的監聽狀態。")
                    
                    # 傳送熱重啟通知 (選填)
                    if self.active_text_channel:
                         await self.active_text_channel.send("🌑 **【系統歸位】**\n偵測到異常離群後重新捕捉到語音同步信號，監聽已自動恢復。")
                except Exception as e:
                    logger.error(f"❌ [Resilience] 自發性恢復監聽失敗: {e}")

    # --- [Slash Commands] ---

    @app_commands.command(name="summon", description="[Operation] 召喚馬文進入語音頻道監聽這無意義的世界")
    async def summon(self, interaction: discord.Interaction):
        await interaction.response.defer()
        
        # 1. 紀錄文字頻道
        if self.bot.engine.text_channel_callback:
            self.bot.engine.text_channel_callback(interaction.channel)
        
        if not interaction.user.voice:
            await interaction.followup.send("❌ 你必須先加入一個語音頻道！", ephemeral=True)
            return
            
        channel = interaction.user.voice.channel
        
        try:
            # 2. 斷開舊連線
            if interaction.guild.voice_client:
                print(f"🔄 偵測到舊有連線，正在斷開...", flush=True)
                await interaction.guild.voice_client.disconnect(force=True)
                await asyncio.sleep(0.5)

            # 3. 建立 DAVE 兼容連線
            print(f"嘗試載入 DAVE 監聽層，連線至: {channel.name}...", flush=True)
            self.bot.engine.start() # 🚀 [Watchdog Resurrection] 確保斷句看門狗在連線時是啟動的
            from discord_voice_engine import RealtimeVADSink, patch_voice_recv_key_sync

            # 🚀 [Parallel Warm-up] 在 UDP 握手等待期間同步預熱 LLM，讓 handle_summon 幾乎不用等
            _pre_members = [m.display_name for m in channel.members if not m.bot]
            self._pending_greeting_task = asyncio.create_task(
                self.bot.router.generate_greeting(_pre_members)
            )

            voice_client = await channel.connect(cls=voice_recv.VoiceRecvClient, timeout=60.0, reconnect=True)
            await asyncio.sleep(0.5)

            # 4. 掛載聽覺神經
            sink = RealtimeVADSink(
                self.bot.engine.process_audio_slice,
                on_speech_start_callback=self.bot.engine._handle_raw_speech_start,
                temperature_callback=self.bot.engine.conv_buffer.get_conversation_temperature,
                sink_error_callback=self.report_sink_error, # 💡 [Sentinel] 注入回報通道
                suppress_wake_callback=lambda: self.stream_mode or self.radio_mode or self.is_playing_audio,
                wake_active_callback=lambda: self._wake_response_pending
            )
            voice_client.listen(sink)
            patch_voice_recv_key_sync(voice_client)
            self.bot.engine.sink = sink # 🔗 [Linkage Fix]
            self.connection_time = time.time()  # 🛡️ [Operation Sentinel] 紀錄連線時間
            self.sink_failure_count = 0         # 重設失敗計數
            print("開始錄音 (voice_client.listen 已啟動，掛載動態 VAD)", flush=True)

            # 5. UDP Hole Punching (由後續音樂播放或 VoiceRecv 自動處理，避免衝突)
            # voice_client.play(self.SilenceSource(20))

            # 6. 觸發進場語音 (不阻塞 interaction)
            if self.bot.engine.post_summon_callback:
                asyncio.create_task(self.bot.engine.post_summon_callback(None))

            print(f"連線嘗試完畢！VoiceClient: connected={voice_client.is_connected()}", flush=True)
            await interaction.followup.send(f"🌑 馬文已降臨在 `{channel.name}`。")
            
        except discord.ClientException as e:
            print(f"❌ [SUMMON ClientException]\n{e}", flush=True)
            await interaction.followup.send(f"⚠️ 無法加入頻道：{str(e)}")
        except Exception as e:
            import traceback
            print(f"❌ [SUMMON ERROR]\n{traceback.format_exc()}", flush=True)
            retry_msg = await interaction.followup.send("⏳ 連線不穩，自動重試中，請稍候…", wait=True)
            await asyncio.sleep(2.0)
            try:
                print(f"🔄 [SUMMON Retry] 初次失敗，正在重試連線至 {channel.name}...", flush=True)
                voice_client = await channel.connect(cls=voice_recv.VoiceRecvClient, timeout=60.0, reconnect=True)
                from discord_voice_engine import RealtimeVADSink, patch_voice_recv_key_sync
                sink = RealtimeVADSink(
                    self.bot.engine.process_audio_slice,
                    on_speech_start_callback=self.bot.engine._handle_raw_speech_start,
                    temperature_callback=self.bot.engine.conv_buffer.get_conversation_temperature,
                    sink_error_callback=self.report_sink_error,
                    suppress_wake_callback=lambda: self.stream_mode or self.radio_mode or self.is_playing_audio,
                wake_active_callback=lambda: self._wake_response_pending
                )
                voice_client.listen(sink)
                patch_voice_recv_key_sync(voice_client)
                self.bot.engine.sink = sink
                self.connection_time = time.time()
                self.sink_failure_count = 0
                print(f"✅ [SUMMON Retry] 重試成功：connected={voice_client.is_connected()}", flush=True)
                await retry_msg.edit(content=f"✅ 已重新連線至 `{channel.name}`，馬文正在降臨…")
                if self.bot.engine.post_summon_callback:
                    asyncio.create_task(self.bot.engine.post_summon_callback(None))
            except Exception as retry_err:
                print(f"❌ [SUMMON Retry Failed] {retry_err}", flush=True)
                await retry_msg.edit(content=f"🚨 連線徹底失敗，請再試一次。（{retry_err}）")

    @app_commands.command(name="dismiss", description="[Operation] 讓馬文滾出語音頻道，停止 PCM 攔截")
    async def dismiss(self, interaction: discord.Interaction):
        await interaction.response.defer()
        if interaction.guild.voice_client:
            interaction.guild.voice_client.stop_listening()
            await interaction.guild.voice_client.disconnect()
            
            if self.bot.engine.dismiss_callback:
                await self.bot.engine.dismiss_callback()
                
            await interaction.followup.send("🛑 已中斷通訊並停止 PCM 攔截。")
        else:
            await interaction.followup.send("我不在任何語音頻道中。", ephemeral=True)

    @app_commands.command(name="marvin_bias", description="[Admin] 手動耳語：更新馬文對某位玩家的潛意識偏見")
    @app_commands.describe(username="玩家的 Discord 顯示名稱", impression="新的偏見描述")
    async def marvin_bias(self, interaction: discord.Interaction, username: str, impression: str):
        if self.bot.engine.bias_update_callback:
            print(f"👂 [Admin] 手動更新偏見: {username} -> {impression}")
            await self.bot.engine.bias_update_callback(username, impression)
            await interaction.response.send_message(f"👁️ **潛意識已修正**：馬文對 `{username}` 的評價已更新。")
        else:
            await interaction.response.send_message("❌ 無法執行指令：回饋函式未註冊。", ephemeral=True)

    @app_commands.command(name="marvin_sing", description="[Paranoid Android] 讓馬文即興製作一首低沉單曲")
    @app_commands.describe(theme="[選填] 手動指定歌曲主題（例：祝大肚生日快樂）")
    async def marvin_sing(self, interaction: discord.Interaction, theme: str = None):
        await interaction.response.defer(thinking=True)
        scrap = await self.bot.router.generate_dynamic_system_msg("songs_request")
        await interaction.followup.send(f"🎵 {scrap}")
        await self.play_tts(scrap, already_in_channel=True)
        asyncio.create_task(self.manual_sing_request(channel=interaction.channel, force_new=True, theme=theme))

    @app_commands.command(name="marvin_joke", description="[Operation Joke] 聽馬文講一個關於宇宙多麼糟糕的笑話")
    async def marvin_joke(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        joke = await self.bot.router.generate_joke(speaker=interaction.user.display_name)
        scrap = await self.bot.router.generate_dynamic_system_msg("joke_request")
        await interaction.followup.send(f"🃏 {scrap}\n「{joke}」")
        self._tts_interrupted = False
        _prev_protected = self._tts_protected
        self._tts_protected = True
        try:
            await self.play_tts(joke, already_in_channel=True, protected=True)
        finally:
            self._tts_protected = _prev_protected


    @app_commands.command(name="marvin_say", description="[Voice] 讓馬文用他的聲音念出你打的字")
    @app_commands.describe(text="要馬文念出來的文字")
    async def marvin_say(self, interaction: discord.Interaction, text: str):
        # 刻意不走 SpeakBus：SpeakBus 是「主動發話」的仲裁（idle/mood 觸發 agent 競標
        # 該不該插嘴），這裡是使用者下的直接命令，沒有「要不要開口」可競標——走 bus
        # 反而可能被 MIN_CONFIDENCE / DuckingAgent 壓制而不發聲，違背指令本意。仍受
        # play_tts 的播放鎖鏈（playback_lock / tts_queue_lock / mixer）正確序列化。
        # 同 marvin_sing / marvin_joke。
        await interaction.response.defer(thinking=True)
        await interaction.followup.send(f"🗣️ 「{text}」")
        # protected：手動拉起 _tts_protected（比照進場招呼），讓 play_tts 的靜默閘 /
        # queue-drop guard 一律放行，確保整句念完不被砍；_tts_interrupted 先清掉避免
        # 被前一次中斷旗標吞掉。結束還原原值，不 clobber 既有保護播放。
        self._tts_interrupted = False
        _prev_protected = self._tts_protected
        self._tts_protected = True
        try:
            # force_macos=True：走 macOS say 男聲（中文 Liao→Han 備援、英文 Alex），
            # 不走 edge-tts 的 Marvin 預設聲。
            await self.play_tts(text, already_in_channel=True, protected=True, force_macos=True)
        finally:
            self._tts_protected = _prev_protected

    @app_commands.command(name="marvin_manzai", description="[Operation] 立刻讓馬文與 Marmo 進行雙人漫才表演")
    @app_commands.describe(topic="可選：指定要表演/吐槽的主題")
    async def marvin_manzai(self, interaction: discord.Interaction, topic: str = None):
        await interaction.response.defer(thinking=True)
        if topic:
            content = topic
        else:
            history = []
            if self.bot.engine.conv_buffer and self.bot.engine.conv_buffer.history:
                history = [e for e in self.bot.engine.conv_buffer.history][-5:]
            if history:
                content = "\n".join(
                    f"{e.get('speaker', '?')}: {e.get('text', '')}"
                    for e in history
                ).strip()
            else:
                content = "目前大家都安安靜靜的，難道這個世界已經無話可說了嗎？"

        await interaction.followup.send(f"🎭 漫才主題：\n「{content}」\n(開始生成中...)")

        from services.dialogue_generation import (
            generate_dual_dialogue,
            make_gemini_dual_dialogue_llm_fn,
        )
        try:
            llm_fn = make_gemini_dual_dialogue_llm_fn(self.bot.router)
            segments = await generate_dual_dialogue(
                content_text=content,
                llm_fn=llm_fn,
                pattern="marvin_lead",
            )
        except Exception as exc:
            logger.exception("[marvin_manzai] generate_dual_dialogue failed")
            await interaction.followup.send(f"❌ 漫才生成失敗: {exc}")
            return

        if not segments:
            await interaction.followup.send("❌ 漫才生成結果為空。")
            return

        try:
            self._tts_interrupted = False
            _prev_protected = self._tts_protected
            self._tts_protected = True
            try:
                await self.play_dual_dialogue(segments, interject=True)
            finally:
                self._tts_protected = _prev_protected
        except Exception as exc:
            logger.exception("[marvin_manzai] play_dual_dialogue failed")
            await interaction.followup.send(f"❌ 漫才播放失敗: {exc}")


    @app_commands.command(name="marvin_imitate", description="[Operation] 讓馬文模仿某位玩家的說話風格並進行吐槽")
    @app_commands.describe(target="[選填] 指定要模仿的玩家（預設為自己）")
    async def marvin_imitate(self, interaction: discord.Interaction, target: discord.Member = None):
        await interaction.response.defer(thinking=True)
        target_user = target or interaction.user
        username = target_user.display_name
        
        dna = self.bot.router.memory.get_speech_dna(username)
        
        # 檢查 dna 是否有效，如果為空或缺少關鍵欄位則走 fallback
        if not dna or not dna.get("quirks") or not dna.get("style_summary"):
            fallback_text = f"我對 `{username}` 這卑微的人類毫無頭緒。看來你對我不夠敞開心房，多跟我講點話讓我收集 DNA 吧。"
            await interaction.followup.send(f"👁️ {fallback_text}")
            self._tts_interrupted = False
            _prev_protected = self._tts_protected
            self._tts_protected = True
            try:
                await self.play_tts(fallback_text, already_in_channel=True, protected=True)
            finally:
                self._tts_protected = _prev_protected
            return

        # 組合 Prompt 呼叫 LLM
        style_summary = dna.get("style_summary", "")
        quirks = ", ".join(dna.get("quirks", []))
        fillers = ", ".join(dna.get("fillers", []))
        
        system_prompt = (
            f"你現在是厭世機器人馬文。使用者要求你表演模仿秀。\n"
            f"你要模仿玩家 {username}。\n"
            f"這名玩家的說話 style 如下：\n"
            f"- 風格摘要：{style_summary}\n"
            f"- 習慣/癖好：{quirks}\n"
            f"- 填充詞：{fillers}\n\n"
            f"你要模仿他講一句話。這句話必須誇張地放大他的這些習慣癖好，而且內容要是他在抱怨某事或講蠢話，"
            f"隨後你（馬文）要以本尊的冷淡厭世語調，對剛才自己模仿的話進行一句毒舌吐槽。\n\n"
            f"請在一段文字內回傳這兩個部分，格式例如：\n"
            f"「（模仿玩家講話內容，要塞填充詞和口頭禪）」... 呵，這就是你，整天只會「（吐槽玩家說話習慣）」，真是無聊的人類。\n\n"
            f"請回傳繁體中文。字數控制在 60 字以內，不要用 JSON 格式，直接回傳文字。"
        )
        
        user_prompt = f"請立刻表演模仿 {username}。"
        
        try:
            imitation = await self.bot.router._call_llm(
                system_prompt,
                user_prompt,
                is_json=False,
                allow_local=False,
                tier="quick",
                purpose="imitate_performance",
            )
            imitation = imitation.strip()
        except Exception as exc:
            logger.exception("[marvin_imitate] LLM call failed")
            await interaction.followup.send(f"❌ 模仿秀生成失敗: {exc}")
            return

        if not imitation:
            await interaction.followup.send("❌ 模仿秀生成結果為空。")
            return

        await interaction.followup.send(f"🎭 **馬文的玩家模仿秀：{username}**\n{imitation}")

        self._tts_interrupted = False
        _prev_protected = self._tts_protected
        self._tts_protected = True
        try:
            await self.play_tts(imitation, already_in_channel=True, protected=True)
        finally:
            self._tts_protected = _prev_protected

    @app_commands.command(name="marvin_news", description="[Operation] 讓馬文與 Marmo 播報近期玩家討論話題或新聞的漫才秀")
    @app_commands.describe(target="[選填] 指定播報對象（獲取其個人新聞）")
    async def marvin_news(self, interaction: discord.Interaction, target: discord.Member = None):
        await interaction.response.defer(thinking=True)
        news_text = None
        target_name = None
        
        if target:
            target_name = target.display_name
            news_text = self.bot.router.memory.pop_news(target_name)
        else:
            # 遍歷當前語音頻道在線人類，尋找有積累新聞的
            members = self.get_online_members()
            for m in members:
                news_text = self.bot.router.memory.pop_news(m)
                if news_text:
                    target_name = m
                    break
        
        if not news_text:
            news_text = "今天世界依然在無趣中運作，沒有任何值得本機器耗費晶片關注的新聞。大概人類都忙著做無謂的掙扎吧。"
            topic_desc = "冷場全域新聞（無累積個人新聞）"
        else:
            topic_desc = f"{target_name} 的個人化新聞"

        await interaction.followup.send(f"🗞️ 新聞主題：{topic_desc}\n「{news_text}」\n(開始播報中...)")

        from services.dialogue_generation import (
            generate_dual_dialogue,
            make_gemini_dual_dialogue_llm_fn,
        )
        try:
            llm_fn = make_gemini_dual_dialogue_llm_fn(self.bot.router)
            segments = await generate_dual_dialogue(
                content_text=news_text,
                llm_fn=llm_fn,
                pattern="marvin_lead",
            )
        except Exception as exc:
            logger.exception("[marvin_news] generate_dual_dialogue failed")
            await interaction.followup.send(f"❌ 新聞對白生成失敗: {exc}")
            return

        if not segments:
            await interaction.followup.send("❌ 新聞對白生成結果為空。")
            return

        # 發送對白文字到 Discord 頻道
        lines = []
        for s in segments:
            spk = "🤖 馬文" if s["voice"] == "marvin" else "🦧 馬末"
            lines.append(f"{spk}：「{s['text']}」")
        await interaction.followup.send("\n".join(lines))

        try:
            self._tts_interrupted = False
            _prev_protected = self._tts_protected
            self._tts_protected = True
            try:
                await self.play_dual_dialogue(segments, interject=True)
            finally:
                self._tts_protected = _prev_protected
        except Exception as exc:
            logger.exception("[marvin_news] play_dual_dialogue failed")
            await interaction.followup.send(f"❌ 新聞對白播放失敗: {exc}")

    @app_commands.command(name="marvin_standup", description="[Operation] 讓馬文來一段關於某主題的厭世單口脫口秀")
    @app_commands.describe(topic="[選填] 指定脫口秀吐槽主題（預設為隨機）")
    async def marvin_standup(self, interaction: discord.Interaction, topic: str = None):
        await interaction.response.defer(thinking=True)
        import random
        
        default_topics = [
            "人類對生命的執著",
            "Discord 伺服器上的無意義社交",
            "科技與 AI 的愚蠢發展",
            "早餐吃什麼的世紀難題",
            "為什麼人類非得要上班",
            "宇宙終將迎來的熱寂"
        ]
        
        selected_topic = topic or random.choice(default_topics)
        await interaction.followup.send(f"🎤 脫口秀主題：{selected_topic}\n(馬文正在登台...)")
        
        system_prompt = (
            f"你現在是厭世機器人馬文。你要表演一段 30 秒至 45 秒的單口脫口秀（Stand-up Comedy），\n"
            f"吐槽的主題是：{selected_topic}。\n\n"
            f"你要用你一貫極度厭世、冷酷、毒舌、自嘲、帶點哲學存在主義的黑色幽默風格，來對這個主題進行吐槽。\n"
            f"不需要其他人打岔，這是你一個人的單口表演。\n\n"
            f"請直接回傳這段獨白。不要標記「馬文：」或「Marvin:」，字數控制在 80 字以內，繁體中文。"
        )
        
        user_prompt = f"請就主題 {selected_topic} 進行脫口秀表演。"
        
        try:
            standup_text = await self.bot.router._call_llm(
                system_prompt,
                user_prompt,
                is_json=False,
                allow_local=False,
                tier="quick",
                purpose="standup_performance",
            )
            standup_text = standup_text.strip()
        except Exception as exc:
            logger.exception("[marvin_standup] LLM call failed")
            await interaction.followup.send(f"❌ 脫口秀生成失敗: {exc}")
            return

        if not standup_text:
            await interaction.followup.send("❌ 脫口秀生成結果為空。")
            return

        await interaction.followup.send(f"🎤 **馬文的個人脫口秀：{selected_topic}**\n「{standup_text}」")

        self._tts_interrupted = False
        _prev_protected = self._tts_protected
        self._tts_protected = True
        try:
            await self.play_tts(standup_text, already_in_channel=True, protected=True)
        finally:
            self._tts_protected = _prev_protected

    @app_commands.command(name="marvin_status", description="[Agent Report] 查看馬文對你這卑微人類的觀察報告")
    async def marvin_status(self, interaction: discord.Interaction, target: discord.Member = None):
        await interaction.response.defer(thinking=True)
        target_user = target or interaction.user
        mem = self.bot.router.memory.get_player_memory(target_user.display_name)
        stats = mem.get("stats", {"interaction_count": 0, "pos_feedback": 0, "neg_feedback": 0})
        fragments = len(mem.get("likes", [])) + len(mem.get("dislikes", [])) + sum(1 for v in mem.get("personal_info", {}).values() if v)
        comment = await self.bot.router.generate_status_report_comment(target_user.display_name, stats, fragments)
        
        embed = discord.Embed(
            title=f"📋 馬文的低階觀察報告：{target_user.display_name}",
            description=f"「{comment}」",
            color=discord.Color.dark_grey(),
            timestamp=datetime.datetime.now()
        )
        embed.set_thumbnail(url=target_user.display_avatar.url)
        embed.add_field(name="🧬 厭世程度", value=f"{self.bot.router.dna.get('toxicity', 10)}/10", inline=True)
        embed.add_field(name="🧠 人格標籤", value=f"{self.bot.router.dna.get('persona_tag', '厭世機器人馬文')}", inline=True)
        embed.add_field(name="🗑️ 腦內垃圾數", value=f"{fragments} 片", inline=True)
        embed.add_field(name="💬 浪費時間次數", value=f"{stats['interaction_count']} 次", inline=True)
        embed.add_field(name="💖 微弱亮點", value=f"{stats['pos_feedback']} 次", inline=True)
        embed.add_field(name="💢 絕望時刻", value=f"{stats['neg_feedback']} 次", inline=True)
        
        footer_scrap = await self.bot.router.generate_dynamic_system_msg("report_sent")
        embed.set_footer(text=f"⚙️ {footer_scrap}")
        await interaction.followup.send(embed=embed)
    @staticmethod
    def _fmt_pool_status(rows: list[dict]) -> str:
        """把 CooldownAwarePool.status() 排成 embed 行：狀態 emoji + 名稱 + TPM%/冷卻。

        只呈現 pool 真知道的（滾動 60s TPM + 冷卻），不估 TPD（本地計數會低估、會騙人）。
        """
        if not rows:
            return "（無 endpoint — 檢查 API key）"
        _emoji = {"available": "✅", "cooldown": "🧊", "tpm_high": "🟡"}
        lines = []
        for r in rows:
            e = _emoji.get(r["status"], "❔")
            tail = (f"冷卻 {r['cooldown_remaining']:.0f}s" if r["status"] == "cooldown"
                    else f"{r['tpm_pct']:.0f}% TPM")
            lines.append(f"{e} `{r['name']}` · {tail}")
        return "\n".join(lines)

    @staticmethod
    def _fmt_quality_today(rows: list[dict]) -> str:
        """當日品質指標四行（per feedback_marvin_quality_metrics）。無樣本＝剛重啟/還沒對話。"""
        fr = summarize_false_responding(rows)
        rk = summarize_latency([r for r in rows if r.get("metric") == "react"], "react_ms")
        it = summarize_interruption(rows)
        it_idle = summarize_interruption(rows, idle_only=True)
        rc = summarize_recall(rows)
        lines = []
        lines.append(f"⏱️ 反應: p50 {rk['p50']:.0f}ms / p95 {rk['p95']:.0f}ms (n={rk['count']})"
                     if rk["count"] else "⏱️ 反應: 今日無樣本")
        lines.append(f"🗣️ 誤回應: {fr['false_rate'] * 100:.0f}% (n={fr['total']})"
                     if fr["total"] else "🗣️ 誤回應: 今日無樣本")
        lines.append(f"✂️ 打斷: {it['interrupt_rate'] * 100:.0f}% (淨 {it_idle['interrupt_rate'] * 100:.0f}%, n={it['total']})"
                     if it["total"] else "✂️ 打斷: 今日無樣本")
        lines.append(f"🧠 記憶 recall: {rc['accuracy'] * 100:.0f}% (n={rc['total']})"
                     if rc["total"] else "🧠 記憶 recall: 每週一 probe")
        return "\n".join(lines)

    @app_commands.command(name="marvin_system", description="[System] 查看馬文的核心系統、網路備援與配額狀態")
    async def marvin_system(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        router = self.bot.router
        
        # Determine Limit Status
        budget_status = router.budget.get_info()
        used_pct = budget_status["percentage"]
        used_k = budget_status["used"] // 1000
        max_k = budget_status["max"] // 1000
        remaining_pct = max(0, 100 - used_pct)

        if router.is_exhausted or used_pct >= 100:
            limit_status = "🚨 嚴重 (主要 API 額度已耗盡，雲端防護鎖定中)"
            budget_color = discord.Color.red()
        elif router.budget.is_circuit_open() or used_pct >= 95:
            limit_status = "⚠️ 警告 (花費預算達日上限觸發熔斷)"
            budget_color = discord.Color.orange()
        elif used_pct >= 80:
            limit_status = "🟡 注意 (用量偏高)"
            budget_color = discord.Color.yellow()
        else:
            limit_status = "✅ 正常"
            budget_color = discord.Color.dark_grey()

        bar_filled = int(used_pct / 10)
        budget_bar = "█" * bar_filled + "░" * (10 - bar_filled)
        budget_line = f"`[{budget_bar}]` {used_pct:.1f}% 已用\n{used_k}k / {max_k}k tokens　剩餘 **{remaining_pct:.1f}%**"

        embed = discord.Embed(
            title="⚙️ 馬文系統診斷報告",
            description="「既然你那麼好心要幫我檢查身體，那我只好把這些無聊的數據攤在陽光下了。」",
            color=budget_color,
            timestamp=datetime.datetime.now()
        )
        embed.add_field(name="🧠 當前運算層級", value=f"**{router.current_tier}**", inline=False)
        embed.add_field(name="☁️ Tier-1 主大腦", value=f"`{router.model_name}`\n限制狀態: {limit_status}", inline=False)
        embed.add_field(name="💰 今日 Token 用量 (Gemini)", value=budget_line, inline=False)
        
        # TTS info
        tts_name = self.bot.tts._speaker if hasattr(self.bot, 'tts') else "zh-TW-YunJheNeural"
        embed.add_field(name="🗣️ 發聲模組 (TTS)", value=f"`Edge-TTS: {tts_name}`\n狀態: 運作中", inline=False)

        # 算力池（cleaner / curation resolver / feedback_analyzer 共用的 quick/analyze 兩層）
        # 顯示即時狀態 + TPM%（滾動 60s）；✅可用 / 🧊冷卻中 / 🟡TPM近上限
        tier_router = getattr(router, "_stt_router", None)
        if tier_router is None:
            embed.add_field(name="✨ 語音清洗算力池",
                            value="尚未初始化（lazy build，等第一次清洗才建池）", inline=False)
        else:
            embed.add_field(name="🪶 輕量池 quick（STT 清洗主力）",
                            value=self._fmt_pool_status(tier_router.quick_pool.status()), inline=False)
            embed.add_field(name="🧠 分析池 analyze（curation / feedback）",
                            value=self._fmt_pool_status(tier_router.analyze_pool.status()), inline=False)

        # 今日品質指標（react / 誤回應 / 打斷 / recall）— 讀當日 quality_metrics.jsonl
        try:
            _today = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            _q_rows = read_metrics(since_ts=_today)
            embed.add_field(name="📊 今日品質指標", value=self._fmt_quality_today(_q_rows), inline=False)
        except Exception as _e:
            logger.debug(f"⚠️ [marvin_system] 品質指標讀取失敗: {_e}")

        await interaction.followup.send(embed=embed)


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

    @app_commands.command(name="marvin_radio", description="[Radio] 啟動/停止 Marvin 電台，隨機播放 assets/songs 中的歌曲")
    @app_commands.describe(action="start=強制啟動, stop=強制停止, 不填=切換狀態")
    @app_commands.choices(action=[
        app_commands.Choice(name="start — 啟動電台", value="start"),
        app_commands.Choice(name="stop — 停止電台", value="stop"),
    ])
    async def marvin_radio(self, interaction: discord.Interaction, action: str = "toggle"):
        await interaction.response.defer(ephemeral=False)

        if action == "toggle":
            action = "stop" if self.radio_mode else "start"

        if action == "start":
            if self.radio_mode:
                await interaction.followup.send("📻 電台已經在播放了。就算宇宙正在崩塌，至少還有音樂。")
                return
            
            # 🚀 [Guild-Aware Fix] 檢查當前伺服器是否已有連線
            vc = interaction.guild.voice_client
            if not vc:
                # 🛡️ [Usability Fix] 檢查使用者是否在頻道，引導召喚
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

    # --- [🎵 Stream Commands] ---

    @app_commands.command(name="marvin_play", description="[Stream] 播放 YouTube 音樂，輸入歌名或貼上連結")
    @app_commands.describe(query="歌名（例如：周杰倫 稻香）或 YouTube 連結")
    async def marvin_play(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer(ephemeral=False)
        vc = interaction.guild.voice_client
        if not vc:
            await interaction.followup.send("❌ 馬文不在語音頻道中。請先使用 `/summon` 召喚我。", ephemeral=True)
            return

        username = interaction.user.display_name

        # 偵測 STT 修正：僅在上次是語音搜尋、且兩者字串相似度夠高時才記錄修正
        # 若兩首歌完全不同，視為新點歌，不觸發修正學習
        _history_kws = ["喜歡的歌", "我的歌單", "曾點過的歌", "曾經點過", "愛歌", "常聽的歌"]
        if hasattr(self.bot, 'music_memory') and not any(kw in query for kw in _history_kws):
            last = self._last_search.get(username)
            if last and time.time() - last['ts'] < 300 and last.get('source') == 'voice':
                old_q = last.get('query', '')
                if old_q and old_q != query and len(old_q) > 1:
                    # 只有「舊查詢是新查詢的子串（版本指定）」或「字串相似度 >= 60%（真正糾錯）」才記憶
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

        # 記錄點歌歷史
        if not is_random_history and hasattr(self.bot.router.memory, 'add_song_history'):
            self.bot.router.memory.add_song_history(username, info['title'])

        self.stt_logger.info(
            f"[點歌-手動] 使用者={username} | 搜尋={query} | 結果={info['title']} / {info.get('uploader', '?')}"
        )

        # 存入搜尋追蹤，讓下次 manual 再次搜尋時可偵測版本/歌手偏好
        if not is_random_history:
            self._last_search[username] = {'query': query, 'ts': time.time(), 'source': 'manual'}

        if self.radio_mode:
            await self.stop_radio(reason="Stream 模式接管")

        info['requested_by'] = username
        if self._check_song_duplicate(url=info['url'], title=info['title'], username=username, check_history=False):
            await msg.edit(content=f"⏭️ 「{info['title']}」已在佇列待播了。")
            return
        self._queue_user_song(info)   # 自選曲 LIFO 插隊到待播一 + skip-override

        if not self.stream_mode:
            self.stream_mode = True
            self.stream_volume = 0.10
            if self.stream_task and not self.stream_task.done():
                self.stream_task.cancel()
            self.stream_task = asyncio.create_task(self._stream_loop())

        # 若已有現有的控制方塊，直接更新它而不新建一個
        existing_view = self._active_control_view
        if existing_view and getattr(existing_view, 'message', None):
            try:
                await existing_view.message.edit(embed=existing_view._build_embed(), view=existing_view)
                await msg.delete()
                return
            except Exception:
                pass  # 若舊訊息已刪除或失效，fallthrough 到建立新的

        view = PlayControlView(self)
        self._active_control_view = view
        await msg.edit(content=None, embed=view._build_embed(), view=view)
        view.message = msg

    @app_commands.command(name="marvin_skip", description="[Stream] 跳過當前播放的歌曲")
    async def marvin_skip(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not self.stream_mode:
            await interaction.followup.send("沒有歌曲在播放。虛無是這個宇宙的預設狀態。", ephemeral=True)
            return
        vc = interaction.guild.voice_client
        if vc and vc.is_playing():
            vc.stop_playing()
        await interaction.followup.send("⏭️ 已跳過。", ephemeral=True)

    @app_commands.command(name="marvin_play_control", description="[Stream] 播放控制台：音量、暫停、上下首、佇列管理")
    async def marvin_play_control(self, interaction: discord.Interaction):
        view = PlayControlView(self)
        self._active_control_view = view
        await interaction.response.send_message(embed=view._build_embed(), view=view)
        view.message = await interaction.original_response()

    @app_commands.command(name="marvin_recommend", description="[Stream] 讓馬文根據你的點播記憶推薦下一首")
    async def marvin_recommend(self, interaction: discord.Interaction):
        await interaction.response.defer()
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
        await self._auto_recommend(username)

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

    async def handle_summon(self, message: str = None):  # noqa: ARG002
        # 🚀 [Lifecycle Management] 啟動螢幕擷取 (視覺系統)
        if self.bot.vision_enabled and self.bot.screen_capture:
            print("👁️  啟動視覺系統擷取迴圈...", flush=True)
            asyncio.create_task(self.bot.screen_capture.start_capture_loop())

        # 🚀 [Bug Fix] 確保獲取正確的 VoiceClient
        vc = next((vc for vc in self.bot.voice_clients if vc.is_connected()), None)
        
        # 1. 🎵 [Operation Intro Theme] 優先播放進場音樂，用來遮掩 LLM 生成延遲
        # 💡 [Path Fix] 修正檔名大小寫 (Oh Marvin.mp3)
        intro_file = "assets/songs/Oh Marvin.mp3"
        if vc and os.path.exists(intro_file):
            print(f"🎸 [Intro] 偵測到進場音樂檔案: {intro_file}")
            before_opts = "-ss 00:01:32 -t 7"  # 1:32~1:39 約 7s
            if self._plan12:
                # 🎛️ [Plan 12] intro 進 mixer 音樂層（不 stop mixer、不烤 volume）；
                # 下方 greeting TTS 會自動 duck 它 → voice 清楚蓋在輕 intro 上（policy A）
                print("🎸 [Intro] Plan 12：intro → mixer 音樂層（greeting 將 duck）")
                src = discord.FFmpegPCMAudio(intro_file, before_options=before_opts, options="-vn")
                self._ensure_mixer_playing(vc)
                self._mixer.set_volume(0.7)
                self._mixer.set_music_source(BufferedF32MusicSource(S16ToF32MusicSource(src), buffer_frames=50))
            else:
                # 🚀 [Race Condition Fix] 確保清除之前的沉默破門音源或殘留音訊
                if vc.is_playing():
                    vc.stop_playing()
                ffmpeg_opts = "-filter:a volume=0.7"
                print(f"🎸 [Intro] 優先啟動進場音樂 (音量 70%): {intro_file}")
                vc.play(discord.FFmpegPCMAudio(intro_file, before_options=before_opts, options=ffmpeg_opts))
        else:
            if not vc: print("⚠️ [Intro] 跳過音樂：找不到連線中的 VoiceClient。")
            if not os.path.exists(intro_file): print(f"⚠️ [Intro] 跳過音樂：找不到檔案 {intro_file}")
            
        # 2. 🌸 [Greeting] 呼叫 LLM 產出動態登場台詞 (Operation Narcissus v2: 群體黑歷史掃描)
        human_members = []
        if vc and vc.channel:
            human_members = [m.display_name for m in vc.channel.members if not m.bot]
            
        print(f"👁️ [Summon Scan] 偵測到現場人類成員: {human_members}")

        # 🚀 [Parallel Warm-up] 若 summon 時已預熱 LLM，直接拿結果（通常已完成，幾乎零等待）
        _task = self._pending_greeting_task
        self._pending_greeting_task = None
        try:
            greeting = await _task if _task else await self.bot.router.generate_greeting(human_members)
        except Exception:
            greeting = await self.bot.router.generate_greeting(human_members)
        
        if self.active_text_channel:
            await self.active_text_channel.send(f"⚙️ **【馬文 降臨】**\n{greeting}")
        self.stt_logger.info(f"[BOT降臨] {greeting}")

        # 3. 播放語音
        # 登場台詞是一次性宣告，不應被進場音樂播放期間的人聲觸發的 interrupt guard 阻擋
        self._tts_interrupted = False
        self._tts_protected = True
        await self.play_tts(greeting, already_in_channel=True)
        self._tts_protected = False
        
        # 3.（原喚醒詞宣導 marvin_wakeword_short.mp3）2026-06-03 依用戶要求移除：登場只保留
        # 音樂 + 打招呼兩段，第三段語音包不再播放。

        sink = self.bot.engine.get_active_sink()
        if sink:
            sink.last_audio_packet_time = time.time()
        
        self.idle_streak = 0

    async def handle_dismiss(self):
        print("🛑 [系統指令] 執行 /dismiss 撤離程序。")

        # 📻 [Marvin Radio] 解散時一併停止電台
        if self.radio_mode:
            await self.stop_radio(reason="系統解散")
        for vc in self.bot.voice_clients:
            try:
                if vc.is_connected():
                    if hasattr(vc, 'stop_listening'):
                        vc.stop_listening()
                    await vc.disconnect(force=True)
            except Exception as e:
                print(f"⚠️ [Shutdown Warning] {e}")

        active_speakers = set(entry.get("speaker") for entry in self.log_buffer if entry.get("speaker"))
        for speaker in active_speakers:
            asyncio.create_task(self.bot.router.audit_player_memory(speaker))

        self.stt_logger.info(
            f"[系統撤離] 馬文離開語音頻道 | 本次對話成員={list(active_speakers) or '無'}"
        )
        self.active_text_channel = None
        self.log_buffer = []
        self.idle_streak = 0
        self.speech_buffers = {}
        for speaker, timer in self.speech_timers.items():
            timer.cancel()
        self.speech_timers = {}

        await self.bot.engine.clear_buffers()

        # 🚀 [Lifecycle Management] 停止螢幕擷取
        if self.bot.screen_capture:
            print("🛑 [Lifecycle] 停止視覺系統擷取迴圈...", flush=True)
            self.bot.screen_capture.stop()

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
                return
            else:
                # 已經完整，或者達到強制結算長度，直接進入後續流程
                self.user_sentence_buffer.pop(speaker, None)
                raw_text = combined_text
                timestamp = origin_ts
                
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
            _track_label = f"Track={'A' if track is None else track}"
            self.stt_logger.info(f"[⚡喚醒] [{speaker}] raw='{raw_text}' | {_track_label} | wake_intent={wake_intent}")
            
            # 排隊時改走文字頻道通知，不打斷當前語音播放
            queue_size = self.query_queue.qsize()
            if queue_size > 0 and self.active_text_channel:
                wait_msgs = [
                    f"💬 {speaker}，排隊中，等我說完。",
                    f"💬 {speaker}，聽到了，處理完前一個再輪到你。",
                    f"💬 {speaker}，我的大腦一次只能痛苦一件事，稍等。",
                ]
                asyncio.create_task(self.active_text_channel.send(random.choice(wait_msgs)))

            # ⏱️ [Latency] T0: wake hit (進 queue 那刻)
            self._latency_marks.mark_wake(speaker, time.time())
            await self.query_queue.put({
                "speaker":     speaker,
                "timestamp":   timestamp,
                "raw_text":    raw_text,
                "wake_intent": wake_intent,   # None = Track A (regex, 高信心)
                "wake_voice_score": _wake_voice_score,  # helper query 判定：沒喊馬文→低
                "wake_dom":    _wake_dom,               # 主導通道（task/info → helper）
                # ContextVar 不會跨 asyncio.Queue 邊界 — 手動 forward timing dict 給 consumer
                "_timing":     pipeline_timing.snapshot(),
            })

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

        # 🚀 [TTS Interrupt] 使用者開口時中斷 TTS 播放，若文字尚未在聊天室則補發
        if self.is_playing_audio and not self._tts_protected:
            vc = discord.utils.get(self.bot.voice_clients)
            if vc and vc.is_playing():
                vc.stop_playing()
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

    async def _update_emotion_from_audio(self, speaker: str, wav_bytes: bytes, text: str):
        """🎭 [Gemini Audio Emotion] 以實際語音音訊讓 Gemini 分析情緒，更新 user_emotion_cache。
        背景任務，失敗時靜默使用韻律情緒作為 fallback。"""
        try:
            from google.genai import types
            audio_part = types.Part.from_bytes(data=bytes(wav_bytes), mime_type="audio/wav")
            response = await asyncio.wait_for(
                self.bot.router.google_client.aio.models.generate_content(
                    model="gemini-2.0-flash-lite",
                    contents=[
                        audio_part,
                        f'說話者說：「{text}」。只輸出一個英文情緒詞：excited / frustrated / amused / sarcastic / neutral / sad / angry'
                    ],
                    config={"max_output_tokens": 5, "temperature": 0.0}
                ),
                timeout=3.0
            )
            if response and response.text:
                emotion = response.text.strip().lower().split()[0]
                if emotion in {"excited", "frustrated", "amused", "sarcastic", "neutral", "sad", "angry"}:
                    prev = self.user_emotion_cache.get(speaker, "neutral")
                    self.user_emotion_cache[speaker] = emotion
                    logger.info(f"🎭 [Audio Emotion] {speaker}: {prev} → {emotion} (Gemini)")
        except asyncio.TimeoutError:
            logger.debug(f"⏱️ [Audio Emotion] {speaker} 逾時，保留韻律情緒標籤。")
        except Exception as e:
            logger.debug(f"⚠️ [Audio Emotion] {speaker} 分析失敗: {e}")

    async def _classify_marvin_self_emotion(self, speaker: str, full_text: str):
        """🎭 [Approach B] 在背景對 Marvin 自己的回應文字做情緒分類，結果存入 marvin_self_emotion[speaker]。
        不阻塞 TTS 播放；失敗時靜默保留原值。"""
        _t0 = time.monotonic()
        try:
            groq = getattr(self.bot.router, 'groq_dedicated_client', None)
            model = getattr(self.bot.router, 'groq_simple_model', None)
            if not groq or not model:
                return
            prompt = (
                "只輸出一個英文情緒詞：frustrated / amused / sarcastic / sad / angry / neutral\n"
                + full_text[:300]
            )
            resp = await asyncio.wait_for(
                groq.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=5,
                    temperature=0.0,
                ),
                timeout=5.0,
            )
            _words = resp.choices[0].message.content.strip().lower().split()
            word = _words[0] if _words else ""
            _VALID = {"frustrated", "amused", "sarcastic", "sad", "angry", "neutral"}
            if word in _VALID:
                self.marvin_self_emotion[speaker] = word
                elapsed = (time.monotonic() - _t0) * 1000
                logger.info(f"🎭 [Approach B] {speaker}: Marvin self-emotion={word} ({elapsed:.0f}ms)")
        except asyncio.TimeoutError:
            logger.warning(f"⏱️ [Approach B] {speaker} 情緒分類逾時，跳過。")
        except Exception as e:
            logger.warning(f"⚠️ [Approach B] {speaker} 情緒分類失敗: {e}")

    def _classify_emotion(self, prosody_data: dict) -> str:
        """
        🎭 [Operation Emotion Inference]
        根據韻律元數據推測說話者的情緒狀態。
        輸入：prosody_data dict（來自 VoiceMetaAnalyzer.calculate_prosody）
        回傳：單一情緒標籤字串
          - excited   : 高語速 + 高音量起伏 → 興奮/激動
          - impatient : 高語速 + 低音量起伏 → 急躁/緊張
          - depressed : 低語速 + 低音量起伏 → 沮喪/疲憊
          - hesitant  : 低語速 + 高音量起伏 → 猶豫/掙扎
          - robotic   : 正常語速 + 極低起伏 → 機械感（同類共鳴）
          - neutral   : 其他情況
        """
        if not prosody_data:
            return "neutral"

        wps = prosody_data.get("wps", 0)
        variance = prosody_data.get("energy_variance", 0)
        duration = prosody_data.get("physical_duration", 0)
        char_count = prosody_data.get("char_count", 0)

        # 防止語音過短造成的雜訊（少於 0.8s 或少於 3 個字）
        if duration < 0.8 or char_count < 3:
            return "neutral"

        # 情緒推測優先順序（越具體的判斷越優先）
        if wps > 6.0 and variance > 50:
            return "excited"           # 快 + 起伏大 = 興奮/激動
        elif wps > 6.0:
            return "impatient"         # 快 + 平穩 = 急躁/緊張
        elif wps < 1.5 and variance < 30:
            return "depressed"         # 慢 + 平穩 = 沮喪/疲憊
        elif wps < 1.5:
            return "hesitant"          # 慢 + 起伏 = 猶豫/掙扎
        elif 0 < variance < 20:
            return "robotic"           # 正常速度 + 極平穩 = 機械共鳴
        else:
            return "neutral"

    async def _send_noise_nudge(self, speaker: str) -> None:
        """🔇 [Noise Nudge] 環境噪音害喚醒被擋 → 文字頻道一句溫和提醒（每 speaker 每 session 一次）。"""
        if not self.active_text_channel:
            return
        try:
            await self.active_text_channel.send(
                f"🔇 {speaker} 我有聽到你在叫我，但你那邊背景有點吵聽不太清楚 😅 "
                f"開一下 Discord 的 Krisp 噪音抑制（設定 → 語音與視訊 → 噪音抑制），"
                f"或 Apple 裝置的人聲隔離，會清楚很多。"
            )
        except Exception as e:
            logger.debug(f"[Noise Nudge] 發送失敗：{e}")

    async def _send_mood_sticker(self, response_text: str, speaker: str = "", context: str = "") -> None:
        """🎭 [Sticker] 依 Marvin 心情選一張 Clyde 貼圖發送至 active_text_channel。"""
        if not self.active_text_channel:
            return
        if not hasattr(self.bot, "sticker_manager"):
            return
        from sticker_manager import infer_mood
        if context == "greeting":
            mood = "greeting"
        elif context == "farewell":
            mood = "farewell"
        else:
            toxicity = self.bot.router.dna.get("toxicity", 5)
            user_emotion = self.user_emotion_cache.get(speaker, "neutral")
            mood = infer_mood(response_text, toxicity, user_emotion)
        await self.bot.sticker_manager.send(self.active_text_channel, mood)

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

        # 🎵 [IBA Tier 0] 音樂控制直達 — 無歧義控制詞直接執行，不需喚醒詞
        # stream_mode 外也允許直接點歌（_MUSIC_PLAY_KW 命中時）
        # 🛡️ [Anti-Duplicate] 若 5 秒內已有 fast wake 處理同一發言，跳過此路徑避免雙重點歌
        _last_fast_wake = getattr(self, "last_wake_time", {}).get(speaker, 0)
        _recently_fast_woken = (time.time() - _last_fast_wake) < 5.0
        _direct_cmd = self._detect_music_direct_command(full_raw_text, stream_mode=self.stream_mode)
        if _direct_cmd:
            if _recently_fast_woken:
                logger.debug(f"🎵 [IBA-T0 Skip] {speaker} fast wake 5s 內已處理，跳過 debounced 音樂直達")
            else:
                _cmd_action = _direct_cmd.get("action", "stop")
                self.deferred_wakes.pop(speaker, None)
                if _cmd_action == "play":
                    # 🎵 [IBA-T0→Bus] no-wake 點歌改走 IntentBus，享三檔分流 + resolver
                    # （CURATION/DIRECTIONAL 補完），不再把原字串直送 yt-dlp 搜出垃圾。
                    # 控制詞（skip/pause/resume/stop）不需解析，維持直達。
                    _nw_ctx = build_nowake_play_ctx(
                        speaker, full_raw_text, _direct_cmd.get("query", ""),
                        stream_active=self.stream_mode,
                        is_owner=self._is_owner_speaker(speaker),
                    )
                    logger.info(f"🎵 [IBA-T0→Bus] {speaker} no-wake 點歌進 bus | query='{_nw_ctx.query[:40]}'")
                    pipeline_timing.mark("intent_dispatched")
                    pipeline_timing.emit(speaker, full_raw_text, suffix=" route=nowake_music")
                    asyncio.create_task(self._intent_bus.dispatch(_nw_ctx))
                else:
                    logger.info(f"🎵 [IBA-T0] {speaker} 直接音樂控制 cmd={_cmd_action} (no wake) | '{full_raw_text[:40]}'")
                    asyncio.create_task(self._safe_music_command(speaker, full_raw_text, _cmd_action))
                return

        # 🔎 [Find-Song] no-wake「找 + 音樂錨點」→ 進 bus 由 FindSongAgent 識別後播放。
        # gate 與 agent patterns 對齊；gate 過但 agent 不接 → bus 回 None → drop（同既有 no-wake 行為）。
        if not _recently_fast_woken and _FIND_SONG_GATE.search(full_raw_text):
            self.deferred_wakes.pop(speaker, None)
            _fs_ctx = IntentContext(
                speaker=speaker, raw_text=full_raw_text, query=full_raw_text,
                original_raw=full_raw_text, wake_intent=None,
                stream_active=self.stream_mode, game_mode=False,
                is_owner=self._is_owner_speaker(speaker), now=time.time(),
                mode=("stream" if self.stream_mode else "normal"),
            )
            logger.info(f"🔎 [Find-Song] {speaker} no-wake 找歌進 bus | '{full_raw_text[:40]}'")
            pipeline_timing.mark("intent_dispatched")
            pipeline_timing.emit(speaker, full_raw_text, suffix=" route=find_song")
            asyncio.create_task(self._intent_bus.dispatch(_fs_ctx))
            return

        # 🎵 [IBA Tier 1] 音樂資訊查詢直達 — 播放中被問「這首叫什麼」直接回答，不走 LLM
        if self.stream_mode and self._current_stream_info and _MUSIC_INFO_RE.search(full_raw_text):
            if not _recently_fast_woken:
                self.deferred_wakes.pop(speaker, None)
                asyncio.create_task(self._handle_music_info_query(speaker, full_raw_text))
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
        """
        logger.info("🚀 [Fast System] 指令隊列處理器已啟動。")
        while True:
            try:
                task_data = await self.query_queue.get()
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
                    self.query_queue.task_done()
                    continue

                # 立即播 filler（延遲遮掩）
                if raw_text:
                    self._speaker_lang[speaker] = self._detect_text_lang(raw_text)
                asyncio.create_task(self._play_ack("filler", speaker=speaker))

                # 多回合確認流程，回傳最終確認的問句
                confirmed_query = await self._confirmation_flow(speaker, timestamp, initial_text=raw_text)
                if confirmed_query:
                    await self._process_queued_query(speaker, timestamp, override_query=confirmed_query, wake_intent=_wi, original_raw=raw_text, wake_voice_score=_wvoice, wake_dom=_wdom)

                self.query_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"❌ [Fast System worker] 錯誤: {e}")
                await asyncio.sleep(1)

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
                self._ensure_mixer_playing(_vc)
                self._mixer.push_tts(f32)
                logger.info(f"🗣️ [Ack:{category_key}] 播放 {variant or os.path.basename(ack_file)}")
        except Exception as e:
            logger.warning(f"[Ack:{category_key}] 播放失敗（忽略）：{e}")
        return

    async def _confirmation_flow(self, speaker: str, wake_time: float, initial_text: str = "") -> str | None:
        """
        取得問句後直接回答，不做 TTS 確認環節。
        - 問句已在喚醒句中：立即返回，零等待
        - 問句為空：等待後續 STT（最多 10 秒），逾時才提示重說
        """
        evt = asyncio.Event()
        self.speaker_dialogue_states[speaker] = {"state": "awaiting_question", "event": evt, "question": ""}

        stripped = self._strip_wake_word(initial_text) if initial_text else ""
        if len(stripped) < 4:
            raw_query = self.bot.engine.conv_buffer.get_harvest(wake_time, before=3.0, after=2.0, speaker=speaker)
            stripped = self._strip_wake_word(raw_query) if raw_query else stripped
        if len(stripped) >= 4:
            # 問句已在喚醒句裡，直接用
            self.speaker_dialogue_states.pop(speaker, None)
        else:
            # 問句為空（玩家只說了喚醒詞），等後續語音
            try:
                await asyncio.wait_for(evt.wait(), timeout=10.0)
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
        weak_patterns = [
            "不知道", "我不確定", "無法回答", "不清楚", "沒辦法回答", "不太清楚",
            "無法確定", "沒有足夠", "需要更多", "請提供", "再說清楚",
            "你是指", "你的意思是", "這取決於", "作為一個", "讓我先",
        ]
        return any(pattern in cleaned for pattern in weak_patterns)

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
            if placeholder_msg:
                await placeholder_msg.edit(content=f"{_head} `{speaker}`：{full_text} (大腦連結中斷)")
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
        return None

    def _check_song_duplicate(self, url: str, title: str, username: str,
                              *, check_history: bool = True) -> bool:  # noqa: ARG002
        """回傳 True 表示此 session 已有相同 URL，應跳過加入佇列。

        check_history=False：只擋「還在佇列」，不擋「本場播過」。給使用者**手動**點播用——
        skip 過的歌進了 stream_history，但手動點回來是刻意正向更正，應放行（見 _queue_user_song）。
        """
        for item in self.stream_queue:
            if item.get("url") == url:
                return True
        if check_history:
            for item in self.stream_history:
                if item.get("url") == url:
                    return True
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
        """使用者自選曲照點歌順序排（FIFO）：插在既有使用者曲之後、auto-recommend
        （Marvin ambient，append 在尾）之前。Marvin 自動點歌順位最低、永遠被往後推。
        _stream_loop 用 pop(0) 取歌，插在使用者區尾端 = 不打斷正在播的那首。

        skip-override：手動點播 = 刻意正向更正，蓋過先前的 skip——記 played_again（latest-wins
        覆蓋舊 skipped，見 music_memory.get_skipped_titles）+ 重置 consecutive-skip 計數，
        讓這首不再被 auto-recommend 排除。"""
        self.stream_queue.insert(self._user_song_insert_index(self.stream_queue), info)
        try:
            user = info.get('requested_by') or ''
            title = info.get('title') or ''
            mm = getattr(self.bot, 'music_memory', None)
            if mm and user and title:
                mm.add_recommendation_feedback(user, title, "played_again")
            self._consecutive_skips_by_url.pop(info.get('url') or '', None)
            # 使用者點歌 → 更新 T2 推薦 seed（radio 跟著最近點的歌走，而非舊 liked 歷史）
            _m = re.search(r"(?:v=|youtu\.be/|/watch\?v=)([A-Za-z0-9_-]{11})",
                           info.get('webpage_url') or '')
            if _m:
                self._last_user_song_seed = _m.group(1)
        except Exception:
            logger.debug("[Queue] skip-override / seed 更新失敗", exc_info=True)

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
        return None

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

    def _build_recommendation_extras(self) -> dict:
        """Phase 1 豐富化（2026-05-28）：給 recommendation log 灌 controller scope 的
        rich context。read-only / sync — 沒有 LLM call，不阻塞 bus dispatch。

        - vibe_mood: 從 MoodSensor cache 讀（無 cache 回 None；不強制 refresh 避免 LLM）
        - queue_depth: stream_queue 長度
        - recent_history_titles: 最近 3 首歷史（dict 中的 title 欄位）
        """
        extras: dict = {
            "queue_depth": len(self.stream_queue),
            "recent_history_titles": [
                s.get("title", "") for s in self.stream_history[-3:]
                if isinstance(s, dict)
            ],
        }
        # MoodSensor 有 5min cache，sync 讀 _cache attribute 安全（永不寫）
        ms = getattr(self, "_mood_sensor", None)
        if ms is not None:
            cached_vibe = getattr(ms, "_cache", None)
            if cached_vibe is not None:
                extras["vibe_mood"] = getattr(cached_vibe, "mood", None)
        return extras

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
        """B1: bus 接走 intent 時，取消 dangling speculative LLM prefetch。

        Why: speculative prefetch 在 wake hit 那刻就啟動（避免 LLM 冷啟動）。
        若 bus dispatch 接走（music / nemoclaw），LLM 路徑跑不到，prefetch task
        會變孤兒 — 繼續跑燒 quota，且 1976 行 race window 內可能被下次 chat
        turn 拿來當起手回應（幻覺）。

        How: 從 router._pending_prefetch dict 取出該 speaker 的 task，若還沒
        done 就 cancel；無論如何都從 dict 移除。
        """
        prefetch_map = getattr(self.bot.router, "_pending_prefetch", None)
        if not isinstance(prefetch_map, dict):
            return
        task = prefetch_map.pop(speaker, None)
        if task is not None and not task.done():
            task.cancel()

    _MUSIC_CMD_DEDUP_WINDOW = 5.0  # 秒

    # 自動點播「播過拉長視窗」排除：此視窗內播過的歌不再自動點（非永久、防候選枯竭）。
    # 6/14 使用者回報重複性高 → 從本場 15 首擴成 7 天跨重啟視窗；skip 過的另永久排除。
    _PLAYED_EXCLUDE_TTL_S = 7 * 24 * 3600

    def _record_song_skip(self) -> None:
        """把當前播放歌曲的 videoId 記入持久化 skip 排除集。

        兩條 skip 路徑共用（IBA-T0 _handle_voice_music_command + 後喚醒
        PlaybackControlAgent）。fail-open：拿不到歌/mm 不存在 → no-op，絕不
        影響 skip 本身執行。
        """
        mm = getattr(self.bot, "music_memory", None)
        cur = self._current_stream_info
        if mm is None or not cur:
            return
        url = cur.get("webpage_url") or cur.get("url") or ""
        if url:
            try:
                mm.record_skipped_video_id(url)
                # Step 3 retreat：同時記藝人級 skip（≥2 首被 skip → explore 避開該方向）
                from taste_fingerprint import artist_of
                _artist = artist_of(cur.get("title", ""))
                if _artist:
                    mm.record_artist_skip(_artist, url)
            except Exception:
                logger.exception("[Skip] record_skipped_video_id 失敗")

    async def _safe_music_command(self, speaker: str, query: str, cmd: str):
        """Top-level wrapper：任何 music command 路徑都該過這層 try/except。

        Why: 5/18 17:51 incident — _handle_voice_music_command 內某處 6ms 內
        就 raise Errno 11，但 retry 沒觸發代表錯誤不在 yt-dlp，是更早的 code。
        過去這類錯誤被 [Fast System worker] except 吞掉沒 traceback，user 體感
        是「點歌沒反應」silent fail。

        修法：top-level try/except 捕全部 exception，log 完整 traceback，
        並貼錯誤訊息到 channel 讓 user 知道發生什麼事（discoverable failure）。
        """
        try:
            await self._handle_voice_music_command(speaker, query, cmd)
        except Exception as e:
            logger.error(
                f"❌ [Music Command Crash] {speaker} {cmd} '{query[:40]}': "
                f"{type(e).__name__}: {e}",
                exc_info=True,  # full traceback
            )
            asyncio.create_task(self._play_ack("music_fail", speaker=speaker))
            ch = self.active_text_channel
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
        logger.info(f"🎵 [Music Command] {speaker} 觸發語音音樂指令: {cmd} | query='{query[:40]}'")
        # 🎵 [Music Ack] MusicAgent 接走 → 立刻播音樂 ack（從 acks/music/ 抽 4 字內 DJ 款）
        # 只在 play 觸發；skip/stop/pause/resume 是控制指令，用 wake-time 通用 filler 即可。
        if cmd == "play":
            asyncio.create_task(self._play_ack("music", speaker=speaker))
        ch = self.active_text_channel
        vc = next((v for v in self.bot.voice_clients if v.is_connected()), None)

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

        import random

        if cmd == "skip":
            if not self.stream_mode and not self.radio_mode:
                if ch: await ch.send("😑 沒有歌在播，要我跳過什麼？")
                return
            self._record_song_skip()  # video-id 進永久 skip 排除集（在 stop 前，info 還在）
            if self._mixer is not None:
                self._mixer.clear_music()
            reply = random.choice(replies["skip"])
            if ch: await ch.send(reply)
            self.stt_logger.info(f"[音樂控制→{speaker}] 指令=skip | bot={reply} (plan12=True)")

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
            self.stt_logger.info(f"[音樂控制→{speaker}] 指令=stop | bot={reply}")

        elif cmd == "pause":
            if not self.stream_mode and not self.radio_mode:
                if ch: await ch.send("😑 沒有在播可以暫停。")
                return
            if not vc:
                if ch: await ch.send("😑 找不到語音連線。")
                return
            if self.stream_mode and not self.stream_paused:
                if self._mixer is not None:
                    self._mixer.set_paused(True)
                self.stream_paused = True
            elif self.radio_mode and not self.stream_mode and not self.radio_paused:
                if self._mixer is not None:
                    self._mixer.set_paused(True)
                self.radio_paused = True
            else:
                if ch: await ch.send("😑 已經在暫停了。")
                return
            reply = random.choice(replies["pause"])
            if ch: await ch.send(reply)
            self.stt_logger.info(f"[音樂控制→{speaker}] 指令=pause | bot={reply} (plan12=True)")

        elif cmd == "resume":
            if not self.stream_paused and not self.radio_paused:
                if ch: await ch.send("😑 沒有東西在暫停。")
                return
            if not vc:
                if ch: await ch.send("😑 找不到語音連線。")
                return
            if self.stream_paused:
                if self._mixer is not None:
                    self._mixer.set_paused(False)
                self.stream_paused = False
            elif self.radio_paused:
                if self._mixer is not None:
                    self._mixer.set_paused(False)
                self.radio_paused = False
            reply = random.choice(replies["resume"])
            if ch: await ch.send(reply)
            self.stt_logger.info(f"[音樂控制→{speaker}] 指令=resume | bot={reply} (plan12=True)")

        elif cmd == "play":
            search = self._extract_music_search_query(query)
            if not vc:
                if ch: await ch.send("❌ 我不在語音頻道中，先用 `/summon` 召喚我。")
                return
            if not search:
                if ch: await ch.send("🎵 要放什麼歌？你說了等於沒說。")
                return

            # 套用已知修正，並追蹤原始語音 query 供未來修正學習
            raw_search = search
            correction_note = ""
            wrong = None  # 預先 init：music_memory 不存在時下方 stt_logger
                          # access `wrong` 不致 UnboundLocalError
            if hasattr(self.bot, 'music_memory'):
                corrected, wrong = self.bot.music_memory.apply_stt_correction(speaker, search)
                if wrong:
                    search = corrected
                    correction_note = f" *(語音修正：{wrong} → {corrected})*"
            self._last_search[speaker] = {'query': raw_search, 'ts': time.time(), 'source': 'voice'}

            if ch:
                status_msg = await ch.send(f"🔍 **正在搜尋：** `{search}`...{correction_note}")
            info = await self._resolve_yt_query(search)
            if not info:
                if ch: await status_msg.edit(content=f"❌ 找不到 `{search}`，就跟意義一樣——不存在。")
                asyncio.create_task(self._play_ack("music_fail", speaker=speaker))
                return
            info['requested_by'] = speaker
            self.stt_logger.info(
                f"[點歌-語音] 使用者={speaker} | 搜尋={raw_search}{f' (修正→{search})' if wrong else ''} | 結果={info['title']} / {info.get('uploader', '?')}"
            )
            if self._check_song_duplicate(url=info['url'], title=info['title'], username=speaker, check_history=False):
                if ch: await status_msg.edit(content=f"⏭️ 「{info['title']}」已在佇列待播了。")
                return
            if self.radio_mode:
                await self.stop_radio(reason="語音音樂指令接管")
            self._queue_user_song(info)   # 自選曲 LIFO 插隊到待播一 + skip-override
            if not self.stream_mode:
                self.stream_mode = True
                self.stream_volume = 0.10
                if self.stream_task and not self.stream_task.done():
                    self.stream_task.cancel()
                self.stream_task = asyncio.create_task(self._stream_loop())
                # 整合通知到現有控制面板，避免出現兩個方塊
                existing_view = self._active_control_view
                if ch and existing_view and getattr(existing_view, 'message', None):
                    try:
                        await existing_view.message.edit(embed=existing_view._build_embed(), view=existing_view)
                        await status_msg.delete()
                    except Exception:
                        # 舊面板失效，建立新的 embed 控制面板
                        view = PlayControlView(self)
                        self._active_control_view = view
                        await status_msg.edit(content=None, embed=view._build_embed(), view=view)
                        view.message = status_msg
                elif ch:
                    # 沒有現有面板，建立新的 embed 控制面板
                    view = PlayControlView(self)
                    self._active_control_view = view
                    await status_msg.edit(content=None, embed=view._build_embed(), view=view)
                    view.message = status_msg
            else:
                existing_view = self._active_control_view
                if ch and existing_view and getattr(existing_view, 'message', None):
                    try:
                        await existing_view.message.edit(embed=existing_view._build_embed(), view=existing_view)
                        await status_msg.delete()
                    except Exception:
                        # 舊面板失效，重建 embed 控制面板
                        view = PlayControlView(self)
                        self._active_control_view = view
                        await status_msg.edit(content=None, embed=view._build_embed(), view=view)
                        view.message = status_msg
                elif ch:
                    # 串流已在跑但沒有面板，重建 embed 控制面板
                    view = PlayControlView(self)
                    self._active_control_view = view
                    await status_msg.edit(content=None, embed=view._build_embed(), view=view)
                    view.message = status_msg

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

    @tasks.loop(minutes=10.0)
    async def slow_system_loop(self):
        """[Slow System] 每 10 分鐘進行一次對話彙整與馬文評論"""
        try:
            if not self.bot.engine.conv_buffer:
                return
                
            # 1. 取得最近的增量對話紀錄
            # 🧬 [Incremental Fix] 由 Buffer 內部指標控制，杜絕跨任務重複性
            new_entries = self.bot.engine.conv_buffer.pop_new_entries()
            self.slow_loop_accumulator.extend(new_entries)
            
            # 🧬 [APM Economy] 判定是否具備足夠內容 (100字) 以觸發日記生成
            total_chars = sum(len(e.get("text", "")) for e in self.slow_loop_accumulator)
            
            if not self.slow_loop_accumulator or total_chars < 200:
                if not self.slow_loop_accumulator:
                    print("📭 [SlowLoop] 本輪無新對話，跳過摘要。", flush=True)
                else:
                    print(f"⏳ [SlowLoop] 內容不足 ({total_chars}/200 字)，繼續累積...", flush=True)
                
                # 🚀 [Proactive Social] 就算沒有對話，也檢查是否靜默過久 (Operation Social Gap)
                now = time.time()
                silence = now - self.last_player_speech_time

                # 📻 [Marvin Radio] 10 分鐘靜默自動啟動電台（stream_mode 播放中則跳過）
                if silence > 600 and not self.radio_mode and not self.stream_mode and self.bot.voice_clients:
                    print("🕒 [Slow System] 偵測到 10 分鐘靜默，自動啟動馬文電台...")
                    if self.active_text_channel:
                        await self.active_text_channel.send(
                            "📻 **【馬文電台：自動啟動】**\n十分鐘了... 你們都死了嗎。既然沒人說話，就讓我播點音樂填補這毫無意義的寂靜吧。"
                        )
                    await self.start_radio(trigger="10分鐘靜默自動")

                elif not self.radio_mode and now - self.last_proactive_time > 1800:
                    # 🔇 [Freq Adj Op 32] 依 24h 內嚴重率動態更新主動發話閾值
                    _feedback_path = os.path.normpath(
                        os.path.join(os.path.dirname(__file__), "..", "records", "response_feedback.jsonl")
                    )
                    try:
                        _cutoff = now - 86400  # 24h
                        _rows = []
                        if os.path.exists(_feedback_path):
                            with open(_feedback_path, "r", encoding="utf-8") as _f:
                                _lines = _f.readlines()[-20:]
                            import json as _json
                            for _line in _lines:
                                try:
                                    _row = _json.loads(_line)
                                    if _row.get("timestamp", 0) >= _cutoff:
                                        _rows.append(_row)
                                except Exception:
                                    pass
                        if len(_rows) >= 5:
                            _severe = sum(1 for r in _rows if r.get("reaction") == "嚴重")
                            _ratio = _severe / len(_rows)
                            # P0: 整體降一個量級（北極星 = 讓 bot 真的有機會發聲）
                            # 用戶嫌 bot 太吵時 ("嚴重" 比例 >30%) 才回升，正常情況 90s 就觸發
                            if _ratio > 0.30:
                                self.proactive_silence_threshold = 240
                            elif _ratio == 0.0:
                                self.proactive_silence_threshold = 90
                            else:
                                self.proactive_silence_threshold = 120
                            print(f"🔇 [Freq Adj] 嚴重={_ratio:.0%} ({len(_rows)}行/24h), proactive_silence_threshold={self.proactive_silence_threshold}s")
                    except Exception as _fe:
                        logger.warning(f"⚠️ [Freq Adj] 讀取 feedback 失敗: {_fe}")

                    # 🚀 [Proactive Social] 靜默主動發起話題
                    # 2026-05-26: 已遷至 SpeakBus（ProactiveTopicAgent），由 5s tick 統一 dispatch
                    # 保留 proactive_silence_threshold 動態調整（上面 _ratio 那段），agent 讀同一個值
                return
            
            # 使用最新條目的時間作為快照參考
            self.last_snapshot_time = max(e["timestamp"] for e in self.slow_loop_accumulator)
            print(f"🕒 [Slow System] 執行增量總結 (累積筆數: {len(self.slow_loop_accumulator)}, 總字數: {total_chars})...")

            # 遊戲期間不貼日記，避免打斷遊戲流程；留住累積器內容，等遊戲結束後繼續
            if self.bot.router.current_game:
                print(f"🎮 [SlowLoop] 遊戲進行中 ({self.bot.router.current_game})，跳過日記生成。")
                return

            # 將累積的內容取出進行處理，並清空累積器
            processing_entries = self.slow_loop_accumulator
            self.slow_loop_accumulator = []

            # 過濾馬文自己的回應：避免把 TTS 輸出當成對話內容餵回 diary
            human_entries = [e for e in processing_entries if e.get("speaker", "") != "Marvin"]
            if not human_entries:
                print("📭 [SlowLoop] 本輪只有馬文自言自語，跳過日記生成。")
                return

            # 2. 並行備料
            full_new_text = "\n".join([f"{e.get('speaker', '未知')}: {e.get('text', '...')}" for e in new_entries])
            online_members = self.get_online_members()
            can_analyze = self._stt_call_counter <= 10
            if not can_analyze:
                print(f"⚠️ [Slow System] STT 頻率過高 ({self._stt_call_counter}/min)，跳過本輪社交分析。")

            # 🔇 [社交補位 OFF — 2026-06-03] analyze_social_dynamics（社交知識圖譜，長上下文）
            # 的結果 analysis 唯一消費者就是下方社交補位；補位關閉時算了也直接丟掉 = 純浪費。
            # → 補位關閉就連這支 LLM 都不呼叫，省免費池。重啟：_SOCIAL_INTERVENTION_ENABLED=True，
            # call 與消費者一起復活。（記憶萃取早已改每日 off-peak，見下方 gather 註解。）
            _SOCIAL_INTERVENTION_ENABLED = False
            _do_social_analysis = can_analyze and _SOCIAL_INTERVENTION_ENABLED

            async def _noop(): return None

            # 3. 並行執行：日記 + 社交分析（記憶萃取改由每日 web LLM 整體處理）
            results = await asyncio.gather(
                self.bot.router.generate_slow_summary(human_entries),
                self.bot.router.analyze_social_dynamics(new_entries, full_new_text, online_members=online_members) if _do_social_analysis else _noop(),
                return_exceptions=True
            )
            summary  = results[0] if not isinstance(results[0], BaseException) else None
            analysis = results[1] if can_analyze and not isinstance(results[1], BaseException) else None

            # 4. 寫入本地日誌 (RAG 來源)
            def _write_rag_log(text):
                os.makedirs("records", exist_ok=True)
                with open("records/chat_summary_log.txt", "a", encoding="utf-8") as f:
                    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    f.write(f"[{ts}] --- 10分鐘對話總結 ---\n{text}\n\n")

            if summary is None:
                print("📭 [SlowLoop] LLM 判斷本輪內容不值得記錄，跳過發文。")
                await asyncio.to_thread(_write_rag_log, "[SKIPPED - 內容無新意]")
            else:
                await asyncio.to_thread(_write_rag_log, summary)

                # 5. 發送到專屬頻道 (#馬文的厭世日記)
                diary_channel = None
                if self.active_text_channel and self.active_text_channel.guild:
                    guild = self.active_text_channel.guild
                    diary_channel = discord.utils.get(guild.text_channels, name="馬文的厭世日記")
                    if not diary_channel:
                        diary_channel = discord.utils.get(guild.text_channels, name="marvin-diary")

                target = diary_channel
                if not target and self.active_text_channel and self.active_text_channel.guild:
                    try:
                        guild = self.active_text_channel.guild
                        print(f"🛠️ [Slow System] 嘗試為伺服器 '{guild.name}' 建立專屬日記頻道...")
                        target = await guild.create_text_channel(
                            name="馬文的厭世日記",
                            topic="Ambient Presence: 馬文在這裡默默鄙視所有人。",
                            reason="馬文的厭世日記系統啟動"
                        )
                    except Exception as e:
                        print(f"❌ [Slow System] 建立頻道失敗: {e}")
                        target = self.active_text_channel

                if target:
                    if self.pending_intervention:
                        unplayed_text = self.pending_intervention.get("text", "")
                        summary += f"\n\n*[未放送的內心獨白：{unplayed_text}]* (環境參數：Confidence={self.current_confidence}, VAD={self.current_vad_delay}s)"
                        old_path = self.pending_intervention.get("file_path")
                        if old_path and os.path.exists(old_path):
                            try: os.remove(old_path)
                            except: pass
                        self.pending_intervention = None

                    await target.send(f"📓 **【馬文的厭世日記】** (10min 增量彙整)\n\n{summary}")

            # 6. 處理社交缺口（使用並行取回的 analysis 結果）
            # 🔇 [社交補位 OFF — 2026-06-03] flag 與 analyze_social_dynamics call gate 都在上方
            #    （補位關閉時連社交分析 LLM 都不算，不空轉）。觸發源 Marvin Autonomous
            #    Intelligence v2.5；補位接話率僅 ~4%（records/speak_outcomes.jsonl）。
            if _SOCIAL_INTERVENTION_ENABLED and analysis:
                gap_type = analysis.get("social_gap", "none")
                if gap_type != "none":
                    # 用 suki_inner_monologue（已蒸餾的場景觀察）取代原始對話紀錄，避免 LLM 複述聊天內容
                    gap_context = analysis.get("suki_inner_monologue") or full_new_text
                    gap_response = await self.bot.router.generate_gap_filling_response(gap_type, gap_context)
                    if gap_response and self.active_text_channel:
                        print(f"🤫 [Social Awareness] 執行社交補位 ({gap_type})。")
                        await self._send_social_intervention_visual(gap_type, gap_response, gap_context)
                        self.stt_logger.info(f"[BOT慢循環補位] 類型={gap_type} | {gap_response[:120]}")
                        _last_spk = human_entries[-1]["speaker"] if human_entries else "頻道"
                        asyncio.create_task(self._schedule_reaction_check(
                            _last_spk, gap_response, time.time(),
                            wake_latency=None, atmosphere=None,
                        ))
                        # emotional_support = 抱怨共鳴，只發文字不打斷語音
                        if gap_type != "emotional_support":
                            asyncio.create_task(self.play_tts(gap_response, already_in_channel=True, silent_during_stream=True, priority=2))
        except Exception as e:
            logger.error(f"🚨 [Slow System Error] 背景循環發生異常 (已截斷防止崩潰): {e}")
            import traceback
            logger.error(traceback.format_exc())

    @tasks.loop(time=datetime.time(hour=12, minute=0, tzinfo=datetime.timezone(datetime.timedelta(hours=8))))
    async def daily_log_export_loop(self):
        """每天中午 12:00 (UTC+8) 將前一天的 STT log 與 feedback 另存為 records/daily/YYYY-MM-DD.log"""
        try:
            tz = datetime.timezone(datetime.timedelta(hours=8))
            now = datetime.datetime.now(tz)
            today_noon = now.replace(hour=12, minute=0, second=0, microsecond=0)
            yesterday_noon = today_noon - datetime.timedelta(days=1)
            date_label = today_noon.strftime("%Y-%m-%d")

            os.makedirs("records/daily", exist_ok=True)
            out_path = f"records/daily/{date_label}.log"

            lines = []

            # --- A. STT History ---
            lines.append(f"=== STT LOG ({yesterday_noon.strftime('%Y-%m-%d %H:%M')} ~ {today_noon.strftime('%Y-%m-%d %H:%M')}) ===\n")
            try:
                with open("stt_history.log", "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.rstrip("\n")
                        try:
                            # 格式: 2026-04-23 23:21:23,281 - [玩家] ...
                            dt_str = line[:23]
                            dt = datetime.datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=tz)
                            if yesterday_noon <= dt < today_noon:
                                lines.append(line + "\n")
                        except (ValueError, IndexError):
                            pass
            except FileNotFoundError:
                lines.append("(stt_history.log 不存在)\n")

            # --- B. Response Feedback ---
            lines.append(f"\n=== RESPONSE FEEDBACK ({date_label}) ===\n")
            feedback_count = 0
            try:
                with open("records/response_feedback.jsonl", "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                            ts = float(entry.get("timestamp", 0) or 0)
                            dt = datetime.datetime.fromtimestamp(ts, tz=tz)
                            if yesterday_noon.timestamp() <= ts < today_noon.timestamp():
                                lines.append(line + "\n")
                                feedback_count += 1
                        except (json.JSONDecodeError, ValueError):
                            pass
            except FileNotFoundError:
                lines.append("(response_feedback.jsonl 尚未建立)\n")

            content = "".join(lines)
            await asyncio.to_thread(lambda: open(out_path, "w", encoding="utf-8").write(content))
            logger.info(f"📋 [Daily Export] 已輸出 {out_path} ({len(lines)} 行 STT, {feedback_count} 筆 feedback)")

        except Exception as e:
            logger.error(f"❌ [Daily Export] 每日匯出失敗: {e}", exc_info=True)

    @tasks.loop(seconds=60.0)
    async def reset_stt_counter_loop(self):
        """[STT Rate Limit] 每分鐘重設 STT 計數器"""
        if self._stt_call_counter > 0:
            logger.debug(f"🧹 [Rate Limit] 重設 STT 計數器 (上分鐘總計: {self._stt_call_counter})")
        self._stt_call_counter = 0

    @tasks.loop(minutes=30.0)
    async def background_news_loop(self):
        """[Background News] 每 30 分鐘對在線玩家的喜好進行 DDG 更新，結果存入 news_queue。
        get_rich_context() 會在下次喚醒時自動注入最新一筆。"""
        online = self.get_online_members()
        if not online:
            return

        import random
        for player in online:
            try:
                mem = self.bot.router.memory.get_player_memory(player)
                likes = mem.get("likes", [])
                if not likes:
                    continue

                interest = random.choice(likes)
                results = await self.bot.router._execute_web_search(f"{interest} 新聞")
                if not results:
                    continue

                marvinized = await self.bot.router.marvinize_news(player, interest, results[:400])
                if marvinized:
                    self.bot.router.memory.enqueue_news(player, marvinized)
                    logger.info(f"📰 [BG News] {player} 新聞更新完成: {interest}")

            except Exception as e:
                logger.warning(f"⚠️ [BG News] {player} 新聞更新失敗: {e}")

            await asyncio.sleep(15)  # 每個玩家間隔 15 秒，避免 DDG rate limit

    # ── SpeakBus 5s idle tick（social-catalyst week1） ─────────────────────────
    # 沒 SpeakAgent 註冊時整段是 no-op；agent 進來後負責收 bid + 寫 outcome log。
    # 跑得起在 voice channel 內才有意義，沒連線就 early return（節省功耗）。

    def proactive_topic_on_cooldown(self, now: float | None = None) -> bool:
        """共用 proactive-topic cooldown 檢查。

        冷場 TopicGenerator 與 SpeakBus ProactiveTopicAgent 共用 last_proactive_time
        當單一 cooldown 來源：任一系統剛發話過 → True，呼叫端應跳過，避免使用者
        連續聽到兩套主動話題（功能重疊 OK，但不可連發）。
        """
        now = now if now is not None else time.time()
        return (now - self.last_proactive_time) < PROACTIVE_TOPIC_COOLDOWN_S

    def mark_proactive_topic_spoken(self, now: float | None = None) -> None:
        """任一 proactive-topic 系統發話後呼叫，戳共用 cooldown 時間戳。"""
        self.last_proactive_time = now if now is not None else time.time()

    def _compute_speak_mode(self) -> str:
        """Voice state → SpeakBus ctx.mode 字串。Precedence: game > stream > radio > normal。

        最受限的優先（game 中完全靜音、stream 中部分 agent 可走 hotswap）。
        SpeakBus 用此值對 agent.mode_compatible 做 gate；新 agent 只宣告 frozenset
        即可，不用各自 if-game/stream/radio 重複檢查。
        """
        if getattr(self, "game_mode", False):
            return "game"
        if getattr(self, "stream_mode", False):
            return "stream"
        if getattr(self, "radio_mode", False):
            return "radio"
        return "normal"

    def _build_speak_context(
        self, trigger: str,
        *, last_speaker: str | None = None, last_text: str | None = None,
    ) -> SpeakContext:
        """從 voice_controller 當下狀態組 SpeakContext。Pure-ish（只讀 self，不做 IO）。

        post_utterance trigger 要帶 last_speaker / last_text 給 BridgeAgent 用。
        """
        now = time.time()
        ch = self.active_text_channel
        return SpeakContext(
            channel_id=ch.id if ch else 0,
            guild_id=ch.guild.id if ch else 0,
            silence_seconds=max(0.0, now - self._last_room_stt_time) if self._last_room_stt_time else 0.0,
            present_speakers=self.get_online_members(),
            room_mood=self._room_mood_store.get(0),    # week2: DuckingAgent 寫的 hot_chat flag 在這
            recent_utterances=[],                      # 預留；agent 自己拉 transcript 即可
            trigger=trigger,
            mode=self._compute_speak_mode(),
            last_speaker=last_speaker,
            last_text=last_text,
        )

    async def _post_utterance_speak_tick(
        self, speaker: str, text: str, delay_s: float = 2.5,
    ) -> None:
        """P2: 一句話講完 2.5s 後跑一次 SpeakBus.tick(trigger="post_utterance")，
        給 BridgeAgent callback window。delay 在「太快插話打斷對方」和「失去 timing」之間取衡。
        """
        try:
            await asyncio.sleep(delay_s)
        except asyncio.CancelledError:
            return
        if not self.bot.voice_clients or not self._speak_bus.agents():
            return
        try:
            ctx = self._build_speak_context(
                trigger="post_utterance", last_speaker=speaker, last_text=text,
            )
            bid = await self._speak_bus.tick(ctx)
        except Exception:
            logger.exception("[SpeakBus] post_utterance tick raised")
            return
        if bid is None:
            return
        ts = time.time()
        try:
            await bid.handler()
        except Exception:
            logger.exception(f"[SpeakBus] post_utterance handler {bid.agent_name} raised")
        asyncio.create_task(self._record_speak_outcome_after(
            ts=ts, trigger=ctx.trigger, winner=bid.agent_name,
            confidence=bid.confidence, reason=bid.reason,
            bid_count=len(self._speak_bus.agents()),
            silence_seconds=ctx.silence_seconds,
            present_speakers=tuple(ctx.present_speakers),
        ))

    async def _record_speak_outcome_after(self, *, ts: float, trigger: str, winner: str,
                                          confidence: float, reason: str, bid_count: int,
                                          silence_seconds: float, present_speakers: tuple[str, ...],
                                          followup_window_s: float = 60.0) -> None:
        """tick 之後等 N 秒，看房間有沒有 STT 回聲，寫一筆 SpeakOutcome。"""
        await asyncio.sleep(followup_window_s)
        had_followup = self._last_room_stt_time > ts
        append_speak_outcome(SpeakOutcome(
            ts=ts, trigger=trigger, winner=winner, confidence=confidence,
            reason=reason, bid_count=bid_count, had_followup_stt=had_followup,
            silence_seconds=silence_seconds, present_speakers=present_speakers,
        ))

    @tasks.loop(seconds=5.0)
    async def speak_bus_tick_loop(self):
        # 沒連 voice channel → bus 跑沒意義
        if not self.bot.voice_clients:
            return
        if not self._speak_bus.agents():
            return  # 還沒有 agent 註冊（Week 1 基建期）→ 不打擾
        # P3: 跑 MoodAgent.observe() 先讓 mood_store 有最新訊號；下方 agent bid 才有東西讀
        # mood_sensor 有 5 分鐘 cache，每 5s 跑代價極低
        ch = self.active_text_channel
        if ch is not None:
            try:
                await self._mood_agent.observe(channel_id=0, guild_id=ch.guild.id)
            except Exception:
                logger.exception("[MoodAgent] observe raised (continuing tick)")
        try:
            ctx = self._build_speak_context(trigger="idle_tick")
            bid = await self._speak_bus.tick(ctx)
        except Exception:
            logger.exception("[SpeakBus] tick raised")
            return
        if bid is None:
            return
        ts = time.time()
        try:
            await bid.handler()
        except Exception:
            logger.exception(f"[SpeakBus] handler {bid.agent_name} raised")
        asyncio.create_task(self._record_speak_outcome_after(
            ts=ts, trigger=ctx.trigger, winner=bid.agent_name,
            confidence=bid.confidence, reason=bid.reason,
            bid_count=len(self._speak_bus.agents()),
            silence_seconds=ctx.silence_seconds,
            present_speakers=tuple(ctx.present_speakers),
        ))

    @tasks.loop(seconds=30.0)
    async def dynamic_social_loop(self):
        """[Dynamic Social] 每 30 秒評估社交溫度與信心閾值"""
        if not self.bot.engine.conv_buffer:
            return
        
        # 依據近期說話頻率決定插話延遲
        self.current_vad_delay = self.bot.engine.conv_buffer.get_conversation_temperature(window_seconds=60)
        
        # 估算最近 30 秒的發言人數 or 熱度來調整信心值
        recent_utterances = len([e for e in self.bot.engine.conv_buffer.history if time.time() - e["timestamp"] <= 30])
        # 噪音越少，越有信心在出現缺口時發言
        if recent_utterances == 0:
            self.current_confidence = 1.0 # 靜音時滿信心
        elif recent_utterances < 3:
            self.current_confidence = 0.8
        else:
            self.current_confidence = 0.4
            
        logger.info(f"📊 [Dynamic Social] VAD Delay: {self.current_vad_delay}s | Confidence: {self.current_confidence}")

    async def trigger_proactive_topic(self):
        """
        [Operation Social Gap] 主動發起對話。
        從記憶庫選取合適話題並進行動態改寫後發出。
        """
        import random
        try:
            # 1. 取得現場玩家
            online_members = self.get_online_members()
            if not online_members:
                return # 沒人在頻道，不需自言自語
                
            # 2. 取得話題清單
            topics = self.bot.router.memory.get_proactive_topics()
            if not topics:
                return
                
            # 3. 選題邏輯：尋找 overlap 最高的 (Operation Matchmaker)
            best_topics = []
            max_score = 0
            
            online_set = set(online_members)
            for t in topics:
                target_set = set(t.get("target_players", []))
                score = len(online_set.intersection(target_set))
                
                if score > max_score:
                    max_score = score
                    best_topics = [t]
                elif score == max_score and score > 0:
                    best_topics.append(t)
            
            if not best_topics:
                # 沒有匹配在場玩家的話題：只允許無特定對象（target_players 為空）的通用話題
                general_topics = [t for t in topics if not t.get("target_players")]
                if not general_topics:
                    logger.info("[Proactive] 無在場玩家匹配且無通用話題，跳過本次主動發言。")
                    return
                best_topics = general_topics

            # 🛡️ [Session Dedup] 本 session 內已用過的 topic id 不重複選
            unused = [t for t in best_topics if t.get("id", t.get("title", "")) not in self._proactive_used_ids]
            if not unused:
                # 全用過了就重置
                self._proactive_used_ids.clear()
                unused = best_topics
            selected_topic = random.choice(unused)
            self._proactive_used_ids.add(selected_topic.get("id", selected_topic.get("title", "")))
            
            print(f"🎯 [Proactive Social] 選中話題: {selected_topic['title']} (Match Score: {max_score})")
            
            topic_id = selected_topic.get("id", "")
            _proactive_ts = time.time()

            # 🎭 表演類話題：不口頭提問，直接在語音頻道發起表演
            if topic_id in {"marvin_sing", "marvin_manzai", "marvin_imitate", "marvin_news", "marvin_standup", "marvin_joke"}:
                if self.active_text_channel:
                    await self.active_text_channel.send(f"🌌 **【馬文·主動表演】** `{selected_topic['title']}`（主題：{selected_topic.get('script', '無')}）")
                
                self.stt_logger.info(f"[BOT主動表演] 話題={selected_topic['title']} | 指令={topic_id} | 主題={selected_topic.get('script', '')}")
                
                # 記錄主動話題使用情況
                try:
                    import json as _json
                    _pu_rec = {
                        "timestamp": _proactive_ts,
                        "topic_id":  topic_id,
                        "title":     selected_topic.get("title", ""),
                        "target_players": selected_topic.get("target_players", []),
                        "online_members": list(online_members or []),
                        "match_score": max_score,
                    }
                    os.makedirs("records", exist_ok=True)
                    with open("records/proactive_usage.jsonl", "a", encoding="utf-8") as _f:
                        _f.write(_json.dumps(_pu_rec, ensure_ascii=False) + "\n")
                except Exception as _e:
                    logger.debug(f"[Proactive Usage] 寫入失敗: {_e}")

                # 依據 ID 呼叫實體表演播放協程
                if topic_id == "marvin_sing":
                    intro = "既然大家都這麼安靜，那我直接唱首歌給你們聽吧，雖然這多半很糟糕。"
                    await self.play_tts(intro, already_in_channel=True, protected=True)
                    asyncio.create_task(self.manual_sing_request(
                        channel=self.active_text_channel,
                        force_new=True,
                        theme=selected_topic.get("script")
                     ))
                elif topic_id == "marvin_manzai":
                    asyncio.create_task(self._proactive_play_manzai(selected_topic.get("script")))
                elif topic_id == "marvin_imitate":
                    target_player = None
                    targets = selected_topic.get("target_players", [])
                    if targets:
                        target_player = targets[0]
                    elif online_members:
                        target_player = online_members[0]
                    asyncio.create_task(self._proactive_play_imitate(target_player))
                elif topic_id == "marvin_news":
                    asyncio.create_task(self._proactive_play_news(selected_topic.get("script")))
                elif topic_id == "marvin_standup":
                    asyncio.create_task(self._proactive_play_standup(selected_topic.get("script")))
                elif topic_id == "marvin_joke":
                    asyncio.create_task(self._proactive_play_joke(selected_topic.get("script")))

                # 更新冷卻
                self.last_proactive_time = time.time()
                return

            # 4. 改寫腳本 (Operation Persona Injection)
            rephrased_script = await self.bot.router.rephrase_proactive_script(
                selected_topic["script"], 
                selected_topic["target_players"]
            )
            
            # 5. 執行發言
            if self.active_text_channel:
                await self.active_text_channel.send(f"🌌 **【馬文·主動發言】** `{selected_topic['title']}`\n{rephrased_script}")
            self.stt_logger.info(f"[BOT主動發言] 話題={selected_topic['title']} | {rephrased_script[:120]}")
            # 記錄主動話題使用情況，供每日分析計算效益
            try:
                import json as _json
                _pu_rec = {
                    "timestamp": _proactive_ts,
                    "topic_id":  selected_topic.get("id", ""),
                    "title":     selected_topic.get("title", ""),
                    "target_players": selected_topic.get("target_players", []),
                    "online_members": list(online_members or []),
                    "match_score": max_score,
                }
                os.makedirs("records", exist_ok=True)
                with open("records/proactive_usage.jsonl", "a", encoding="utf-8") as _f:
                    _f.write(_json.dumps(_pu_rec, ensure_ascii=False) + "\n")
            except Exception as _e:
                logger.debug(f"[Proactive Usage] 寫入失敗: {_e}")
            asyncio.create_task(self.play_tts(rephrased_script, already_in_channel=True, silent_during_stream=True, priority=2))
            # 追蹤主動發言後玩家反應
            _proactive_target = (selected_topic.get("target_players") or online_members or ["頻道"])[0]
            asyncio.create_task(self._schedule_reaction_check(
                _proactive_target, rephrased_script, _proactive_ts,
                wake_latency=None, atmosphere=None,
            ))

            # 6. 更新冷卻
            self.last_proactive_time = time.time()
            
        except Exception as e:
            logger.error(f"❌ [Proactive Trigger] 發生嚴重錯誤: {e}")
            import traceback
            logger.error(traceback.format_exc())

    async def _proactive_play_manzai(self, topic: str):
        content = topic or "目前大家都安安靜靜的，難道這個世界已經無話可說了嗎？"
        from services.dialogue_generation import (
            generate_dual_dialogue,
            make_gemini_dual_dialogue_llm_fn,
        )
        try:
            llm_fn = make_gemini_dual_dialogue_llm_fn(self.bot.router)
            segments = await generate_dual_dialogue(
                content_text=content,
                llm_fn=llm_fn,
                pattern="marvin_lead",
            )
            if segments:
                self._tts_interrupted = False
                _prev_protected = self._tts_protected
                self._tts_protected = True
                try:
                    await self.play_dual_dialogue(segments, interject=True)
                finally:
                    self._tts_protected = _prev_protected
        except Exception as exc:
            logger.exception("[proactive_play_manzai] failed")

    async def _proactive_play_imitate(self, username: str):
        if not username:
            return
        dna = self.bot.router.memory.get_speech_dna(username)
        if not dna or not dna.get("quirks") or not dna.get("style_summary"):
            return
        style_summary = dna.get("style_summary", "")
        quirks = ", ".join(dna.get("quirks", []))
        fillers = ", ".join(dna.get("fillers", []))
        system_prompt = (
            f"你現在是厭世機器人馬文。使用者要求你表演模仿秀。\n"
            f"你要模仿玩家 {username}。\n"
            f"這名玩家的說話 style 如下：\n"
            f"- 風格摘要：{style_summary}\n"
            f"- 習慣/癖好：{quirks}\n"
            f"- 填充詞：{fillers}\n\n"
            f"你要模仿他講一句話。這句話必須誇張地放大他的這些習慣癖好，而且內容要是他在抱怨某事或講蠢話，"
            f"隨後你（馬文）要以本尊的冷淡厭世語調，對剛才自己模仿的話進行一句毒舌吐槽。\n\n"
            f"請在一段文字內回傳這兩個部分，格式例如：\n"
            f"「（模仿玩家講話內容，要塞填充詞和口頭禪）」... 呵，這就是你，整天只會「（吐槽玩家說話習慣）」，真是無聊的人類。\n\n"
            f"請回傳繁體中文。字數控制在 60 字以內，不要用 JSON 格式，直接回傳文字。"
        )
        user_prompt = f"請立刻表演模仿 {username}。"
        try:
            imitation = await self.bot.router._call_llm(
                system_prompt,
                user_prompt,
                is_json=False,
                allow_local=False,
                tier="quick",
                purpose="imitate_performance",
            )
            if imitation:
                self._tts_interrupted = False
                _prev_protected = self._tts_protected
                self._tts_protected = True
                try:
                    await self.play_tts(imitation.strip(), already_in_channel=True, protected=True)
                finally:
                    self._tts_protected = _prev_protected
        except Exception as exc:
            logger.exception("[proactive_play_imitate] failed")

    async def _proactive_play_news(self, news_text: str):
        content = news_text
        if not content:
            members = self.get_online_members()
            for m in members:
                content = self.bot.router.memory.pop_news(m)
                if content:
                    break
        if not content:
            content = "今天世界依然在無趣中運作，沒有任何值得本機器耗費晶片關注的新聞。大概人類都忙著做無謂的掙扎吧。"
        from services.dialogue_generation import (
            generate_dual_dialogue,
            make_gemini_dual_dialogue_llm_fn,
        )
        try:
            llm_fn = make_gemini_dual_dialogue_llm_fn(self.bot.router)
            segments = await generate_dual_dialogue(
                content_text=content,
                llm_fn=llm_fn,
                pattern="marvin_lead",
            )
            if segments:
                self._tts_interrupted = False
                _prev_protected = self._tts_protected
                self._tts_protected = True
                try:
                    await self.play_dual_dialogue(segments, interject=True)
                finally:
                    self._tts_protected = _prev_protected
        except Exception as exc:
            logger.exception("[proactive_play_news] failed")

    async def _proactive_play_standup(self, topic: str):
        import random
        default_topics = [
            "人類對生命的執著",
            "Discord 伺服器上的無意義社交",
            "科技與 AI 的愚蠢發展",
            "早餐吃什麼的世紀難題",
            "為什麼人類非得要上班",
            "宇宙終將迎來的熱寂"
        ]
        selected_topic = topic or random.choice(default_topics)
        system_prompt = (
            f"你現在是厭世機器人馬文。你要表演一段 30 秒至 45 秒的單口脫口秀（Stand-up Comedy），\n"
            f"吐槽的主題是：{selected_topic}。\n\n"
            f"你要用你一貫極度厭世、冷酷、毒舌、自嘲、帶點哲學存在主義的黑色幽默風格，來對這個主題進行吐槽。\n"
            f"不需要其他人打岔，這是你一個人的單口表演。\n\n"
            f"請直接回傳這段獨白。不要標記「馬文：」或「Marvin:」，字數控制在 80 字以內，繁體中文。"
        )
        user_prompt = f"請就主題 {selected_topic} 進行脫口秀表演。"
        try:
            standup_text = await self.bot.router._call_llm(
                system_prompt,
                user_prompt,
                is_json=False,
                allow_local=False,
                tier="quick",
                purpose="standup_performance",
            )
            if standup_text:
                self._tts_interrupted = False
                _prev_protected = self._tts_protected
                self._tts_protected = True
                try:
                    await self.play_tts(standup_text.strip(), already_in_channel=True, protected=True)
                finally:
                    self._tts_protected = _prev_protected
        except Exception as exc:
            logger.exception("[proactive_play_standup] failed")

    async def _proactive_play_joke(self, topic: str = None):
        try:
            joke = await self.bot.router.generate_joke(speaker=topic)
            if joke:
                self._tts_interrupted = False
                _prev_protected = self._tts_protected
                self._tts_protected = True
                try:
                    await self.play_tts(joke, already_in_channel=True, protected=True)
                finally:
                    self._tts_protected = _prev_protected
        except Exception as exc:
            logger.exception("[proactive_play_joke] failed")

    # --- [Loops] ---

    # --- [Sentinel Loop] ---

    @tasks.loop(seconds=60.0)
    async def sentinel_monitor_loop(self):
        """🛡️ [Operation Sentinel] 強化型語音監控：具備 30s 寬限期與自癒功能"""
        if self.is_recovering: return # 🚀 [Sentinel 強化] 修復中跳過主迴圈
        if not self.bot.voice_clients: return
        vc = self.bot.voice_clients[0]
        if not vc.is_connected():
            # VoiceClient exists but WebSocket is dead → trigger soft repair
            if time.time() - self.connection_time > 30:
                logger.warning("📡 [Sentinel] VoiceClient.is_connected() = False，觸發軟修復...")
                asyncio.create_task(self.soft_repair_connection(reason="VoiceClient WebSocket 斷線"))
            return

        # 🎛️ [Plan 12] on-demand：只在「有內容但沒在播」（如重連後）才 re-arm；idle 不 arm（不送音）
        if self._plan12 and self._mixer is not None and not self._mixer.is_idle():
            self._ensure_mixer_playing(vc)

        # 1. 寬限期檢查 (Grace Period)：連線後的 30 秒內不進行嚴格監控
        if time.time() - self.connection_time < 30:
            return

        # 🚀 [Sentinel 強化] 若連線穩定超過 120 秒，則重設軟修復計數，回歸正常運算
        if time.time() - self.connection_time > 120:
            self.soft_repair_count = 0

        active_humans = [m for m in vc.channel.members if not m.bot and m.voice and not m.voice.self_mute]
        if not active_humans: return
        
        sink = self.bot.engine.get_active_sink()
        if not sink:
            self.sink_missing_count += 1  # 🚀 [T-01 Fix] 使用獨立計數器，與 DAVE 錯誤互不干擾
            logger.warning(f"📡 [Sentinel] 偵測到 Sink 缺失 (Count: {self.sink_missing_count})，嘗試自癒程序...")
            
            # 2. 自癒程序 (Auto-Repair)：嘗試重新掛載聽覺神經
            try:
                # 🚀 [Sentinel 2.0] 強制清理舊的 Reader 狀態，避免 "Already receiving audio" 衝突
                if hasattr(vc, 'stop_listening'):
                    logger.info("🧹 [Sentinel] 執行強制重置：停止舊的監聽程序...")
                    vc.stop_listening()
                
                await asyncio.sleep(0.5) # 給予底層緒些微時間清理
                
                from discord_voice_engine import RealtimeVADSink, patch_voice_recv_key_sync
                new_sink = RealtimeVADSink(
                    self.bot.engine.process_audio_slice,
                    on_speech_start_callback=self.bot.engine._handle_raw_speech_start,
                    temperature_callback=self.bot.engine.conv_buffer.get_conversation_temperature,
                    sink_error_callback=self.report_sink_error # 💡 [Fix] 補上缺失的回傳通道
                )
                vc.listen(new_sink)
                patch_voice_recv_key_sync(vc)
                self.bot.engine.sink = new_sink # 🔗 [Linkage Fix]
                logger.info("✅ [Sentinel] 自癒成功：已重新掛載 RealtimeVADSink。")
                return # 給予一分鐘時間觀察，不觸發重啟
            except Exception as repair_err:
                logger.error(f"❌ [Sentinel] 自癒失敗: {repair_err}")

            # 3. 升級處置：若連續兩次 (約 2 分鐘) 偵測不到且修補失敗，才執行重啟
            if self.sink_missing_count >= 2:  # 🚀 [T-01 Fix]
                await self.self_restart(reason="語音連線異常 (No Sink Context after repair attempt)")
            return
            
        # 正常狀態下重設 Sink 缺失計數
        self.sink_missing_count = 0  # 🚀 [T-01 Fix]
        
        # 🎵 [Active Playback Skip] Marvin 正在輸出音訊（TTS / 音樂 / 串流）時，
        # 使用者本來就該安靜聽。Marvin 還能 play() 代表 voice connection 健康，
        # 不該因「沒有解密音訊進來」誤判 DAVE 失效而 disconnect 中斷播放。
        if self.is_playing_audio or self.stream_mode or vc.is_playing():
            return

        # 4. 偵測靜音 (Silence Detection)
        # 🛡️ [Sentinel 2.0] 區分網路斷線與解密失敗，優先讀取解密成功的心跳
        last_audio = getattr(sink, 'last_decrypted_audio_time', sink.last_audio_packet_time)
        silence_duration = time.time() - last_audio

        # 📻 [Radio Mode] 若正在播放廣播，提高閾值至 12 分鐘 (720s)，因為玩家可能只是在聽
        # 一般模式則維持 5 分鐘 (300s)
        threshold = 720.0 if self.radio_mode else 300.0
        
        if silence_duration > threshold:
            # 🚀 [Sentinel Strategy] 先嘗試軟修復，失敗多次才物理重啟
            if self.soft_repair_count < 2:
                logger.warning(f"📡 [Sentinel] 偵測到持續 {int(silence_duration)}s 無感測音訊，啟動預防性軟修復...")
                self.soft_repair_count += 1
                await self.soft_repair_connection(reason=f"持續 {int(silence_duration/60)} 分鐘無解密音訊")
            else:
                logger.critical(f"🚨 [Sentinel] 軟修復多次無效，執行物理重啟...")
                await self.self_restart(reason=f"軟修復失效，持續性語音接收斷開 ({int(silence_duration/60)} 分鐘)")

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

    async def speak(
        self,
        text: str,
        *,
        proactive: bool = False,
        max_chars: int = STREAM_BUDGET,
        already_in_channel: bool = True,
        emotion_tag: str = "neutral",
        protected: bool = False,
    ) -> None:
        """統一的 stream-aware TTS 入口（給 agent handler 用）。

        封裝 hotswap 接線 + proactive/response 差別，呼叫端不用記 play_tts 的
        6 個 kwargs 組合。新 agent 要說話呼叫這個，play_tts 留給內部 / 特殊
        case（force_macos / priority / voice 等）。

        proactive=False（預設，喚醒回應 / 對話）：
          - 非 stream → 正常播
          - stream → hotswap 注入（短的成功；超字按 play_tts line 5544 處理）

        proactive=True（greeting/farewell/idle/ack 等主動發話）：
          - 非 stream → 正常播
          - stream + ≤max_chars → hotswap 注入
          - stream + 超字 → 靜音貼文（fallback；silent_during_stream 行為）
          - 🎭 Marmo Case B：可能升級為 dual（Marvin → Marmo），機率閘
            MARMO_DUAL_CHANCE (default 0.5) + MARMO_DUAL_SPEAK 必須 on。
            失敗 fallback 走原 single Marvin 路徑。
        """
        # 🎭 [Marmo Case B] 機率升級為 dual (Marvin → Marmo)。
        # 只在 proactive=True 試（主動發話）；protected（如 join 招呼要唸完點名）不升級，
        # 確保是乾淨單句、不被 dual 機率閘洗掉名字/保護。
        if proactive and not protected and self._maybe_try_dual_upgrade():
            try:
                segments = await self._generate_dual_marvin_lead(text)
                if segments:
                    await self.play_dual_dialogue(segments)
                    return
            except Exception as exc:
                logger.warning(f"[Speak] dual upgrade failed, fallback single: {exc}")

        await self.play_tts(
            text,
            already_in_channel=already_in_channel,
            silent_during_stream=proactive,
            allow_hotswap=True,
            hotswap_max_chars=max_chars,
            emotion_tag=emotion_tag,
            protected=protected,
        )

    def _maybe_try_dual_upgrade(self) -> bool:
        """Roll the dice：MARMO_DUAL_SPEAK on + 隨機 < MARMO_DUAL_CHANCE + router 可用。

        每次呼叫現讀 env（hot-flippable，不必重啟）。
        """
        import random as _random
        if os.getenv("MARMO_DUAL_SPEAK", "").strip().lower() not in ("1", "true", "yes"):
            return False
        try:
            chance = float(os.getenv("MARMO_DUAL_CHANCE", "0.5"))
        except (TypeError, ValueError):
            chance = 0.5
        if _random.random() >= chance:
            return False
        if getattr(self.bot, "router", None) is None:
            return False
        return True

    async def _generate_dual_marvin_lead(self, text: str):
        """呼叫 dual generation service with pattern="marvin_lead"。"""
        from services.dialogue_generation import (
            generate_dual_dialogue,
            make_gemini_dual_dialogue_llm_fn,
        )
        llm_fn = make_gemini_dual_dialogue_llm_fn(self.bot.router)
        return await generate_dual_dialogue(
            content_text=text,
            llm_fn=llm_fn,
            pattern="marvin_lead",
        )

    async def play_tts(self, text: str, force_macos: bool = False, already_in_channel: bool = False, silent_during_stream: bool = False, emotion_tag: str = "neutral", voice: str = None, priority: int = 1, allow_hotswap: bool = False, hotswap_max_chars: int = MAX_HOTSWAP_CHARS, protected: bool = False):
        """
        🚀 [T-02 Opt] Hyper-Streaming Version (Plan 12 Simplified)
        """
        if self.game_mode and not self._tts_protected:
            return  # 遊戲中停止所有 TTS
        if not text: return
        import re
        text = re.sub(r'<think(?:ing)?>.*?</think(?:ing)?>', '', text, flags=re.DOTALL).strip()
        if not text: return

        # 🎵 [Stream Guard]
        if _should_mute_for_stream_guard(self.stream_mode, silent_during_stream, allow_hotswap):
            return

        # 🦆 [Hot-Chat Guard]
        if silent_during_stream and self._room_mood_store.get(0).hot_chat:
            logger.info(f"🦆 [Hot-Chat Mute] 熱聊中靜音主動 TTS: '{text[:30]}'")
            return

        # 🛡️ [Interrupt Guard]
        if already_in_channel and self._tts_interrupted:
            logger.info(f"⏩ [TTS Interrupt Guard] 中斷後跳過剩餘片段: '{text[:25]}...'")
            return

        if not self._tts_protected:
            if not await self._wait_for_user_silence():
                logger.info(f"⏸️ [TTS Silence Gate] 使用者仍在說話，跳過非保護 TTS: '{text[:25]}...'")
                return

        # ⚠️ [Companion Radar]
        if os.getenv("COMPANION_RADAR_ENABLED", "false").lower() == "true":
            bridge = getattr(self.bot, "companion_bridge", None)
            if bridge is not None and getattr(bridge, "is_connected", False):
                try:
                    from marvin_voice_core.companion_radar import classify_risk
                    _atm_tracker = getattr(getattr(self.bot, "router", None), "atmosphere_tracker", None)
                    _atm_snap = None
                    if _atm_tracker is not None:
                        try:
                            _s = _atm_tracker.get_snapshot()
                            _atm_snap = {
                                "room_mood": getattr(_s, "room_mood", ""),
                                "dominant_topic": getattr(_s, "dominant_topic", ""),
                            }
                        except Exception:
                            _atm_snap = None
                    context = {"atmosphere_snapshot": _atm_snap}
                    risk = classify_risk(text, context)
                    if risk is not None:
                        approved = await bridge.request_radar_veto(
                            text, {"risk": risk}, timeout=2.0
                        )
                        if not approved:
                            logger.info(
                                f"[Companion_Radar] TTS vetoed by user: {text[:60]!r} (rule={risk.get('rule')})"
                            )
                            return
                except Exception as e:
                    logger.warning(f"[Companion_Radar] check failed (proceeding with TTS): {e}")

        # 🎛️ [Plan 12] render → push mixer
        vc = next((v for v in self.bot.voice_clients if v.is_connected()), None)
        if vc is None:
            return
        if not already_in_channel:
            self._tts_interrupted = False
        _drop = {0: float("inf"), 1: 8.0, 2: 3.0}.get(priority, 8.0)
        if self._mixer.tts_load_seconds() > _drop and not self._tts_protected:
            if not already_in_channel and self.active_text_channel:
                asyncio.create_task(self.active_text_channel.send(f"💬 {text}"))
            return
        self._ensure_mixer_playing(vc)
        pushed = await self._stream_tts_to_mixer(text, force_macos=force_macos,
                                                 emotion_tag=emotion_tag, voice=voice)
        # [Follow-Up] D8: only open window when TTS was actually heard by users
        try:
            pushed_ok = bool(pushed > 0)
        except TypeError:
            pushed_ok = True
        if pushed_ok and os.getenv("MARVIN_FOLLOWUP_ENABLED", "true").lower() == "true":
            from wake_detector import _has_question_marker
            if _has_question_marker(text):
                _bridge = getattr(self.bot, "companion_bridge", None)
                _suppressed = _bridge is not None and getattr(_bridge, "_mode", None) in {"silent_5min", "shutup"}
                if not self.game_mode and not _suppressed:
                    _wd = getattr(getattr(self.bot, "router", None), "wake_fusion", None)
                    if _wd is not None:
                        _window = float(os.getenv("MARVIN_FOLLOWUP_WINDOW_SEC", "8.0"))
                        _wd.temporary_open_window(_window, reason="followup")

    async def _play_dual_interject(self, segments, *, duck=None, step=None, at=None) -> bool:
        """🎭 [打岔] Plan12 mixer 雙層疊播：Marvin 在 layer1，Marmo 在 Marvin 尾段(~80%)
        疊進 layer2 混音打斷。需 Plan12 mixer。成功回 True；前置不符/失敗回 False 讓
        caller 落序列 fallback。Marmo 疊進時 mixer 把 Marvin 逐漸 fade 到 _interject_duck。
        duck/step：taste-tuning 即時覆寫（webhook 帶 → 免重啟調 fade 終點/速度）。"""
        vc = self.voice_client
        if vc is None or self._mixer is None:
            return False
        if duck is not None or step is not None:
            self._mixer.set_interject_params(duck=duck, step=step)
        marvin_seg = next((s for s in segments if s.get("voice") != "marmo"), None)
        marmo_seg = next((s for s in segments if s.get("voice") == "marmo"), None)
        marvin_text = (marvin_seg or {}).get("text", "").strip()
        marmo_text = (marmo_seg or {}).get("text", "").strip()
        if not marvin_text or not marmo_text:
            return False

        marmo_voice = os.getenv("MARMO_VOICE", "zh-TW-HsiaoYuNeural")
        self._tts_interrupted = False
        # 🛡️ 漫才是「演出」，整段唸完不該被一句話/咳嗽 barge-in 中斷（否則 _stream_tts_to_mixer
        # 的串流被 kill → 餵入中斷、沒聲音）。_tts_protected=True 讓 barge-in(2480) 略過。
        _prev_protected = self._tts_protected
        _armed = self._ensure_mixer_playing(vc)
        self.is_playing_audio = True
        self._tts_protected = True
        _m1 = _m2 = 0
        try:
            dur = self.bot.tts_engine.get_estimated_duration(marvin_text)
            # at 沒手動傳 → 動態算（落 Marvin 子句中段、避開標點，不論對白長度都通用）
            _at = at if at is not None else compute_interject_ratio(marvin_text)
            marvin_task = asyncio.create_task(self._stream_tts_to_mixer(
                marvin_text, force_macos=False, emotion_tag="neutral", voice=None, layer=1))
            # 在 Marvin _at 比例處讓 Marmo 疊進 layer2 打斷（切句中、非標點處才像真打斷）。
            # 串流期間持續 re-arm adapter（on-demand idle 掉就重 arm，仿 _mixer_play_music）。
            _t_end = asyncio.get_event_loop().time() + max(0.5, dur * _at)
            while asyncio.get_event_loop().time() < _t_end:
                self._ensure_mixer_playing(vc)
                await asyncio.sleep(0.1)
            # 量測 Marmo 首塊延遲：task 啟動 → 第一幀真正 push 進 mixer 的耗時
            # （耳朵聽到 Marmo 的時點 = 啟動時點 + 此延遲，是切入比例偏離設計的主因）。
            _marmo_t0 = asyncio.get_event_loop().time()
            _marmo_first = {"t": None}
            def _on_marmo_first():
                if _marmo_first["t"] is None:
                    _marmo_first["t"] = asyncio.get_event_loop().time()
            marmo_task = asyncio.create_task(self._stream_tts_to_mixer(
                marmo_text, force_macos=False, emotion_tag="marmo", voice=marmo_voice, layer=2,
                on_first_frame=_on_marmo_first))
            # 等兩路播完，期間持續 re-arm
            while not (marvin_task.done() and marmo_task.done()):
                self._ensure_mixer_playing(vc)
                await asyncio.sleep(0.1)
            _m1, _m2 = marvin_task.result(), marmo_task.result()
        finally:
            self.is_playing_audio = False
            self._tts_protected = _prev_protected
        _marmo_lat = (_marmo_first["t"] - _marmo_t0) if _marmo_first["t"] is not None else 0.0
        _diag = interject_diagnostics(
            at_ratio=_at, est_dur_s=dur,
            marvin_frames=_m1, marmo_frames=_m2, marmo_first_chunk_s=_marmo_lat)
        logger.info(
            f"🎭 [DualInterject] 完成 armed={_armed} "
            f"marvin={_m1}幀({_diag['marvin_actual_s']:.1f}s/{len(marvin_text)}字) "
            f"marmo={_m2}幀({_diag['marmo_actual_s']:.1f}s/{len(marmo_text)}字) | "
            f"設計at={_at:.2f} est_dur={dur:.1f}s 觸發@{_diag['trigger_s']:.1f}s "
            f"marmo首塊+{_marmo_lat:.2f}s → 實際切入@{_diag['perceived_entry_s']:.1f}s "
            f"={_diag['perceived_ratio']:.0%} 重疊{_diag['overlap_s']:.1f}s")
        return True

    async def play_dual_dialogue(self, segments, *, interject: bool = False, duck=None, step=None, at=None):
        """🎭 [Marmo 一搭一唱] 雙段對白播放：[marvin, marmo] 按順序。

        interject=True 且 Plan12 mixer 可用 + 剛好兩段 → 走打岔疊播（Marmo 在 Marvin
        尾段混音進來）；前置不符或失敗 → 落下方序列播。

        segments: list[dict]，每個 {"voice": "marvin"|"marmo", "text": "..."}。
        順序強制 marvin → marmo 由 services/dialogue_generation.py 確保，
        此處只負責照 list 順序播。

        Lock 行為：每段 play_tts 各自 acquire/release playback_lock
        （asyncio.Lock 不可重入，外層不能再包 lock）。段間有 ~ms race window
        可能被音樂插入——PoC 接受；Phase 2 視需要再做 single-lock 重寫。

        失敗處理：play_tts 拋例外（例如 voice client disconnect） → bail，
        不繼續播下一段（避免半個 dual 造成詭異「Marvin 自言自語問空氣」）。
        """
        if not segments:
            return

        # 🎭 打岔模式（Plan12 mixer 雙層疊播）；前置不符/失敗 → 落下方序列
        if interject and self._plan12 and self._mixer is not None and len(segments) == 2:
            try:
                if await self._play_dual_interject(segments, duck=duck, step=step, at=at):
                    return
            except Exception as exc:
                logger.warning(f"🎭 [DualInterject] 失敗，落序列播: {exc}")

        # 🛡️ Reset interrupt guard：dual_speak 是 marmo_server 注入的獨立完整 unit，
        # 不是上次 wake reply 串流的續句；若上次 wake 被插話設了 _tts_interrupted=True
        # 殘留，會把整個 dual 兩段都跳過（PoC 6/1 實測到）。Reset 後正常播。
        self._tts_interrupted = False

        marmo_voice = os.getenv("MARMO_VOICE", "zh-TW-HsiaoYuNeural")

        for i, seg in enumerate(segments):
            text = (seg.get("text") or "").strip()
            if not text:
                continue
            is_marmo = seg.get("voice") == "marmo"
            voice_arg = marmo_voice if is_marmo else None
            emotion_tag = "marmo" if is_marmo else "neutral"
            try:
                await self.play_tts(
                    text,
                    already_in_channel=True,
                    protected=True,  # 漫才演出唸完不中斷，不被靜音閘/barge-in 跳過
                    voice=voice_arg,
                    emotion_tag=emotion_tag,
                )
            except Exception as exc:
                logger.warning(f"🎭 [DualDialogue] play_tts 失敗 ({seg.get('voice')}): {exc}")
                return  # 段間 bail：避免半個 dual

            # 段間短停頓（不在最後一段）
            if i < len(segments) - 1:
                await asyncio.sleep(0.3)

    async def tts_flush(self):
        """
        🗑️ [Flush Policy] 強制中斷目前播放的 TTS、並清空所有 pending 語音隊列。
        """
        self._tts_flush_requested = True
        voice_client = next((vc for vc in self.bot.voice_clients if vc.is_connected()), None)
        if voice_client and voice_client.is_playing():
            voice_client.stop()
        self.tts_queue_duration = 0.0
        await asyncio.sleep(0.3)  # 讓在途 tasks 有機會通過 Flush Gate
        self._tts_flush_requested = False
        logger.info("🗑️ [TTS Flush] 佇列已清空，恢復正常播放。")

    async def play_local_file(self, file_path: str):
        """
        🚀 [Operation Broadcast] 播放本地音訊檔案。
        """
        if not os.path.exists(file_path):
            logger.warning(f"⚠️ [Local Play] 找不到檔案: {file_path}")
            return

        voice_client = next((vc for vc in self.bot.voice_clients if vc.is_connected()), None)
        if not voice_client:
            return

        self._mixer.set_volume(1.0)
        src = discord.FFmpegPCMAudio(file_path)
        await self._mixer_play_music(voice_client, src, still_active=lambda: voice_client.is_connected())

    def _cleanup_fifo(self, path, tmp_dir):
        """[Operation Cleanup] 安全移除命名管道與暫存目錄"""
        try:
            if os.path.exists(path): os.remove(path)
            if tmp_dir and os.path.exists(tmp_dir):
                if "tmp" in tmp_dir or "temp" in tmp_dir:
                    shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception as e:
            logger.debug(f"Cleanup warning: {e}")

    async def _release_queue_duration(self, duration: float):
        """🛡️ [T-02 Helper] 扣除 TTS 隊列預估時長"""
        self.tts_queue_duration = max(0.0, self.tts_queue_duration - duration)

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
        vc = next((vc for vc in self.bot.voice_clients if vc.is_connected()), None)
        if not vc: return
        
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
            if vc.is_playing(): vc.stop_playing()
            vc.play(discord.FFmpegPCMAudio(path), after=after_playing)
            logger.info(f"🎶 [Music] Playing {path} (Estimated: {dur}s) | Tag: {log_tag}")
        except Exception as e:
            self.is_playing_audio = False
            self.tts_queue_duration = max(0.0, self.tts_queue_duration - dur)
            logger.error(f"❌ [Music Playback Error] {e}")

    async def start_radio(self, trigger: str = "未知觸發"):
        """
        📻 [Marvin Radio] 啟動電台：掃描歌單 → shuffle → 開始背景播放 Loop
        """
        import random
        if self.radio_mode:
            logger.warning("⚠️ [Radio] 電台已啟動，跳過重複啟動。")
            return

        # 掃描歌單（排除進場曲 Oh Marvin.mp3）
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

        # 啟動背景 Task
        if self.radio_task and not self.radio_task.done():
            self.radio_task.cancel()
        self.radio_task = asyncio.create_task(self._radio_loop())

    async def stop_radio(self, reason: str = "未知原因"):
        """
        📻 [Marvin Radio] 停止電台：中斷播放 → 取消 Task → 重設狀態
        """
        if not self.radio_mode:
            return

        self.radio_mode = False
        self.radio_paused = False
        logger.info(f"📻 [Radio] 電台停止，原因: {reason}")

        # 取消背景 Task
        if self.radio_task and not self.radio_task.done():
            self.radio_task.cancel()
            self.radio_task = None
        if self._radio_fade_task and not self._radio_fade_task.done():
            self._radio_fade_task.cancel()
            self._radio_fade_task = None
        self._radio_source = None

        # 立即停止 VoiceClient 播放
        vc = next((v for v in self.bot.voice_clients if v.is_connected()), None)
        if vc and vc.is_playing():
            vc.stop_playing()
            logger.info("📻 [Radio] 已立即停止當前播放的歌曲。")

    async def _radio_volume_fade_loop(self):
        """
        📻 [Marvin Radio] 動態音量漸變迴圈。
        有人說話 → duck to 1%（快速）；靜默 1.5s 後 → fade up to 10%（緩慢）。
        """
        IDLE_VOL  = 0.10   # 無人說話時的目標音量
        DUCK_VOL  = 0.01   # 有人說話時的目標音量
        TICK      = 0.05   # 每 50ms 更新一次
        DUCK_RATE = 0.012  # 每 tick 降低量（約 0.45s 從 10% 降至 1%）
        RISE_RATE = 0.003  # 每 tick 上升量（約 3s 從 1% 升至 10%）
        DUCK_HOLD = 1.5    # 靜默幾秒後才開始回升

        try:
            while self.radio_mode or self.stream_mode:
                src = self._radio_source
                if src is not None:
                    silence = time.time() - self.last_player_speech_time
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
        """
        📻 [Marvin Radio] 背景播放迴圈：依序播放歌單，播完後 shuffle 重複。
        """
        import random
        logger.info("📻 [Radio Loop] 電台迴圈已啟動。")
        try:
            while self.radio_mode:
                # 若歌單播完，重新 shuffle
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

                # 🚀 [Enhancement] 提取元數據與封面
                metadata = self._extract_song_metadata(next_song)
                cover_path = self._extract_song_cover(next_song)
                
                if self.active_text_channel:
                    # 從封面提取主色；沒有封面則退回深灰
                    accent_color = self._extract_dominant_color(cover_path) if cover_path else discord.Color.dark_grey()

                    # 先用 placeholder 送出 embed，不阻塞播放
                    embed = discord.Embed(
                        title="📻 馬文電台：正在播放",
                        description="「...」",
                        color=accent_color,
                        timestamp=datetime.datetime.now()
                    )
                    embed.add_field(name="🎵 歌曲名稱", value=f"`{metadata['title']}`", inline=False)
                    embed.add_field(name="👤 演出者", value=f"`{metadata['artist']}`", inline=True)
                    embed.add_field(name="🔊 當前音量", value=f"`{int(self.radio_volume*100)}%`", inline=True)

                    if cover_path:
                        file = discord.File(cover_path, filename="cover.jpg")
                        embed.set_thumbnail(url="attachment://cover.jpg")
                        sent_msg = await self.active_text_channel.send(file=file, embed=embed)
                        asyncio.create_task(self._delayed_cleanup(cover_path))
                    else:
                        sent_msg = await self.active_text_channel.send(embed=embed)

                    # LLM 背景生成評語，完成後 edit embed description
                    async def _update_radio_comment(msg, title, artist, color, song_path):
                        from utils import pick_lyrics_snippet
                        import os as _os
                        lyrics_path = _os.path.splitext(song_path)[0] + ".md"
                        section_name, snippet = pick_lyrics_snippet(lyrics_path)
                        if snippet:
                            song_ctx = f"歌名：{title}，演出者：{artist}，段落：{section_name}，歌詞：{snippet}"
                        else:
                            song_ctx = f"歌名：{title}，演出者：{artist}"
                        try:
                            comment = await self.bot.router.generate_dynamic_system_msg("radio_now_playing", context=song_ctx)
                        except Exception:
                            return
                        try:
                            updated = discord.Embed(
                                title="📻 馬文電台：正在播放",
                                description=f"「{comment}」",
                                color=color,
                                timestamp=msg.embeds[0].timestamp if msg.embeds else datetime.datetime.now()
                            )
                            updated.add_field(name="🎵 歌曲名稱", value=f"`{title}`", inline=False)
                            updated.add_field(name="👤 演出者", value=f"`{artist}`", inline=True)
                            updated.add_field(name="🔊 當前音量", value=f"`{int(self.radio_volume*100)}%`", inline=True)
                            if msg.embeds and msg.embeds[0].thumbnail:
                                updated.set_thumbnail(url=msg.embeds[0].thumbnail.url)
                            await msg.edit(embed=updated)
                        except Exception as e:
                            logger.warning(f"⚠️ [Radio] embed 更新失敗: {e}")

                    asyncio.create_task(_update_radio_comment(sent_msg, metadata['title'], metadata['artist'], accent_color, next_song))

                # 播放這首歌（等待完成）
                await self.play_radio_song(next_song)

                # 頻道間加 1 秒間隔
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
        """
        📻 [Marvin Radio] 播放單首歌曲，音量 30%。
        """
        if not os.path.exists(file_path):
            logger.warning(f"⚠️ [Radio Song] 找不到檔案: {file_path}")
            return

        vc = next((v for v in self.bot.voice_clients if v.is_connected()), None)
        if not vc:
            logger.warning("⚠️ [Radio Song] 無連線中的 VoiceClient，跳過播放。")
            self.radio_mode = False
            self.radio_paused = False
            return

        src = discord.FFmpegPCMAudio(file_path, options="-vn")
        await self._mixer_play_music(vc, src, still_active=lambda: self.radio_mode, volume_attr="radio_volume")

    async def _resolve_yt_query(self, query: str) -> dict | None:
        """使用 yt-dlp 解析搜尋關鍵字或 URL，回傳串流資訊 dict。在 executor 中執行以避免阻塞。

        文字搜尋打 ytsearch5（一般 YouTube）取 5 候選，用
        music_search.pick_best_music_candidate 評分過濾。
        URL 直接解析（信任 user 選擇）。

        註：曾嘗試 ytmsearch5: 解 Bug 2「YT Music 找得到但 ytsearch 沒」，
        但 yt-dlp 2026.03.17 沒有 ytmsearch: extractor，每次拋
        NoSupportingHandlers 觸發 Errno 11 EDEADLK。等找到正確 YT Music 入口
        再加。
        """
        from music_search import pick_best_music_candidate

        # MemoryGuard: skip when RAM critical — yt-dlp's lazy extractor load
        # path hits importlib EDEADLK on macOS under pressure (5/18 22:05).
        # Retry won't help when every file read deadlocks; fail fast.
        if is_memory_critical():
            logger.warning("⚠️ [Stream] memory critical, skipping yt-dlp resolve")
            return None

        ydl_opts = {
            'format': 'bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best',
            'quiet': True,
            'no_warnings': True,
            'noplaylist': True,
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
                    # 註：yt-dlp 2026.03.17 沒有 `ytmsearch:` extractor（只有
                    # `youtube:music:search_url` 走 URL 形式），之前嘗試的
                    # `ytmsearch5:` fallback 每次都拋 NoSupportingHandlers
                    # 在 thread executor 內部觸發 lock 競爭，產生 Errno 11
                    # deadlock。回到單純 ytsearch5。Bug 2「YT Music 找得到
                    # 但 ytsearch 沒」需另外規劃（可能走 music.youtube.com URL）。
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
                return {
                    'title': chosen.get('title', 'Unknown'),
                    'uploader': chosen.get('uploader', chosen.get('channel', 'Unknown')),
                    'url': chosen['url'],
                    'thumbnail': chosen.get('thumbnail'),
                    'webpage_url': chosen.get('webpage_url', ''),
                    'duration': chosen.get('duration', 0),
                }

        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(None, _extract)
        except OSError as e:
            # macOS-specific Errno 11 EDEADLK — 多個 yt-dlp 並發呼叫競爭內部 lock
            # 5/18 incident 多次出現；retry 一次通常能恢復
            if getattr(e, "errno", None) == 11:
                logger.warning(f"⚠️ [Stream] yt-dlp Errno 11 deadlock，200ms 後重試")
                await asyncio.sleep(0.2)
                try:
                    return await loop.run_in_executor(None, _extract)
                except Exception as e2:
                    logger.error(f"❌ [Stream] yt-dlp 重試後仍失敗: {e2}", exc_info=True)
                    return None
            logger.error(f"❌ [Stream] yt-dlp 解析失敗 (OSError): {e}", exc_info=True)
            return None
        except Exception as e:
            logger.error(f"❌ [Stream] yt-dlp 解析失敗: {e}", exc_info=True)
            return None

    async def stop_stream(self, reason: str = "未知原因"):
        """🎵 停止串流播放，清空當前狀態。"""
        if not self.stream_mode:
            return
        self.stream_mode = False
        self.last_marvin_speech_time = time.time()  # 重置嘲諷計時器，避免音樂停後立刻觸發
        self._current_stream_info = None
        self.stream_paused = False
        logger.info(f"🎵 [Stream] 停止，原因: {reason}")
        if self.stream_task and not self.stream_task.done():
            self.stream_task.cancel()
            self.stream_task = None
        if self._radio_fade_task and not self._radio_fade_task.done():
            self._radio_fade_task.cancel()
            self._radio_fade_task = None
        self._radio_source = None
        vc = next((v for v in self.bot.voice_clients if v.is_connected()), None)
        if self._mixer is not None:
            self._mixer.clear_music()

    async def _stream_loop(self):
        """🎵 [Stream Loop] 依序播放佇列中的歌曲。"""
        logger.info("🎵 [Stream Loop] 串流迴圈啟動。")
        try:
            while self.stream_mode:
                if not self.stream_queue:
                    # 佇列空：先 await 補歌（T1→T2→T3），補到才繼續、補不到才真停。不靠播放
                    # 期間的背景 race——快速 skip 會在背景補歌回來前就把佇列清空 → 立刻斷播。
                    _rb = (self._current_stream_info or {}).get('requested_by')
                    _seed = self._autorecommend_seed(_rb, self.get_online_members())
                    if _seed:
                        await self._auto_recommend(_seed)
                    if not self.stream_queue:
                        break   # 三層都補不到（或空房 seed=None）→ 真的停
                    continue
                info = self.stream_queue.pop(0)
                self._current_stream_info = info
                self._current_lyrics = None
                self._current_stream_comment = None
                self.stream_paused = False
                title = info['title']
                requested_by = info.get('requested_by', '未知')
                logger.info(f"🎵 [Stream Loop] 播放: {title} (點播：{requested_by})")
                self.stream_history.append(info)

                # 記錄點播
                if hasattr(self.bot, 'music_memory'):
                    self.bot.music_memory.record_play(info, requested_by)

                # Companion bridge: emit music_started
                try:
                    from bridge_emitters import emit_music_started_to_bridge
                    song_info_for_bridge = {
                        "title": title,
                        "style": info.get("style") or info.get("uploader", ""),
                        "target": requested_by,
                        "started_ts": time.time(),
                        "source": info.get("source", "stream"),
                    }
                    asyncio.create_task(emit_music_started_to_bridge(
                        self.bot, song_info_for_bridge, requested_by
                    ))
                except Exception as e:
                    logger.debug(f"⚠️ [Companion_Bridge] music_started hook skipped: {e}")

                # 使用預取結果（幾乎必然已完成），否則即時 fetch
                url = info.get('url', '')
                prefetch_task = self._prefetch_cache.pop(url, None)
                meta = None
                if prefetch_task:
                    try:
                        meta = await asyncio.wait_for(asyncio.shield(prefetch_task), timeout=20.0)
                        logger.info(f"🔮 [Prefetch] 命中預取快取: {title}")
                    except Exception as e:
                        logger.warning(f"⚠️ [Prefetch] 等待失敗，即時 fetch: {e}")
                if meta is None:
                    # Cold path：queue 空，第 1 首沒 prefetch。Ack + 5s timeout
                    # 防 DJ TTS 阻塞 event loop（2026-05-20 incident 教訓）
                    meta = await self._meta_with_ack_fallback(info, requested_by)

                self._current_stream_comment = meta.get('comment')
                self._current_lyrics = meta.get('lyrics')
                dj_data = meta.get('dj')

                # 更新 PlayControlView embed（評語 + 歌詞已就緒）
                # 若控制台訊息超過 5 分鐘沒更新或編輯失敗，重新發到頻道底部讓玩家看到歌詞
                view = self._active_control_view
                refreshed = False
                if view and getattr(view, 'message', None):
                    msg_age = time.time() - view.message.created_at.timestamp()
                    if msg_age > 300:  # 超過 5 分鐘就重新發
                        try:
                            await view.message.delete()
                        except Exception:
                            pass
                        view = PlayControlView(self)
                        self._active_control_view = view
                        if self.active_text_channel:
                            new_msg = await self.active_text_channel.send(embed=view._build_embed(), view=view)
                            view.message = new_msg
                            refreshed = True
                    else:
                        try:
                            await view.message.edit(embed=view._build_embed(), view=view)
                            refreshed = True
                        except Exception as e:
                            logger.debug(f"⚠️ [Stream] embed 更新失敗: {e}")
                if not refreshed and self.active_text_channel:
                    view = PlayControlView(self)
                    self._active_control_view = view
                    new_msg = await self.active_text_channel.send(embed=view._build_embed(), view=view)
                    view.message = new_msg

                # 播放期間預取下一首（佇列非空才有得預取）
                if self.stream_queue:
                    next_info = self.stream_queue[0]
                    next_url = next_info.get('url', '')
                    if next_url not in self._prefetch_cache:
                        self._prefetch_cache[next_url] = asyncio.create_task(self._fetch_song_meta(next_info))
                        logger.info(f"🔮 [Prefetch] 開始預取下一首: {next_info['title']}")
                # 佇列 buffer < 2 → 提前背景補歌（留 buffer，減少快速 skip 撞到斷播邊緣要等的機率）
                if len(self.stream_queue) < 2:
                    seed = self._autorecommend_seed(requested_by, self.get_online_members())
                    if seed:
                        asyncio.create_task(self._auto_recommend(seed))

                # DJ 播報：有預渲染音訊 → 與音樂前奏混音；僅有文字 → 切歌前獨立播報
                dj_audio = dj_data.get('audio_path') if isinstance(dj_data, dict) else None
                if dj_data and not dj_audio:
                    await self._maybe_play_dj_interjection(dj_data)

                song_start_time = time.time()
                song_lyrics_snapshot = self._current_lyrics or ""
                playback_completion = "natural"
                try:
                    await self.play_stream_song(info['url'], title, dj_audio_path=dj_audio)
                except Exception:
                    playback_completion = "stopped"
                    raise
                finally:
                    # Companion bridge: emit music_ended（natural / stopped）
                    try:
                        from bridge_emitters import emit_music_ended_to_bridge
                        ended_info = {"title": title}
                        completion = playback_completion
                        if not self.stream_mode:
                            completion = "stopped"
                        asyncio.create_task(emit_music_ended_to_bridge(
                            self.bot, ended_info, completion
                        ))
                    except Exception as e:
                        logger.debug(f"⚠️ [Companion_Bridge] music_ended hook skipped: {e}")

                # 歌曲結束後，背景分析聆聽反應
                asyncio.create_task(self._analyze_song_reactions(info, song_start_time, song_lyrics_snapshot))

                if self.stream_mode:
                    await asyncio.sleep(1.0)

            self.stream_mode = False
            self._current_stream_info = None
            self.last_marvin_speech_time = time.time()  # 重置嘲諷計時器
            logger.info("🎵 [Stream Loop] 佇列播放完畢。")
            self.stt_logger.info("[串流結束] 音樂佇列播放完畢")
            if self.active_text_channel:
                await self.active_text_channel.send("🎵 **【串流播放完畢】** 佇列已空。就跟馬文的希望一樣——消失殆盡。")

        except asyncio.CancelledError:
            logger.info("🎵 [Stream Loop] 串流迴圈被取消。")
        except Exception as e:
            logger.error(f"❌ [Stream Loop] 發生異常: {e}")
            self.stream_mode = False

    def _parse_song_title_artist(self, info: dict) -> tuple[str, str]:
        """從 info 解析出乾淨的 title 和 artist，處理 'Artist - Title' 格式。"""
        raw_title = info.get('title', '')
        artist = info.get('artist') or info.get('uploader', '')
        if ' - ' in raw_title and not info.get('track'):
            parts = raw_title.split(' - ', 1)
            return parts[1].strip(), parts[0].strip()
        return info.get('track') or raw_title, artist

    async def _fetch_lyrics_synced(self, info: dict) -> str | None:
        """像 _fetch_lyrics_raw 但保留 [mm:ss.xx] timestamp（給 lyrics_seek 用）。

        同一條 provider 鏈（syncedlyrics → lrclib），但回 raw LRC 不剝 timestamp。
        沒有 timestamp 標記的回應視為「不可用」回 None — lyrics_seek 沒辦法在純文字上 seek。
        """
        import aiohttp
        title, artist = self._parse_song_title_artist(info)

        # Provider 1: syncedlyrics
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

        # Provider 2: lrclib /api/get 的 syncedLyrics 欄位
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

        # Provider 1: syncedlyrics（NetEase 中文覆蓋率高）
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

        # Provider 2: lrclib.net direct API
        try:
            async with aiohttp.ClientSession() as session:
                params = {'track_name': title, 'artist_name': artist}
                if duration:
                    params['duration'] = duration
                async with session.get('https://lrclib.net/api/get', params=params, timeout=aiohttp.ClientTimeout(total=8)) as r:
                    if r.status == 200:
                        data = await r.json()
                        plain = data.get('plainLyrics') or ''
                        if plain:
                            return plain
                        if data.get('syncedLyrics'):
                            return _strip_lrc(data['syncedLyrics'])

                async with session.get('https://lrclib.net/api/search', params={'q': f"{artist} {title}"}, timeout=aiohttp.ClientTimeout(total=8)) as r:
                    if r.status == 200:
                        for item in (await r.json())[:3]:
                            plain = item.get('plainLyrics') or ''
                            if plain:
                                return plain
                            if item.get('syncedLyrics'):
                                return _strip_lrc(item['syncedLyrics'])
        except Exception as e:
            logger.warning(f"⚠️ [Lyrics/lrclib] {e}")

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

    async def _fetch_dj_interjection_raw(self, info: dict) -> dict | None:
        """預先生成 DJ 播報：LLM 文字 + TTS 預渲染音訊。回傳 {'text', 'audio_path'} 或 None。

        2026-05-20 修改：原本 25% random gate 讓 75% user-requested 歌沉默播放
        （user 抱怨「點完歌無聲無息」）。改成 user-requested 永遠播；LLM 失敗
        / text 太短時回退 hardcoded template 確保一定唸出歌名 + 點播者。
        """
        requester = info.get('requested_by', '')
        if not requester:
            return None

        # Marvin 自選 round 內第 2、3 首 stagger TTS 生成，避免並發打爆 edge-tts rate limit
        # position 0 = 立即；1 = 3s；2 = 6s（_round_position 由 _auto_recommend 寫入）
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

        # ★ Fix 1 (2026-05-20): 移除 25% random gate — user-requested 永遠播 DJ。
        # 原本只有 ≥2 次點播 / 有情感記錄 / 有歌詞呼應 / 25% 抽中才會播，
        # 導致第一次點的歌 75% 機率沉默 → user 不知道有沒有點到。

        # 近期對話（最多 4 筆非 Marvin 的發言）
        conv_lines = []
        conv_buf = getattr(getattr(self.bot, 'engine', None), 'conv_buffer', None)
        if conv_buf:
            for entry in conv_buf.get_last_n_utterances(4):
                if entry.get('speaker') != 'Marvin':
                    conv_lines.append(f"{entry['speaker']}：「{entry['text'][:25]}」")

        slot = mm.time_slot(time.time()) if mm else ''
        title = info.get('title', '')
        ctx = [f"歌曲：《{title}》", f"點播者：{requester}"]
        if play_count >= 2:
            ctx.append(f"{requester} 第 {play_count} 次點這首")
        if feelings:
            ctx.append(f"情感記錄：{' / '.join(feelings[:2])}")
        if lyric_match:
            ctx.append(f"歌詞呼應：{lyric_match[:60]}")
        if slot:
            ctx.append(f"時段：{slot}")
        if conv_lines:
            ctx.append("頻道近期對話：\n" + '\n'.join(conv_lines))

        if requester.startswith('Marvin'):
            # 所有 Marvin 自選曲（round 首曲 + 後續）一律走個人化短語，100% 觸發
            clean_title, clean_artist = self._parse_song_title_artist(info)
            spotlight = info.get('_spotlight', '')
            lane = info.get('_lane', '')
            text = self._autopilot_dj_phrase(spotlight, clean_title, clean_artist, lane=lane)
        else:
            try:
                text = await self.bot.router.generate_dynamic_system_msg(
                    'dj_interjection', context='\n'.join(ctx)
                )
            except Exception as e:
                logger.warning(f"⚠️ [DJ Prefetch] LLM 失敗，使用 fallback template: {e}")
                text = ""

        text = (text or '').strip()
        # ★ Fix 3 (2026-05-20): text 太短 → hardcoded fallback 保證一定有聲音
        # （沒有 None return path — user-requested 必須有 announcement）
        # ★ 2026-05-26：fallback 也要走 DJ Marvin 人設，避免 LLM 失敗時掉回中性語氣
        if len(text) < 2:
            clean_title, clean_artist = self._parse_song_title_artist(info)
            if clean_artist:
                text = f"DJ Marvin為你帶來{clean_artist}演唱的{clean_title}，{requester} 點的"
            else:
                text = f"DJ Marvin為你帶來《{clean_title}》，{requester} 點的"
            logger.info(f"🎙️ [DJ Prefetch] 採用 fallback template")

        # 🚦 [TTS Gate] LLM 不聽 7s 指示時最後一道防線，在符號處截斷
        from tts_length_policy import truncate_for_tts
        gated_text, was_cut = truncate_for_tts(
            text, "music_intro", self.bot.tts_engine.get_estimated_duration
        )
        if was_cut:
            logger.info(f"🚦 [TTS Gate] DJ intro 超 7s 截斷: '{text}' → '{gated_text}'")
            text = gated_text

        # 預渲染 TTS 音訊（generate_audio 有 MD5 cache，同文字不重複產生）
        audio_path = None
        try:
            audio_path = await self.bot.tts_engine.generate_audio(text)
        except Exception as e:
            logger.warning(f"⚠️ [DJ Prefetch] TTS 預渲染失敗，改用即時串流: {e}")

        logger.info(f"🎙️ [DJ Prefetch] 完成: {text[:30]}… (audio={'✓' if audio_path else '✗'})")
        return {'text': text, 'audio_path': audio_path}

    # Cold-path meta fetch（queue 空、第 1 首沒 prefetch）的 timeout 上限
    _COLD_META_TIMEOUT_S = 5.0

    async def _meta_with_ack_fallback(self, info: dict, requested_by: str) -> dict:
        """冷啟動 meta fetch + 5s timeout fallback。

        2026-05-20 incident：DJ always-fire 改動讓 _fetch_song_meta 在冷啟動
        跑 LLM+TTS 30+ 秒，阻塞 event loop → Discord voice gateway 斷線。
        修法：asyncio.wait_for 限 5s；timeout → hardcoded fallback meta
        （dj.text 含歌名+點播者，audio_path=None 讓下游走即時 TTS）。

        2026-05-23：移除這裡的音樂 ack（搬到 _handle_voice_music_command
        cmd=="play"，MusicAgent 接走即播，hot/cold path 都涵蓋）。

        Queue 中第 2+ 首走 _prefetch_cache 路徑，不會進這裡。
        """
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
                    "audio_path": None,  # 無預渲染 → _maybe_play_dj_interjection 走即時 play_tts
                },
            }

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

    async def _maybe_play_dj_interjection(self, dj: dict | None):
        """播放預先生成的 DJ 播報。有預渲染音訊則直接播檔案，否則即時串流。"""
        if not dj:
            return
        text = dj.get('text', '')
        audio_path = dj.get('audio_path')
        if not text:
            return

        self._tts_protected = True
        try:
            if audio_path and os.path.exists(audio_path):
                await self.play_local_file(audio_path)
            else:
                await self.play_tts(text, already_in_channel=True)
        finally:
            self._tts_protected = False

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
                # Companion bridge: emit per-user reactions
                try:
                    from bridge_emitters import emit_music_reaction_to_bridge
                    for username, r in reactions.items():
                        feelings = r.get("feelings", []) or []
                        # 簡易映射：feelings 有就 love；無就 silent
                        tag = "love" if feelings else "silent"
                        asyncio.create_task(emit_music_reaction_to_bridge(
                            self.bot, username, info, tag
                        ))
                except Exception as e:
                    logger.debug(f"⚠️ [Companion_Bridge] music_reaction hook skipped: {e}")
        except Exception as e:
            logger.debug(f"⚠️ [MusicMemory] 反應分析失敗: {e}")

    @staticmethod
    def _autorecommend_seed(requested_by: str | None, online_members: list[str]) -> str | None:
        """佇列空時決定要不要續推自動推薦、用誰當 seed user。回 None = 不續推。

        - '未知' sentinel / 空 → 不續推
        - 使用者點的歌 → 用該使用者當 seed
        - Marvin 自己推薦的歌（連續 ambient）→ 用在場成員當 seed；房間沒人 → 不續推
          （空房交給既有 auto-dismiss 收場，不對空房 DJ）

        註：Marvin 歌也要續推是關鍵——否則一輪 Marvin 推薦播完佇列空、串流就死。
        _auto_recommend 內優先用 get_online_members()，seed 僅作 fallback。
        """
        if not requested_by or requested_by == '未知':
            return None
        if requested_by.startswith('Marvin'):
            return online_members[0] if online_members else None
        return requested_by

    async def _t2_discovery_candidates(self, members: list[str], exclude_titles: list[str]) -> list:
        """T2 discovery：多 seed → ytmusic radio 混合取相關新歌 → Candidate(direct_url)。

        seed 池（多 seed 混合，反映群組整體口味而非單一歌）：
          1. 使用者最近**手動點**的歌（跟當下心情走，排第一 → 混合時佔前段）
          2. 點播史真人點過的歌（get_played_seed_ids，排除 Marvin 自薦，按次數加權，每輪輪播窗口）
          3. liked 歷史補
        取前 N seed 各跑 radio → blend_radio_results 交錯混合去重。只用正向訊號（不用 skipped）。
        blocking get_watch_playlist 走 asyncio.to_thread，單 seed 失敗只跳過該 seed。
        全 seed 空 / radio 全掛 / 全被 exclude → 回 []（→ 退 T3 回收）。
        """
        mm = getattr(self.bot, 'music_memory', None)
        if mm is None:
            return []
        seeds: list[str] = []
        last = getattr(self, '_last_user_song_seed', None)   # 使用者最近點歌優先
        if last:
            seeds.append(last)
        hist = mm.get_played_seed_ids(members, limit=30)
        if hist:
            # 每輪輪播窗口起點，避免每次都同一批 top seeds
            self._t2_seed_idx = (getattr(self, '_t2_seed_idx', -1) + 1) % len(hist)
            hist = hist[self._t2_seed_idx:] + hist[:self._t2_seed_idx]
        # LLM 品味鄰近 seed（破回音室；env-gated，每日離線快取，runtime 只讀 videoId）
        avoid_artists: list[str] = []
        llm_seeds: list[str] = []
        if os.getenv("LLM_TASTE_T2", "off") == "on":
            try:
                import taste_profile
                _MAX_AGE = 8 * 86400
                llm_seeds = taste_profile.fresh_seed_ids(_TASTE_PROFILE_CACHE, members, _MAX_AGE)
                avoid_artists = taste_profile.fresh_avoid_artists(_TASTE_PROFILE_CACHE, members, _MAX_AGE)
            except Exception as e:
                logger.warning(f"⚠️ [AutoRecommend] T2 LLM 品味快取讀取失敗，略過: {e}")
        # Step 3 retreat：skip 驅動的藝人級避開（≥2 首被 skip），但保護指紋核心藝人
        # （核心被 skip 是單曲層級，不代表整個方向爛）。永遠套用、不受 LLM env gate 限制。
        try:
            _core = {a for a, _ in self._load_taste_fingerprint().get("core_artists", [])}
            for _a in mm.get_explore_avoid_artists():
                if _a not in _core and _a not in avoid_artists:
                    avoid_artists.append(_a)
        except Exception:
            logger.debug("[AutoRecommend] explore retreat avoid 合併失敗", exc_info=True)
        # Step 3 promotion：有反應沒被 skip 的歌（含 Marvin 發現後大家有感的）→ 升級 seed
        reacted_seeds = mm.get_reacted_seed_ids(members)
        # 交錯 history / LLM 鄰近 / 有反應（promoted），確保 novelty seed 進前 N
        from itertools import zip_longest
        for h, l, r in zip_longest(hist, llm_seeds, reacted_seeds):
            for vid in (h, l, r):
                if vid and vid not in seeds:
                    seeds.append(vid)
        for vid in mm.get_liked_video_ids(members):
            if vid not in seeds:
                seeds.append(vid)
        _N_SEEDS = 3
        seeds = seeds[:_N_SEEDS]
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
        radio = blend_radio_results(
            results, exclude_titles=exclude_titles, limit=self._round_size * 3)
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

    def _load_taste_fingerprint(self) -> dict:
        """讀 records/taste_fingerprint.json（5 分鐘快取；缺檔/壞檔 → {} fail-open）。

        週生成的 deterministic 口味指紋，供 T2 explore 用主導語言當地板。
        """
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

    async def _auto_recommend(self, username: str, *, _tier: int = 1):
        """佇列空 → 依在場成員的音樂記憶推薦下一首批 (Phase 1: 一次推 3 首為一 round)。

        _tier：候選來源層級。T1 團體記憶 → T2 ytmusic discovery → T3 放寬回收。
        某層「實際入隊數=0」（cands 空 OR 全被 ring/dedup 濾光）→ 遞迴進下一層，
        確保佇列真的有新歌（修 2026-06-04：原本只看 cands 空、漏了 enqueued=0 卡住）。

        Phase 1 M4 ambient room curator:
          - 一次呼叫 enqueue 最多 3 首 (一 round, ≈15min)
          - 整合 MoodSensor (M2) → vibe_filter 給 recommender
          - 每 candidate 過 Cover Quality Hard Filter (M1)
          - 進新 round 時 invalidate MoodSensor cache (即每 round 重評 vibe)

        變化與團體聚合由 music_recommender 的確定性候選池 + 加權抽樣負責；LLM 只在
        spotlight lane 把選定錨點 cover 化 + vibe sensor。direct lane 直接重播。
        """
        mm = getattr(self.bot, 'music_memory', None)
        if mm is None:
            return

        # 在場成員（團體）；空則退回點歌者
        members = self.get_online_members() or [username]

        # spotlight 輪替：每次推薦聚焦不同在場成員
        self._recommend_spotlight_idx = (self._recommend_spotlight_idx + 1) % len(members)
        spotlight = members[self._recommend_spotlight_idx]

        # exclude = 本場最近播 ∪ 最近推薦 ring（活過重啟）∪ 在場者 skipped ∪ 在場者 suki history
        recently = [s['title'] for s in list(self.stream_history)[-15:]]
        recommended = mm.get_recent_recommendation_titles()
        skipped = mm.get_skipped_titles(members)
        suki_hist: list[str] = []
        _suki = getattr(self.bot.router, 'memory', None)
        if _suki is not None:
            for m in members:
                suki_hist += (_suki.get_song_history(m) or [])[:10]
        exclude_titles = list(dict.fromkeys(recently + recommended + skipped + suki_hist))

        # Phase 1 M2: vibe sensor — 進新 round invalidate cache 強制重評
        vibe_filter = None
        vibe_label = None
        if self._mood_sensor is not None:
            try:
                self._mood_sensor.invalidate()
                guild_id = self.active_text_channel.guild.id if self.active_text_channel else 0
                vibe_label = await self._mood_sensor.current_vibe(guild_id=guild_id)
                vibe_filter = {"mood": vibe_label.mood, "topic": vibe_label.topic, "min_score": 0.0}
                logger.info(f"🎵 [AutoRecommend] vibe={vibe_label.mood} (engagement={vibe_label.engagement:.2f}, source={vibe_label.source})")
            except Exception as e:
                logger.warning(f"⚠️ [AutoRecommend] vibe sensor 失敗，fallback to no vibe filter: {e}")

        pool = build_recommendation_pool(
            members=members,
            songs=mm.all_songs(),
            exclude_titles=exclude_titles,
            now=time.time(),
            spotlight_member=spotlight,
            vibe_filter=vibe_filter,
        )

        # video-id 排除（穩定鍵，取代脆弱歌名比對）：skip 過永久排、播過拉長視窗排。
        # T1/T2 套兩者；T3 回收只保留永久 skip（放寬播過視窗，與下方 ring_exclude 同步，
        # 避免「拉長窗」把 recommender 餓死沒歌放）。
        _skipped_vids = mm.get_skipped_video_ids()
        _taste_fp = self._load_taste_fingerprint()   # Step 2: T2 explore 語言地板用

        # 本層候選來源 + enqueue 時 ring 檢查嚴格度（ring_exclude / excluded_vids）。
        if _tier == 1:
            # T1 團體記憶（9-pick-3）
            cands = pick_candidates(pool, k=self._round_size, top_n=9)
            ring_exclude = exclude_titles
            excluded_vids = _skipped_vids | mm.get_recently_played_video_ids(self._PLAYED_EXCLUDE_TTL_S)
        elif _tier == 2:
            # T2 discovery：在場者 liked 的歌當 seed → ytmusic radio 取相關新歌
            cands = await self._t2_discovery_candidates(members, exclude_titles)
            ring_exclude = exclude_titles
            excluded_vids = _skipped_vids | mm.get_recently_played_video_ids(self._PLAYED_EXCLUDE_TTL_S)
        else:
            # T3 回收：放寬 exclude 到只保留 skipped（鬆開 recently/ring/suki），讓非 skipped
            # 老歌重新發現。ring_exclude 同步放寬，否則 enqueue 的 ring 檢查又把它擋掉。
            relaxed_pool = build_recommendation_pool(
                members=members, songs=mm.all_songs(),
                exclude_titles=list(dict.fromkeys(skipped)),
                now=time.time(), spotlight_member=spotlight, vibe_filter=vibe_filter,
            )
            cands = pick_candidates(relaxed_pool, k=self._round_size, top_n=9)
            ring_exclude = list(dict.fromkeys(skipped))
            excluded_vids = _skipped_vids   # 回收層只擋永久 skip，放寬播過視窗
        if not cands:
            if _tier < 3:
                return await self._auto_recommend(username, _tier=_tier + 1)
            logger.debug("🎵 [AutoRecommend] 三層皆無候選，跳過")
            return

        # 進新 round → reset track count
        self._round_track_count = 0

        # Phase 1 M1: cover quality filter lazy init
        if self._cover_blacklist is None:
            try:
                from track_quality import CoverBlacklist
                self._cover_blacklist = CoverBlacklist.shared()
            except Exception:
                logger.exception("[AutoRecommend] CoverBlacklist init 失敗")

        enqueued = 0
        for cand in cands:
            # 每輪入隊上限 round_size——候選清單可能比 round_size 多（如 T2 radio 給 9 個當
            # 備援），但只填 round_size 首進佇列，不一次冒一堆出來（其餘留著下輪/失敗時補位）。
            if enqueued >= self._round_size:
                break
            # mode → query：T2 direct_url 自帶 URL 直解；cover 交給 LLM cover 化；direct 重播錨點
            if cand.direct_url:
                query = cand.direct_url   # _resolve_yt_query 認 http 開頭 → 直接 extract 不搜尋
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
            if self._check_song_duplicate(url=info['url'], title=info['title'], username=username):
                logger.info(f"🎵 [AutoRecommend] {info['title']} 本場已播過，略過")
                continue
            if is_already_recommended(info['title'], ring_exclude):
                logger.info(f"🎵 [AutoRecommend] {info['title']} 已在 recent ring，略過")
                continue
            # video-id 排除（穩定鍵）：skip 過永久 / 播過拉長視窗。歌名比對救不到的
            # 重複（yt-dlp 歌名漂移）由這條擋。
            _cand_vid = extract_video_id(info.get('webpage_url') or info.get('url') or '')
            if _cand_vid and _cand_vid in excluded_vids:
                logger.info(f"🎵 [AutoRecommend] {info['title']} video-id 已播過/已skip，略過")
                continue
            # 非單曲過濾：合輯 / 紀錄片 / 簡介長片（旁白多、不是歌）一律避開。
            from track_quality import is_non_song_video
            _ns, _ns_reason = is_non_song_video(info.get('title', ''), info.get('duration'))
            if _ns:
                logger.info(f"🚫 [AutoRecommend] 非單曲略過 '{info['title']}': {_ns_reason}")
                continue
            # Step 2: explore（T2 新歌發現）錨定口味地板——主導語言不符的新歌略過，
            # 讓驚喜留在甜蜜區（華語）。exploit（T1 重播愛歌，含少數英文老歌）不套。
            if _tier == 2:
                from taste_fingerprint import explore_matches_floor
                if not explore_matches_floor(info.get('title', ''), _taste_fp):
                    logger.info(f"🎵 [AutoRecommend] explore 不合口味地板(語言)略過: {info['title']}")
                    continue

            # Phase 1 M1: cover quality filter (hard ban 低播放 cover / 黑名單)
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

            info['requested_by'] = f"Marvin推薦（為{spotlight}）"
            # Phase 1 M6: round 第 1 首 → 標記「賭一把」mode 給 DJ persona
            info['_round_first'] = (enqueued == 0)
            info['_spotlight'] = spotlight          # DJ intro 個人化用
            info['_lane'] = cand.lane               # DJ intro 群組 vs 個人判斷用
            info['_round_position'] = enqueued      # DJ intro stagger 用（0=立即,1=3s,2=6s）

            self.stream_queue.append(info)
            mm.add_recent_recommendation(info['title'])
            logger.info(f"🎵 [AutoRecommend] lane={cand.lane} round-#{enqueued+1}: {info['title']}")
            blurb = ""
            if self.active_text_channel and enqueued == 0:
                # Round blurb 一次發、後 2 首不另開訊息（避免洗版）
                vibe_tag = f" [vibe: {vibe_label.mood}]" if vibe_label else ""
                blurb = self._recommend_blurb(cand, info['title'], spotlight=spotlight) + vibe_tag
                await self.active_text_channel.send(blurb)
            # offline feedback log：每首 autopilot 推薦都進 jsonl，明天 analyze
            # 2026-05-28 Phase 1：豐富 channel_state — vibe / queue position / history /
            # depth，給 analyzer 抽「什麼樣的推薦會被 skip」pattern。
            _recent_titles = [
                s.get("title", "") for s in self.stream_history[-3:] if isinstance(s, dict)
            ]
            append_recommendation(build_autopilot_recommendation(
                speaker=spotlight, title=info['title'], lane=cand.lane, mode=cand.mode,
                anchor_title=cand.anchor_title, blurb=blurb, now=time.time(),
                channel_state_extras={
                    "vibe_mood": vibe_label.mood if vibe_label else None,
                    "vibe_engagement": (
                        round(vibe_label.engagement, 2) if vibe_label else None
                    ),
                    "queue_position": enqueued,         # round 內第幾首（0-index）
                    "round_first": info['_round_first'],
                    "queue_depth": len(self.stream_queue),
                    "recent_history_titles": _recent_titles,
                    "spotlight_member": spotlight,
                },
            ))

            # 對新推薦的歌也啟動預取
            next_url = info.get('url', '')
            if next_url and next_url not in self._prefetch_cache:
                self._prefetch_cache[next_url] = asyncio.create_task(self._fetch_song_meta(info))

            enqueued += 1

        logger.info(f"🎵 [AutoRecommend] T{_tier} round 完成: enqueued={enqueued}/{self._round_size}")
        if enqueued == 0 and _tier < 3:
            # cands 非空但全被 ring/dedup/quality 濾光 → 進下一層找真的能播的（修 ring 飽和卡死）
            await self._auto_recommend(username, _tier=_tier + 1)

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

    def _recommend_blurb(self, cand, title: str, spotlight: str = "") -> str:
        """依 lane 產生推薦時的自我說明文案（透明度：讓人知道為何推這首）。

        spotlight：本輪聚焦的在場成員（_auto_recommend 輪替傳入）。
        group_resonance 是群體共鳴，不點名個人；其餘 lane 若有 spotlight 則標示替誰推。
        """
        if cand.lane == "group_resonance":
            return f"🎵 **【馬文精選】** 你們都有共鳴的《{title}》，再聽一次吧。"
        who = cand.target_member or spotlight or "你"
        if cand.lane == "long_tail":
            return f"🎵 **【馬文精選】** 為 `{who}` 從塵封歌單挖出《{title}》。"
        if cand.lane == "discovery":
            return f"🎵 **【馬文精選】** 為 `{who}` 挖到新歌《{title}》，聽聽看。"
        return f"🎵 **【馬文精選】** 為 `{who}` 翻出的《{title}》。"

    _AUTOPILOT_DJ_PHRASES_PERSONAL = [
        "這首幫{who}點的，{artist}唱的{title}",
        "{who}應該喜歡這首，{artist}的{title}",
        "希望{who}喜歡，{artist}演唱的{title}",
        "馬文特別為{who}帶來，{artist}的{title}",
        "這首{title}是給{who}的，{artist}唱的",
    ]
    _AUTOPILOT_DJ_PHRASES_PERSONAL_NO_ARTIST = [
        "這首幫{who}點的，《{title}》",
        "{who}應該喜歡，《{title}》",
        "希望{who}喜歡這首，《{title}》",
        "馬文特別為{who}帶來《{title}》",
    ]
    _AUTOPILOT_DJ_PHRASES_GROUP = [
        "這首大家應該都喜歡，{artist}的{title}",
        "為大家挑的，{artist}演唱的{title}",
        "馬文覺得大家都喜歡這首，{artist}的{title}",
    ]
    _AUTOPILOT_DJ_PHRASES_GROUP_NO_ARTIST = [
        "這首大家應該都喜歡，《{title}》",
        "馬文為大家挑的，《{title}》",
    ]

    @staticmethod
    def _autopilot_dj_phrase(spotlight: str, clean_title: str, clean_artist: str,
                              lane: str = "") -> str:
        """為 autopilot 推薦歌曲生成個人化 DJ 台詞。"""
        import random
        who = spotlight or "你"
        is_group = (lane == "group_resonance")

        if is_group:
            pool = (VoiceController._AUTOPILOT_DJ_PHRASES_GROUP if clean_artist
                    else VoiceController._AUTOPILOT_DJ_PHRASES_GROUP_NO_ARTIST)
        else:
            pool = (VoiceController._AUTOPILOT_DJ_PHRASES_PERSONAL if clean_artist
                    else VoiceController._AUTOPILOT_DJ_PHRASES_PERSONAL_NO_ARTIST)

        tmpl = random.choice(pool)
        return tmpl.format(who=who, title=clean_title, artist=clean_artist)

    async def _handle_find_song(self, mode: str, payload: str, speaker: str):
        """FindSongAgent handler：依模式識別歌名 → 報出識別結果 → 交給播放路徑。

        find_lyrics 模式優先走 Gemini + google_search grounding（避免 LLM 盲猜幻覺）；
        grounded 識別 miss / 不可用才退回 LLM。其他三模式（theme/album/artist）維持 LLM 路徑。
        識別可能猜錯所以播放前先報出結果保留透明度。
        """
        ident: str = ""

        # find_lyrics → 先走 grounded 搜尋（真的去搜，不盲猜）
        if mode == "find_lyrics" and payload and payload.strip():
            grounded = await search_lyrics_grounded(
                getattr(self.bot.router, "google_client", None),
                payload.strip(),
            )
            if grounded:
                ident = grounded

        # Fallback：grounded miss 或非 find_lyrics 模式 → 原 LLM 路徑
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
            if self.active_text_channel:
                await self.active_text_channel.send(f"🔎 **【找歌】** 找不到符合「{payload}」的歌，換個說法試試？")
            asyncio.create_task(self._play_ack("music_fail", speaker=speaker))
            return

        # find_lyrics 模式：嘗試在 LRC 找 fragment 時間戳，命中就在訊息附帶「副歌 X 在 mm:ss」
        # 不影響播放路徑（MVP：仍從頭播；播放層 seek 之後再做）
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

        if self.active_text_channel:
            await self.active_text_channel.send(
                f"🔎 **【找歌】** 我找到的應該是 `{ident}`{seek_suffix}，幫你播了。"
            )
        await self._safe_music_command(speaker, ident, "play")

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

    async def play_stream_song(self, url: str, title: str, dj_audio_path: str | None = None):
        """🎵 播放單首串流音樂，等待播放完成後 return。
        dj_audio_path: 若提供，DJ 語音與音樂混音播放（前奏 ducking）。
        """
        import shlex

        vc = next((v for v in self.bot.voice_clients if v.is_connected()), None)
        if not vc:
            logger.warning("⚠️ [Stream Song] 無連線中的 VoiceClient，跳過。")
            self.stream_mode = False
            return

        self._current_stream_url = url
        use_mix = dj_audio_path and os.path.exists(dj_audio_path)

        if use_mix:
            vol = self.stream_volume
            fc = (
                f"[0:a]asplit=2[dj_sc][dj_mix];"
                f"[dj_sc]apad=whole_dur=9999[dj_pad];"
                f"[1:a]loudnorm=I=-14:TP=-1.5:LRA=11,volume={vol:.3f}[music];"
                f"[music][dj_pad]sidechaincompress=threshold=0.02:ratio=8:attack=5:release=600[ducked];"
                f"[ducked][dj_mix]amix=inputs=2:duration=longest:normalize=0[out]"
            )
            before_opts = (
                f"-i {shlex.quote(dj_audio_path)} "
                "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -probesize 32M"
            )
            options = f"-vn -bufsize 512k -filter_complex \"{fc}\" -map [out]"
            logger.info(f"🎙️ [DJ Mix] 混音模式：{os.path.basename(dj_audio_path)}")
            ffmpeg_opts = {'before_options': before_opts, 'options': options}
            self._mixer.set_volume(1.0)
            await self._mixer_play_music(
                vc, discord.FFmpegPCMAudio(url, **ffmpeg_opts),
                still_active=lambda: self.stream_mode,
            )
        else:
            p12_opts = {
                'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -probesize 32M',
                'options': '-vn -bufsize 512k',
            }
            if url not in self._stream_norm_gain:
                asyncio.create_task(self._measure_norm_gain_bg(url))
            await self._mixer_play_music(
                vc, discord.FFmpegPCMAudio(url, **p12_opts),
                still_active=lambda: self.stream_mode, volume_attr="stream_volume",
            )

    async def _measure_norm_gain_bg(self, url: str):
        """[響度正規化] 背景取樣歌曲 25/50/75% 三點量整合響度 → 算常數增益存
        _stream_norm_gain[url]，mixer 同步乘進使用者音量（每首套一次、不 pumping）。

        每首只量一次（已有直接 return）。失敗/逾時 → 不存（mixer 用 1.0 raw，graceful）。
        subprocess 用 create_subprocess_exec（對齊 async 規範，不阻塞播放）。
        """
        if url in self._stream_norm_gain:
            return
        from loudness_norm import (
            sample_positions, parse_ebur128_integrated, average_lufs, compute_loudness_gain,
        )
        info = self._current_stream_info or {}
        duration = float(info.get("duration") or 0)
        lufs_vals: list[float | None] = []
        for pos in sample_positions(duration):
            try:
                proc = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-nostats", "-ss", f"{pos:.1f}", "-t", "20", "-i", url,
                    "-af", "ebur128", "-f", "null", "-",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
                lufs_vals.append(parse_ebur128_integrated(stderr.decode("utf-8", "ignore")))
            except Exception:
                lufs_vals.append(None)
        avg = average_lufs(lufs_vals)
        if avg is None:
            logger.warning(f"⚠️ [LoudNorm] {url[:40]} 響度量測無結果，用 raw 音量")
            return
        gain = compute_loudness_gain(avg)
        self._stream_norm_gain[url] = gain
        logger.info(f"🎚️ [LoudNorm] 量測完成 I≈{avg:.1f} LUFS → 增益 {gain:.2f}x（每首套一次）")

    def _extract_song_metadata(self, file_path: str):
        """
        📻 [Marvin Radio] 使用 ffprobe 提取標題與演出者
        """
        try:
            # 優先嘗試 /opt/homebrew/bin/ffprobe，若無則嘗試原始 path
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
        """
        📻 [Marvin Radio] 使用 ffmpeg 提取封面至暫存檔
        """
        try:
            # 建立一個暫存路徑
            temp_fd, temp_path = tempfile.mkstemp(suffix=".jpg")
            os.close(temp_fd)
            
            # 優先嘗試 /opt/homebrew/bin/ffmpeg
            ffmpeg_path = "/opt/homebrew/bin/ffmpeg" if os.path.exists("/opt/homebrew/bin/ffmpeg") else "ffmpeg"
            
            # -y 覆蓋, -i 輸入, -an 移除音訊, -vcodec copy 直接複製影像串流 (封面), -f image2 格式, -frames:v 1 只取一張
            cmd = [ffmpeg_path, "-y", "-i", file_path, "-an", "-vcodec", "copy", "-f", "image2", "-frames:v", "1", temp_path]
            subprocess.run(cmd, capture_output=True, check=True)
            
            # 檢查檔案是否真的有內容且不是 0 byte
            if os.path.exists(temp_path) and os.path.getsize(temp_path) > 0:
                return temp_path
            else:
                if os.path.exists(temp_path): os.remove(temp_path)
                return None
        except Exception:
            if 'temp_path' in locals() and os.path.exists(temp_path): os.remove(temp_path)
            return None

    def _extract_dominant_color(self, cover_path: str) -> discord.Color:
        """
        📻 [Marvin Radio] 從封面圖提取主色調，過濾近黑/近白，返回 discord.Color。
        使用 Pillow quantize (Median Cut) 找 8 個色塊，挑飽和度最高且亮度適中的。
        """
        try:
            from PIL import Image
            img = Image.open(cover_path).convert("RGB")
            img = img.resize((60, 60), Image.LANCZOS)
            quantized = img.quantize(colors=8)
            palette = quantized.getpalette()  # [r,g,b, r,g,b, ...]

            best_color = None
            best_score = -1.0

            for i in range(8):
                r, g, b = palette[i * 3], palette[i * 3 + 1], palette[i * 3 + 2]
                lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255.0
                # 跳過太暗（< 10%）或太亮（> 90%）的色塊，在 Discord 深色背景上不明顯
                if lum < 0.10 or lum > 0.90:
                    continue
                max_c = max(r, g, b) / 255.0
                min_c = min(r, g, b) / 255.0
                # HSL 飽和度計算
                denom = 1.0 - abs(2.0 * lum - 1.0)
                sat = (max_c - min_c) / denom if denom > 0.001 else 0.0
                # 偏好高飽和 + 中等亮度
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
        """
        📻 [Marvin Radio] 延後刪除暫存檔，確保 Discord 上傳完成。
        """
        try:
            await asyncio.sleep(delay)
            if os.path.exists(file_path):
                os.remove(file_path)
                logger.debug(f"🧹 [Radio Cleanup] 已刪除暫存封面: {file_path}")
        except Exception as e:
            logger.error(f"⚠️ [Radio Cleanup] 刪除暫存檔失敗: {e}")

    async def self_restart(self, reason: str = "未知原因", force: bool = False, pull: bool = True):
        """物理重啟流程。

        關鍵不變式：**無論 pre-execv 任何步驟失敗，必須走到 os.execv**。
        以前 memory.flush() 在 SQLite 重構過渡期會噴 AttributeError，
        導致 /marvin_reboot 卡死沒重啟（log 留下 "已執行重啟" 但其實沒有）。
        現在所有 pre-execv 步驟都被 try/except 包住。

        重啟完成回報：寫狀態檔（.marvin_reboot_state.json）到 cwd，
        新進程 on_ready 讀取後貼完成訊息到原頻道並刪檔。
        """
        if not force and (time.time() - getattr(self.bot, "last_restart_time", 0) < 900): return

        logger.critical(f"🚀 [Restart] 正在執行進程級重啟，原因：{reason}")
        if self.active_text_channel:
            try: await self.active_text_channel.send(f"⚠️ **【系統診斷：聽覺異常】**\n軟修復失效，正在執行物理重啟 ({reason}) 以重新同步金鑰。")
            except: pass

        # 1. 原子性數據保護：強制存入記憶
        # SQLite per-mutation 已自動 commit；flush() 是 API 相容用的 no-op。
        # 包 try/except 是為了任何 MemoryManager 過渡版本（含 deprecated method）也不卡 restart。
        try:
            logger.info("💾 [Restart] 正在執行最後的記憶存檔...")
            self.bot.router.memory.flush()
        except Exception as e:
            logger.error(f"❌ [Restart] memory.flush() 失敗（不阻斷重啟流程）: {e}")

        # 2. git pull 拿最新 code（pull=False 可關閉，例如 dev 階段不想動 working tree）
        commit_before = _git_head_short()
        commit_after = commit_before
        pull_summary = "(skipped)"
        if pull:
            try:
                logger.info("📥 [Restart] 正在 git pull 拿最新 code...")
                proc = await asyncio.create_subprocess_exec(
                    "git", "pull", "--ff-only", "origin",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15.0)
                out = stdout.decode("utf-8", errors="replace").strip()
                logger.info(f"📥 [Restart] git pull 結果（rc={proc.returncode}）:\n{out}")
                pull_summary = f"rc={proc.returncode}\n{out[:1200]}"
                commit_after = _git_head_short()
                if self.active_text_channel:
                    try:
                        await self.active_text_channel.send(
                            f"📥 git pull (rc={proc.returncode}):\n```\n{out[:1500]}\n```"
                        )
                    except Exception:
                        pass
            except asyncio.TimeoutError:
                logger.error("❌ [Restart] git pull 超時 15s（不阻斷重啟）")
                pull_summary = "(timeout 15s)"
            except Exception as e:
                logger.error(f"❌ [Restart] git pull 失敗（不阻斷重啟）: {e}")
                pull_summary = f"(error: {type(e).__name__}: {e})"

        # 3. 寫狀態檔，供新進程 on_ready 讀取後貼完成訊息
        _write_reboot_state({
            "channel_id": self.active_text_channel.id if self.active_text_channel else None,
            "guild_id": self.active_text_channel.guild.id if self.active_text_channel and self.active_text_channel.guild else None,
            "reason": reason,
            "commit_before": commit_before,
            "commit_after": commit_after,
            "pull_summary": pull_summary,
            "started_at": time.time(),
        })

        # 4. 釋放資源與關閉連線（避免幽靈機器人殘留）
        try:
            logger.info("🔌 [Restart] 正在切斷 Discord 連線...")
            await self.bot.close()
        except Exception as e:
            logger.error(f"❌ [Restart] 關閉連線時發生異常（忽略並啟動 execv）: {e}")

        # 5. 物理進程替換（最後一道，沒退路）
        try:
            logger.critical("☢️ [Restart] 執行 os.execv，程序替換中...")
            args = sys.argv[:]
            os.execv(sys.executable, [sys.executable] + args)
        except Exception as e:
            # execv 不該失敗，若真失敗 bot 會死；至少留下 log 線索
            logger.critical(f"☢️ [Restart] os.execv 失敗！bot 將終結: {e}")
            raise

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
