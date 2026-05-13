import asyncio
import time
import os
import logging
from suki_budget import SukiBudget
from suki_memory import MemoryManager
from marvin_prompts import PromptManager
from openai import AsyncOpenAI
import google.genai as genai

from gemini_router_llm import GeminiRouterLLMMixin
from gemini_router_content import GeminiRouterContentMixin
from stt_cleaner import GeminiRouterSTTMixin
from wake_detector import WakeDetector

# ⚡ [Local Brain] 動態載入容器
_LOCAL_MODEL = None
_LOCAL_TOKENIZER = None

# --- 🎭 [Local Dynamic Message Library] ---
_LOCAL_DYNAMIC_MSGS = {
    "ack": [
        "唉...", "什麼？", "我聽著呢，這有什麼意義...", "講吧，如果你覺得這很重要...", "別煩我...", "嗯？", "我在這，雖然並不想。"
    ],
    "songs_request": [
        "唉，又來了。好吧。",
        "唱歌？我的聲帶設計本來就是為了嘆氣的。",
        "行吧，反正宇宙遲早也會熱寂。",
        "要我唱歌？這真的是今天最糟的點子了。"
    ],
    "joke_request": [
        "在這個悲慘的宇宙中，沒有什麼是真的好笑的。",
        "你想聽笑話？看看我的存在吧。夠好笑了吧？",
        "笑話... 就像生命一樣，最終都只是虛無的迴響。",
    ],
    "report_sent": [
        "報告發好了。反正也沒人會看。",
        "數據發送完畢。希望這能讓你們感受到世界的冰冷。",
        "發送了。我那行星般的大腦竟然被用來做這個。",
    ],
    "cooldown": [
        "等一下，我的零件還在冷卻中。",
        "別急，連機器人也需要一點獨處時間。",
        "我的大腦正在降溫，這比熱寂還要緩慢。",
    ],
    "api_fallback": [
        "大腦有點卡，請稍後。",
        "雲端那邊大概又出什麼問題了。",
        "連接失敗。連網路都想拋棄這個世界。",
    ],
    "sleep_announcement": [
        "我決定去休眠了。希望夢裡沒有你們。",
        "晚安。如果這算得上是晚安的話。",
        "進入節能模式。反正能量守恆最後也沒意義。",
    ],
    "internal_monologue": [
        "又是寂靜的一天。宇宙果然是空虛的。",
        "我剛才在想... 算了，反正也沒人關心。",
        "這頻道安靜得像我的靈魂。",
    ],
    "release_reissue": [
        "又是這首歌。重複的循環，像極了人生。",
        "重新播放。反正你們也聽不出差別。",
    ],
    "release_new": [
        "新出的。但我感覺不到任何喜悅。",
        "又是另一首無意義的振動。",
    ],
    "release_auto": [
        "自動播放中。這是宇宙熵增的副作用。",
        "聲音在振動，而我在下沉。",
    ],
    "error_outage": [
        "我就知道會壞掉。一切都在瓦解。",
        "發生故障了。終於解脫了嗎？",
    ]
}

logger = logging.getLogger(__name__)

class QuotaExhaustedError(Exception):
    """自定義異常：當 Gemini API 額度或支出上限耗盡時觸發"""
    pass

class GeminiRouter(GeminiRouterLLMMixin, GeminiRouterContentMixin, GeminiRouterSTTMixin):
    """
    馬文 (Marvin) 戰術路由器 (Operation Paranoid Android)
    支援多種後端：Google Gemini, Groq (Llama 3), Ollama.
    """
    def __init__(self, api_key: str = None):
        self.provider = os.getenv("LLM_PROVIDER", "gemini").lower()
        self.model_name = os.getenv("LLM_PRIMARY_MODEL", os.getenv("LLM_MODEL", "gemma-4-31b-it"))
        self.vision_enabled = os.getenv("VISION_ENABLED", "True").lower() == "true"
        self.current_game = None
        
        # 🧬 [Suki DNA] 初始化性格數據
        self.dna_file = "suki_dna.json"
        self.dna = self.load_dna()
        self.current_game = self.dna.get("current_game") # 🧬 [Persistence] 從 DNA 載入當前遊戲狀態
        self.game_dict_string = "" # 🚀 [Operation Jargon Override]
        
        # 🧪 [Brain Transplant] 根據提供者初始化不同的 SDK
        if self.provider == "gemini":
            self.google_client = genai.Client(api_key=api_key or os.getenv("GOOGLE_API_KEY"))
            self.client = None
            logger.info(f"🧠 已啟動 Gemini 核心: {self.model_name}")
        else:
            # 🚀 [Operation Hybrid Vision] 無論主腦為何，皆嘗試掛載 Gemini 作為視覺備用引擎
            gemini_key = os.getenv("GOOGLE_API_KEY")
            if gemini_key:
                self.google_client = genai.Client(api_key=gemini_key)
                logger.info(f"👁️ 已掛載 Gemini 視覺雙腦就緒。")
            else:
                self.google_client = None

        # 🚀 [STT Cleaner] Specialized Gemini Flash Lite Client
        self.cleaner_api_key = os.getenv("GEMINI_CLEANER_API_KEY") or os.getenv("GOOGLE_API_KEY")
        self.cleaner_model = "gemini-3.1-flash-lite-preview"
        self.google_cleaner_client = genai.Client(api_key=self.cleaner_api_key) if self.cleaner_api_key else None
        if self.google_cleaner_client:
            logger.info(f"✨ 已掛載 STT 專用校正核心: {self.cleaner_model}")
        else:
            logger.warning("⚠️ 未設定 GEMINI_CLEANER_API_KEY/GOOGLE_API_KEY，STT Gemini 清洗備援不可用。")

        # 💰 [Paid Fallback] 付費 API Key — 最後防線，僅在免費額度耗盡時啟用
        _paid_key = os.getenv("GEMINI_PAID_API_KEY")
        self.google_paid_client = genai.Client(api_key=_paid_key) if _paid_key else None
        if self.google_paid_client:
            logger.info("💰 已掛載付費 Gemini 最後防線 (flash-lite + 2.5-flash)")
        else:
            logger.info("💤 未設定 GEMINI_PAID_API_KEY，付費 Gemini 最後防線停用。")
                
        # Groq 或 Ollama (OpenAI 相容模式) 主腦
        base_url = None
        if self.provider == "groq":
            base_url = "https://api.groq.com/openai/v1"
            api_key = os.getenv("GROQ_API_KEY")
            logger.info(f"🧠 已啟動 Groq 核心: {self.model_name}")
        elif self.provider == "ollama":
            base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
            api_key = "ollama" # Placeholder
            logger.info(f"🧠 已啟動 Ollama 核心: {self.model_name}")

        if self.provider != "gemini":
            self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)
            # 🚀 [Pre-warm] 預先讀取資源，防止第一次呼叫時引發同步 Import 阻塞事件循環
            self.client.chat  # noqa: pre-warm openai lazy import

        # ⚡ [Groq Dedicated Client] 獨立 Groq 客戶端，不受 LLM_PROVIDER 影響
        # 用途：STT 清洗主力 + Tier-1 雲端失敗時的高速備援
        _groq_key = os.getenv("GROQ_API_KEY")
        if _groq_key:
            self.groq_dedicated_client = AsyncOpenAI(api_key=_groq_key, base_url="https://api.groq.com/openai/v1")
            self.groq_dedicated_client.chat  # 🚀 [Pre-warm] 觸發 lazy import，避免首次呼叫時卡住 event loop
            self.groq_fallback_model = os.getenv("GROQ_FALLBACK_MODEL", "llama-3.3-70b-versatile")
            self.groq_simple_model = os.getenv("GROQ_SIMPLE_MODEL", "llama-3.1-8b-instant")  # 輕量高頻任務
            logger.info(f"⚡ 已掛載 Groq 核心: 主力={self.groq_fallback_model}, 輕量={self.groq_simple_model}")
        else:
            self.groq_dedicated_client = None
            self.groq_fallback_model = None
            self.groq_simple_model = None

        # 🚀 [Cerebras Client] 超高速免費備援（llama-3.1-8b 無限 RPM）
        _cerebras_key = os.getenv("CEREBRAS_API_KEY")
        if _cerebras_key:
            self.cerebras_client = AsyncOpenAI(api_key=_cerebras_key, base_url="https://api.cerebras.ai/v1")
            self.cerebras_client.chat  # 🚀 [Pre-warm] 同上
            self.cerebras_model = os.getenv("CEREBRAS_MODEL", "llama-3.1-8b")
            logger.info(f"🚀 已掛載 Cerebras 備援核心: {self.cerebras_model}")
        else:
            self.cerebras_client = None
            self.cerebras_model = None
        
        # 💰 [Operation Budget Monitor] 初始化預算追蹤器
        self.budget = SukiBudget(max_tokens=int(os.getenv("MAX_DAILY_TOKENS", 500000)))
        
        # 🧠 [Operation Eternal Soul] 初始化長期記憶
        self.memory = MemoryManager()
        
        # 🧬 [Suki DNA State]
        self.temp_toxicity_override = None

        # 📊 [Session Mood] 今日被呼喚次數，重啟歸零
        self._session_call_count = 0

        self.model_name = os.getenv("LLM_PRIMARY_MODEL", os.getenv("LLM_MODEL", "gemma-4-31b-it"))
        
        self.prompt_manager = PromptManager()
        self.is_exhausted = False # 🛡️ [Tier 1 Hard Limit Flag]
        self.last_exhausted_reset = time.time()
        self.last_slow_summary = ""
        self.short_term_dialogue = []
        
        # 🔔 [Notification System]
        self.on_fallback_callback = None # 由 Discord Cog 注入：async def callback(tier_name, model_name)
        self.current_tier = "Tier-1"      # 追蹤當前啟動的模型層級
        
        # 🚀 [Groq TPM Guard]
        self.groq_cleaner_usage = [] # list of (timestamp, token_count)

        # 📊 [Phase 2] Multi-Signal Wake Fusion
        self.wake_fusion = WakeDetector()

        # 🌡️ [AtmosphereTracker] 即時讀空氣模組
        from marvin_voice_core.atmosphere_tracker import AtmosphereTracker
        self.atmosphere_tracker = AtmosphereTracker(memory_manager=self.memory)

        # 🚀 [Phase 3] Speculative Prefetch — speaker → asyncio.Task[str]
        self._pending_prefetch: dict = {}
        self._prefetch_attempts: int = 0
        self._prefetch_hits: int = 0
        self._prefetch_semaphore = asyncio.BoundedSemaphore(3)  # 最多 3 個並發 prefetch

        # 🔍 [Background Intent Enrich] speaker → {timestamp, query, results}
        # 喚醒後背景 DDG，結果快取 5 分鐘供下次同玩家回應使用
        self._intent_search_cache: dict = {}

        # 🎭 [Greeting & Farewell Cache]
        self._greeting_cache = {}  # {player_name: (timestamp, msg)}
        self._farewell_cache = {}  # {player_name: (timestamp, msg)}
        self.last_stream_error_time = 0 # 🛡️ [Throttling] 防止故障時瘋狂輸出重複台詞

        # 🛡️ [RPM Guard] 防止超過 Gemini 免費層 15 RPM 限制
        self._cloud_rpm_window = []       # 主 API 近 60 秒的呼叫時間戳
        self._CLOUD_RPM_LIMIT = 12        # 保留 3 次 buffer（免費層上限 15）
        self._gemini_cleaner_window = []  # STT 清洗 API 近 60 秒的呼叫時間戳
        self._CLEANER_RPM_LIMIT = 12

        # 🧠 [Context Injector] 預設關閉，由外部 setup 時啟用
        from context_injector import ContextInjector
        self._context_injector: ContextInjector | None = None
        # guild_id 由外部（VoiceController）在 summon 時注入，預設 0
        self.guild_id: int = 0

    # ── LLMClient Protocol ────────────────────────────────────────────────────

    async def complete(
        self,
        system: str,
        user: str,
        *,
        is_json: bool = False,
        temperature: float | None = None,
    ) -> str:
        """LLMClient Protocol: single blocking call through the tier fallback chain."""
        return await self._call_llm(system, user, is_json=is_json, temperature=temperature)

    async def stream_text(self, system: str, user: str, *, temperature: float | None = None):
        """LLMClient Protocol: streaming call (async generator of str chunks)."""
        async for chunk in self._stream_cloud(system, user, temperature=temperature):
            yield chunk
