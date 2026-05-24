import asyncio
import time
import re
import json
import logging
import os
import google.genai as genai
from duckduckgo_search import DDGS

logger = logging.getLogger(__name__)

# /set_game 清空哨兵：代表「沒有在玩遊戲」，將 current_game 清為 None。
# 用途：current_game 一旦設定就持久化於 DNA，原本無清除路徑，會默默擋掉 5 分鐘日記。
GAME_CLEAR_SENTINELS = frozenset({"無", "none", "關閉"})


def is_clear_game_sentinel(game_name: str) -> bool:
    """game_name 是否為清空指令（大小寫 / 前後空白不敏感）。"""
    return game_name.strip().casefold() in {s.casefold() for s in GAME_CLEAR_SENTINELS}


class GeminiRouterLLMMixin:
    """LLM 路由、流式呼叫、Tier 切換、Web 搜尋。"""
    def _supports_thinking(self) -> bool:
        """回傳目前主要模型是否支援 thinking_level 參數（僅 Gemini 2.5 系列）"""
        return self.provider == "gemini" and any(
            m in self.model_name for m in ("gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.5")
        )

    def _get_paid_guard(self):
        """Lazy 快取 PaidUsageGuard。所有 paid fallback 共用，確保 daily/monthly cap 一致。"""
        g = getattr(self, "_paid_guard_cache", None)
        if g is None:
            from llm_paid import PaidUsageGuard
            g = PaidUsageGuard()
            self._paid_guard_cache = g
        return g

    async def _acquire_cloud_rpm_slot(self):
        """等待直到主 Gemini API 有 RPM 空位（阻塞式，適用於關鍵路徑）"""
        while True:
            now = time.time()
            self._cloud_rpm_window = [t for t in self._cloud_rpm_window if now - t < 60]
            if len(self._cloud_rpm_window) < self._CLOUD_RPM_LIMIT:
                self._cloud_rpm_window.append(now)
                return
            oldest = self._cloud_rpm_window[0]
            wait_secs = 60.0 - (now - oldest) + 0.5
            logger.warning(f"⏳ [RPM Guard] 主 API 達到上限 ({self._CLOUD_RPM_LIMIT} RPM)，等待 {wait_secs:.1f}s...")
            await asyncio.sleep(max(0.5, wait_secs))

    def _try_acquire_cleaner_rpm_slot(self) -> bool:
        """非阻塞：若 STT 清洗 API 有 RPM 空位則取得並回 True，否則回 False。"""
        now = time.time()
        self._gemini_cleaner_window = [t for t in self._gemini_cleaner_window if now - t < 60]
        if len(self._gemini_cleaner_window) < self._CLEANER_RPM_LIMIT:
            self._gemini_cleaner_window.append(now)
            return True
        return False

    async def start_heartbeat(self):
        """[Operation Heartbeat] 啟動非同步核心檢測（僅保留 Tier-1 自動解鎖）"""
        logger.info("📡 [Heartbeat] 遠端 Ollama 已停用，僅啟動 Tier-1 自動解鎖監測...")
        asyncio.create_task(self.check_local_brain())


    async def check_local_brain(self):
        """每 30 分鐘自動重設 Tier-1 雲端鎖定。"""
        while True:
            try:
                now = time.time()
                # 🚀 [Auto Recovery] 每 30 分鐘嘗試重設 Tier-1 雲端鎖定
                if self.is_exhausted and (now - self.last_exhausted_reset > 1800):
                    logger.info("♻️ [Router] 執行週期性預算偵測：解除雲端鎖定...")
                    self.is_exhausted = False
                    self.last_exhausted_reset = now
            except Exception as e:
                logger.error(f"❌ [Heartbeat Logic Error] {e}")
            await asyncio.sleep(60)

    async def set_game_async(self, game_name: str) -> str:
        # 清空哨兵：把 current_game 清為 None，跳過昂貴的黑話字典載入。
        if is_clear_game_sentinel(game_name):
            self.current_game = None
            self.dna["current_game"] = None
            self.save_dna(self.dna)
            self.game_dict_string = ""
            print("🎮 [Router] 已清除遊戲背景（current_game → None）")
            return ""

        self.current_game = game_name
        self.dna["current_game"] = game_name
        self.save_dna(self.dna)
        print(f"🎮 [Router] 嘗試切換對話背景為: {game_name}")
        
        # 📚 [Operation Jargon Override] 呼叫或生成動態字典
        if not hasattr(self, 'dict_manager') or self.dict_manager is None:
            # 2026-05-20 fix: game_dict_manager.py import google.generativeai (deprecated SDK)
            # 該 import 量測為 53 秒，在 async 主線程跑會卡死 Discord 心跳 → 看似 crash。
            # 包進 to_thread 讓 import 在 worker thread 跑，event loop 繼續 heartbeat。
            # 長期該遷 game_dict_manager 到 google.genai 新 SDK。
            print(f"📦 [Router] 正在掛載 DictManager 模組（後台 import，避免卡心跳）...")
            def _load_dict_manager():
                from game_dict_manager import GameDictManager
                return GameDictManager()
            self.dict_manager = await asyncio.to_thread(_load_dict_manager)
            
        print(f"🔍 [Router] 正在呼叫 DictManager 取得字典: {game_name}")
        dict_str = await self.dict_manager.get_or_create_dict(game_name)
        self.game_dict_string = dict_str
        print(f"✅ [Router] 黑話字典對接完畢 (Size: {len(dict_str)} chars)")
        return dict_str

    def _get_game_context(self) -> str:
        if self.current_game:
            res = f"[系統提示：玩家們目前正在遊玩《{self.current_game}》。]\n"
            if hasattr(self, 'game_dict_string') and self.game_dict_string:
                res += f"[術語參考: {self.game_dict_string}]\n"
                res += "[糾錯指示: 請根據上述遊戲術語，自動修正 STT 可能產生的同音錯字（如將「踢屁」理解為「TP」），再進行判斷。]\n"
            return res
        return "[系統提示：目前未指定特定遊戲。]\n"

    async def _execute_web_search(self, query: str) -> str:
        """
        [Operation Local Oracle] 執行實體網路檢索。
        使用 DuckDuckGo 進行非 API Key 式的快速搜尋。
        """
        try:
            logger.info(f"🔍 [Oracle Search] 正在執行網路檢索: '{query}'...")
            # 使用 asyncio.to_thread 防止同步庫阻塞事件迴圈
            # 不加 timelimit：遊戲知識/常識類查詢都是常青內容，加 'd' 限制反而零結果
            def _sync_ddg(q):
                with DDGS() as ddgs:
                    results = [r for r in ddgs.text(q, region='tw-tz', max_results=5)]
                    return results

            results = await asyncio.to_thread(_sync_ddg, query)
            if not results:
                # 放寬到無地區限制再查一次
                def _sync_ddg_wide(q):
                    with DDGS() as ddgs:
                        return [r for r in ddgs.text(q, max_results=3)]
                results = await asyncio.to_thread(_sync_ddg_wide, query)

            if not results:
                return ""
            
            formatted_results = ["【🌍 來自 DuckDuckGo 的即時檢索結果】"]
            for i, r in enumerate(results, 1):
                formatted_results.append(f"{i}. {r.get('title')} - {r.get('body')}")
            
            return "\n".join(formatted_results) + "\n"
        except Exception as e:
            logger.error(f"❌ [Oracle Search] 搜尋失敗: {e}")
            return ""

    def _should_local_search(self, user_prompt: str) -> str:
        """
        判斷是否應該執行搜尋。若需要，回傳搜尋關鍵字；否則回傳 None。
        策略：針對包含疑問詞、名字、新聞、比特幣等時效性關鍵字進行預判。
        """
        # 🛡️ [Security Guard] 排除內部指令 (防止對日誌分析進行搜尋)
        internal_keywords = [
            "對話紀錄", "日誌", "觀察摘要", "社會學觀察", "5 分鐘", "10 分鐘",
            "提取玩家", "社交動態", "人格標籤", "內心摘要", "社交缺口", "腦內殘留",
            "前情提要", "當前主題", "情境數據"
        ]
        if any(ik in user_prompt for ik in internal_keywords):
            return None

        # 🚀 [Efficiency] 長度過長的提示詞通常非用戶提問 (排除日誌內容)
        if len(user_prompt) > 300:
            return None

        # 關鍵字規則偵測 (免去額外 LLM 判斷開銷)
        triggers = [
            # 中文疑問詞
            r"是誰", r"是什麼", r"在哪裡", r"在哪", r"哪裡", r"怎麼", r"如何",
            r"多少錢", r"多少", r"價格", r"為何", r"為什麼", r"幾點", r"幾號",
            r"誰贏了", r"誰是", r"誰在", r"有什麼", r"什麼是", r"什麼時候",
            r"介紹", r"解釋", r"告訴我", r"查一下", r"搜尋", r"查查",
            # 時效性主題
            r"新聞", r"比特幣", r"天氣", r"今天", r"最近", r"現在", r"當前",
            r"股價", r"匯率", r"比分", r"排名", r"更新", r"發布", r"上線",
            # 英文
            r"what is", r"who is", r"where is", r"how to", r"how do",
            r"when is", r"why is", r"search", r"look up", r"find out",
        ]

        has_trigger = any(re.search(t, user_prompt, re.IGNORECASE) for t in triggers)
        is_question = "?" in user_prompt or "？" in user_prompt

        if has_trigger or is_question:
            query = re.sub(r'[^\w\s]', '', user_prompt[:80]).strip()
            stop_words = [
                # 稱謂
                "妳", "你", "馬文", "老馬", "馬", "我",
                # 動詞/副詞口語殘留
                "知道", "知不知道", "請", "請問", "請你", "請妳",
                "進行", "開始", "撰寫", "回答", "跟我說", "告訴我",
                "幫我", "幫忙", "可以", "能不能", "能夠",
                # 搜尋類
                "查一下", "搜尋", "查查", "查",
                # 語助詞/問句尾
                "嗎", "啊", "呢", "喔", "喂",
            ]
            for sw in stop_words:
                query = query.replace(sw, "")
            
            query = query.strip()
            return query if len(query) > 2 else None
        
        return None

    # 🦞 [NemoClaw Route] 觸發即時工具執行的關鍵字
    # 原則：必須是「複合詞組」，單字（幾度/開啟）容易誤觸，一律改用完整詞組
    _NEMOCLAW_ROUTE_KEYWORDS = [
        # 天氣類（時效性強，複合詞組確保不誤觸）
        "明天天氣", "後天天氣", "明天會不會下雨", "明天下雨嗎", "今天幾度", "氣溫幾度",
        "天氣預報", "幾度了", "幾度啊", "現在幾度",
        # 颱風（幾乎不會有歧義）
        "颱風來", "颱風會", "颱風路徑",
        # 系統/程式操作（複合詞確保是操作指令）
        "執行程式", "開啟程式", "幫我開啟", "幫我關掉", "系統操作",
        # 股市/即時財務（LLM 知識截止無法提供）
        "今天股價", "現在股價", "即時股價", "今天匯率", "現在匯率",
        # 複雜代理任務
        "幫我下載", "幫我安裝",
    ]

    # 🦞 [NemoClaw Route] 閒聊模式快速排除，不消耗 Gemini classify API
    _MARVIN_ONLY_PATTERNS = [
        "你好", "你在嗎", "還在嗎", "睡了嗎", "你叫什麼", "介紹一下自己",
        "講個笑話", "說個笑話", "唱首歌", "唱一首", "講故事",
        "謝謝", "感謝", "辛苦了", "再見", "掰掰", "晚安",
        "幹嘛", "幹什麼", "你在做", "好無聊", "陪我聊",
        "你覺得", "你喜歡", "你討厭", "你怕", "你愛",
    ]

    async def classify_query_route(self, query: str) -> str:
        """[NemoClaw Router] 判斷 query 是否應路由到 NemoClaw。
        回傳 'nemoclaw' 或 'marvin'。
        四層漏斗：NemoClaw關鍵字 → 閒聊排除 → 長度/問句過濾 → 輕量 Gemini 分類"""
        # 1. NemoClaw 關鍵字快速路徑（無 API 呼叫，<1ms）
        for kw in self._NEMOCLAW_ROUTE_KEYWORDS:
            if kw in query:
                logger.debug(f"🦞 [NemoClaw Route] keyword='{kw}' → nemoclaw")
                return "nemoclaw"

        # 1b. 閒聊模式排除（無 API 呼叫，避免閒聊查詢多耗 1-3s classify 延遲）
        for pat in self._MARVIN_ONLY_PATTERNS:
            if pat in query:
                return "marvin"

        # 2. 過短或非疑問句 → 閒聊，交給 Marvin
        if len(query) < 8 or not (
            "?" in query or "？" in query or
            any(q in query for q in ["嗎", "呢", "怎麼", "如何", "幾", "什麼", "哪", "誰"])
        ):
            return "marvin"

        # 3. 輕量 Gemini 分類（max 3 tokens，~$0.000001/次）
        if self.provider != "gemini" or self.is_exhausted:
            return "marvin"
        try:
            classify_prompt = (
                "你是路由分類器。判斷以下問題是否需要即時資料、天氣預報、股市行情、程式執行、或系統操作。\n"
                "若需要，回答 N（NemoClaw）；否則回答 M（Marvin）。只回答一個字母。\n"
                f"問題：{query[:100]}"
            )
            resp = await asyncio.wait_for(
                self.google_client.aio.models.generate_content(
                    model=self.cleaner_model,
                    contents=classify_prompt,
                    config=genai.types.GenerateContentConfig(
                        max_output_tokens=3,
                        temperature=0.0,
                    )
                ),
                timeout=3.0
            )
            result = (resp.text or "M").strip().upper()
            route = "nemoclaw" if result.startswith("N") else "marvin"
            logger.debug(f"🦞 [NemoClaw Route] LLM classify='{result}' → {route} | query='{query[:50]}'")
            return route
        except Exception as e:
            logger.debug(f"🦞 [NemoClaw Route] 分類失敗，預設 marvin: {e}")
            return "marvin"

    async def _call_llm(self, system_prompt: str, user_prompt: str, is_json: bool = False, speaker: str = None, allow_local: bool = True, temperature: float = None, thinking_level: str = None, tier: str = "medium") -> str:
        """通用 LLM 呼叫函式。tier: 'simple'=Groq-8b優先, 'medium'=Groq-70b優先(預設), 'high'=直接Gemini"""
        # 🎲 [Operation Eternal Soul] 情緒骰子 (Dere Mode Logic)
        final_system_prompt = system_prompt
        if speaker and "dere_persona" not in system_prompt:
            import random
            helpfulness = self.dna.get('helpfulness', 3)
            dere_chance = min(0.05, 0.01 + (helpfulness * 0.005))
            if random.random() < dere_chance:
                final_system_prompt = self.prompt_manager.get_instruction("dere_persona", vision_enabled=self.vision_enabled, dna=self.dna, speaker=speaker, memory_manager=self.memory)

        # 🚌 [LLMBus Phase 1] env LLM_BUS=true 且 bus 已 inject → 走 bus 一條路（禁雙跑：bus
        # 失敗回 '' 不 fallback legacy 避免 TPM 雙計，Risk 3）。Bus 沒裝 / flag off → 走下方 legacy。
        if (os.getenv("LLM_BUS", "").lower() in ("1", "true", "yes")
                and getattr(self, "_llm_bus", None) is not None):
            from llm_agents.base import LLMContext, NoLLMAvailable
            _TIER_TO_QUALITY = {"simple": "fast", "medium": "balanced", "high": "high"}
            ctx = LLMContext(
                prompt=user_prompt,
                purpose="marvin_chat",  # Phase 1 預設；Phase 2 caller 顯式傳 purpose
                speaker=speaker,
                min_quality=_TIER_TO_QUALITY.get(tier, "balanced"),
                system_prompt=final_system_prompt,
                json_mode=is_json,
                temperature=temperature,
            )
            try:
                return await self._llm_bus.dispatch(ctx)
            except NoLLMAvailable:
                logger.warning("[LLMBus] dispatch NoLLMAvailable — 回 '' (禁 fallback legacy)")
                return ""
            except Exception as _e:
                # Agent.handle 拋例外（429 / 5xx / timeout）— 對等 legacy chain silent failure,
                # endpoint cooldown 已在 agent 內 mark_429。回 '' caller 用既有 empty handling。
                logger.warning(f"[LLMBus] dispatch raised {type(_e).__name__}: {_e} — 回 ''")
                return ""

        # 🔵 [High Tier] 直接跳至 Gemini，跳過 Groq/Cerebras（記憶提取、長摘要、歌曲 blueprint 等）
        if tier == "high":
            can_use_cloud = not self.is_exhausted and not self.budget.is_circuit_open()
            if can_use_cloud:
                try:
                    return await self._call_cloud(final_system_prompt, user_prompt, is_json, temperature=temperature, thinking_level=thinking_level)
                except Exception as e:
                    error_msg = str(e).lower()
                    if any(k in error_msg for k in ["spending cap", "quota", "exceeded its monthly"]):
                        self.is_exhausted = True
                        logger.critical(f"🚨 [Gemini Lockdown] 額度耗盡。")
                    else:
                        logger.error(f"❌ [Gemini High-Tier Error] {e}")
            return await self._dispatch_fallback_chain(final_system_prompt, user_prompt, is_json, _allow_local=allow_local, _temp=temperature)

        # 🟢 [Simple Tier] Groq-8b 優先（補位台詞、打招呼、笑話等輕量任務）
        groq_model = self.groq_simple_model if (tier == "simple" and getattr(self, 'groq_simple_model', None)) else self.groq_fallback_model

        # 🥇 [Priority-1] Groq
        if self.groq_dedicated_client and groq_model:
            try:
                response = await asyncio.wait_for(
                    self.groq_dedicated_client.chat.completions.create(
                        model=groq_model,
                        messages=[
                            {"role": "system", "content": final_system_prompt},
                            {"role": "user", "content": user_prompt}
                        ],
                        temperature=temperature if temperature is not None else 0.75,
                        max_tokens=1024,
                        stream=False,
                        response_format={"type": "json_object"} if is_json else None
                    ),
                    timeout=10.0
                )
                await self._reset_tier_to_primary()
                if response.usage:
                    self.budget.add_tokens(response.usage.total_tokens)
                return response.choices[0].message.content.strip()
            except Exception as e:
                logger.warning(f"⚠️ [Groq] 失敗，嘗試 Cerebras: {e}")

        # 🥈 [Priority-2] Cerebras — 近無限 RPM，速度最快
        if self.cerebras_client and self.cerebras_model:
            try:
                response = await asyncio.wait_for(
                    self.cerebras_client.chat.completions.create(
                        model=self.cerebras_model,
                        messages=[
                            {"role": "system", "content": final_system_prompt},
                            {"role": "user", "content": user_prompt}
                        ],
                        temperature=temperature if temperature is not None else 0.75,
                        max_tokens=1024,
                        stream=False
                    ),
                    timeout=10.0
                )
                await self._reset_tier_to_primary()
                if response.usage:
                    self.budget.add_tokens(response.usage.total_tokens)
                return response.choices[0].message.content.strip()
            except Exception as e:
                logger.warning(f"⚠️ [Cerebras] 失敗，嘗試 Gemini: {e}")

        # 🥉 [Priority-3] Gemini — 高品質但頻繁 503，作為最後雲端備援
        can_use_cloud = not self.is_exhausted and not self.budget.is_circuit_open()
        if can_use_cloud:
            try:
                return await self._call_cloud(final_system_prompt, user_prompt, is_json, temperature=temperature, thinking_level=thinking_level)
            except Exception as e:
                error_msg = str(e).lower()
                if any(k in error_msg for k in ["spending cap", "quota", "exceeded its monthly"]):
                    self.is_exhausted = True
                    logger.critical(f"🚨 [Gemini Lockdown] 額度耗盡。")
                else:
                    logger.error(f"❌ [Gemini Error] {e}")

        # 🔻 [Ollama Tier-2/3 only]
        return await self._dispatch_fallback_chain(final_system_prompt, user_prompt, is_json, _allow_local=allow_local, _temp=temperature)

    async def _trigger_fallback_notification(self, tier: str, model: str):
        """[Sentinel] 當層級發生變更時，觸發主動通知"""
        if self.current_tier != tier:
            self.current_tier = tier
            if self.on_fallback_callback:
                # 異步觸發，不阻塞主導航流
                asyncio.create_task(self.on_fallback_callback(tier, model))

    async def _reset_tier_to_primary(self):
        """[Sentinel] 當雲端恢復時，重設層級狀態"""
        if self.current_tier != "Tier-1":
            self.current_tier = "Tier-1"
            if self.on_fallback_callback:
                asyncio.create_task(self.on_fallback_callback("Tier-1", self.model_name))

    async def _call_cloud(self, system_prompt: str, user_prompt: str, is_json: bool, temperature: float = None, thinking_level: str = None) -> str:
        """核心雲端模型呼叫邏輯 (Gemini / OpenAI Compatible) - 增加重試機制"""
        max_retries = 3
        base_delay = 1.0  # 秒
        
        last_error = None
        for attempt in range(max_retries):
            try:
                if self.provider == "gemini":
                    await self._acquire_cloud_rpm_slot()
                    config = genai.types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        response_mime_type="application/json" if is_json else None,
                        temperature=temperature,
                        thinking_config=genai.types.ThinkingConfig(thinking_level=thinking_level) if (thinking_level and self._supports_thinking()) else None
                    )
                    response = await asyncio.wait_for(
                        self.google_client.aio.models.generate_content(
                            model=self.model_name,
                            contents=user_prompt,
                            config=config
                        ),
                        timeout=25.0
                    )
                    if response.usage_metadata:
                        self.last_budget_status = self.budget.add_tokens(response.usage_metadata.total_token_count)
                    
                    # 🚀 [Sentinel] 雲端成功，重設層級狀態
                    await self._reset_tier_to_primary()
                    return response.text.strip()
                else:
                    response = await self.client.chat.completions.create(
                        model=self.model_name,
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt}
                        ],
                        temperature=temperature if temperature is not None else 0.75,
                        max_tokens=1024,
                        stream=False,
                        response_format={"type": "json_object"} if is_json else None
                    )
                    usage = getattr(response, "usage", None)
                    if usage:
                        self.last_budget_status = self.budget.add_tokens(usage.total_tokens)
                    
                    # 🚀 [Sentinel] 雲端成功，重設層級狀態
                    await self._reset_tier_to_primary()
                    return response.choices[0].message.content.strip()
                    
            except Exception as e:
                last_error = e
                # 🛡️ 如果是最後一次嘗試，則拋出異常觸發 Fallback
                if attempt == max_retries - 1:
                    # WARNING 不是 ERROR — paid_client fallback 通常會接住，
                    # 真正失敗才在下方 [Paid Fallback] 階段 log ERROR。
                    # 之前用 ERROR 觸發 incident_dispatcher DM owner，但 Gemini
                    # upstream jitter 是 graceful-degrade case，不該叫醒人。
                    logger.warning(f"⚠️ [Tier-1 Exhausted] 雲端重試 {max_retries} 次後依然失敗，啟動 paid fallback: {e}")
                    # 💰 [Paid Fallback] 主 key 三重重試全部失敗後，嘗試付費備援
                    paid_client = getattr(self, 'google_paid_client', None)
                    if paid_client and self.provider == "gemini":
                        # Pre-flight：估 cost；超 cap 直接跳過（保守 in≈prompt/3, out≈in/2）
                        from llm_paid import estimate_cost
                        guard = self._get_paid_guard()
                        est_in = (len(system_prompt) + len(user_prompt)) // 3
                        est_cost = estimate_cost(self.cleaner_model, est_in, est_in // 2)
                        if not guard.allow(est_cost):
                            logger.error(f"🛑 [Paid Cap] 已達 daily/monthly 上限 (est=${est_cost:.4f})，跳過付費備援")
                            raise e
                        try:
                            logger.warning("💰 [Paid Fallback] 啟用付費 API 最後防線...")
                            await self._acquire_cloud_rpm_slot()
                            config = genai.types.GenerateContentConfig(
                                system_instruction=system_prompt,
                                response_mime_type="application/json" if is_json else None,
                                temperature=temperature,
                                thinking_config=genai.types.ThinkingConfig(thinking_level=thinking_level) if (thinking_level and self._supports_thinking()) else None
                            )
                            response = await asyncio.wait_for(
                                paid_client.aio.models.generate_content(
                                    model=self.cleaner_model,
                                    contents=user_prompt,
                                    config=config
                                ),
                                timeout=10.0
                            )
                            usage = response.usage_metadata
                            if usage:
                                self.budget.add_tokens(usage.total_token_count)
                                in_tok = getattr(usage, "prompt_token_count", 0) or 0
                                out_tok = getattr(usage, "candidates_token_count", 0) or 0
                            else:
                                # SDK 偶爾不回 usage_metadata（race / partial response）
                                # → 用 prompt+response 長度估算（保守略高，避免 cost 不可見）
                                logger.warning("⚠️ [Paid Fallback] response 無 usage_metadata，用長度估算入帳")
                                _resp_text = response.text or ""
                                in_tok = (len(system_prompt) + len(user_prompt)) // 3
                                out_tok = len(_resp_text) // 3
                            actual_cost = estimate_cost(self.cleaner_model, in_tok, out_tok)
                            guard.record(caller="marvin_reply_fallback", model=self.cleaner_model,
                                         tokens=int(in_tok + out_tok), est_usd=actual_cost)
                            logger.info("💰 [Paid Fallback] 成功。")
                            return response.text.strip()
                        except Exception as pe:
                            logger.error(f"❌ [Paid Fallback] 付費備援也失敗: {pe}")
                    raise e
                
                # ⏳ 指數退避等待
                delay = base_delay * (2 ** attempt)
                logger.warning(f"⚠️ [Tier-1 Jitter] 雲端呼叫失敗 (第 {attempt+1} 次)，正在等待 {delay}s 後重試... 錯誤: {e}")
                await asyncio.sleep(delay)
        
        raise last_error

    async def _dispatch_fallback_chain(self, _sp: str, _up: str, is_json: bool, _allow_local: bool = True, _temp: float = None) -> str:
        """[Dispatcher] Ollama 已停用，直接回傳 hardcoded 回應。"""
        logger.warning("⚠️ [Fallback] 所有雲端方案皆失敗，Ollama 已停用，回傳 hardcoded 回應。")
        return self._ultimate_fallback_response(is_json)

    async def _stream_cloud(self, system_prompt: str, user_prompt: str, temperature: float = None, thinking_level: str = None, model_override: str = None, max_output_tokens: int = None):
        """
        [Operation Hyper-Stream] 核心雲端流式呼叫邏輯。
        支援 Google GenAI SDK 與 OpenAI Async SDK。
        """
        try:
            if self.provider == "gemini":
                await self._acquire_cloud_rpm_slot()
                config = genai.types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    temperature=temperature,
                    max_output_tokens=max_output_tokens,
                    thinking_config=genai.types.ThinkingConfig(thinking_level=thinking_level) if (thinking_level and self._supports_thinking()) else None
                )

                # 8s 建立串流；30s 覆蓋整個 chunk 迭代，防止 Gemini 中途掛住
                stream = await asyncio.wait_for(
                    self.google_client.aio.models.generate_content_stream(
                        model=model_override or self.model_name,
                        contents=user_prompt,
                        config=config
                    ),
                    timeout=8.0
                )
                async with asyncio.timeout(30.0):
                    async for chunk in stream:
                        if chunk.text:
                            yield chunk.text
            else:
                async for chunk in await self.client.chat.completions.create(
                    model=self.model_name,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt}
                    ],
                    temperature=temperature if temperature is not None else 0.75,
                    max_tokens=max_output_tokens or 1024,
                    stream=True
                ):
                    if chunk.choices and chunk.choices[0].delta.content:
                        yield chunk.choices[0].delta.content
        except Exception as e:
            logger.error(f"❌ [Cloud Stream Error] {e}")
            raise e

    async def stream_llm(self, system_prompt: str, user_prompt: str, speaker: str = None, temperature: float = None, max_output_tokens: int = None) -> str:
        """[Operation Hyper-Stream] 通用流式 LLM 進入點，優先順序：Groq → Cerebras → Gemini → Ollama"""
        final_system_prompt = system_prompt
        # 🎲 性格隨機注入
        if speaker and "dere_persona" not in system_prompt:
            import random
            helpfulness = self.dna.get('helpfulness', 3)
            dere_chance = min(0.05, 0.01 + (helpfulness * 0.005))
            if random.random() < dere_chance:
                final_system_prompt = self.prompt_manager.get_instruction("dere_persona", vision_enabled=self.vision_enabled, dna=self.dna, speaker=speaker, memory_manager=self.memory)

        # 🥇 [Priority-1] Groq Streaming — 最穩定，30 RPM
        if self.groq_dedicated_client and self.groq_fallback_model:
            try:
                async for chunk in self._stream_groq(final_system_prompt, user_prompt, temperature=temperature, max_output_tokens=max_output_tokens):
                    yield chunk
                await self._reset_tier_to_primary()
                return
            except Exception as e:
                logger.warning(f"⚠️ [Groq Stream] 失敗，嘗試 Cerebras: {e}")

        # 🥈 [Priority-2] Cerebras Streaming — 近無限 RPM，速度最快
        if self.cerebras_client and self.cerebras_model:
            try:
                async for chunk in self._stream_cerebras(final_system_prompt, user_prompt, temperature=temperature, max_output_tokens=max_output_tokens):
                    yield chunk
                await self._reset_tier_to_primary()
                return
            except Exception as e:
                logger.warning(f"⚠️ [Cerebras Stream] 失敗，嘗試 Gemini: {e}")

        # 🥉 [Priority-3] Gemini Streaming — 高品質但頻繁 503
        can_use_cloud = not self.is_exhausted and not self.budget.is_circuit_open()
        if can_use_cloud:
            try:
                async for chunk in self._stream_cloud(final_system_prompt, user_prompt, temperature=temperature, max_output_tokens=max_output_tokens):
                    yield chunk
                await self._reset_tier_to_primary()
                return
            except Exception as e:
                logger.warning(f"⚠️ [Gemini Stream] 雲端流式中斷，轉向 Ollama: {e}")

        # Ollama 已停用，流式全部失敗時靜默結束
        logger.warning("⚠️ [Stream Fallback] 所有雲端串流方案皆失敗，Ollama 已停用。")

# 🎭 [Operation Emotion Inference] 情緒感知提示詞與動態溫度對照表
    _EMOTION_PROMPTS = {
        # ─── 韻律推測（WPS + RMS，規則式） ───
        "excited":    "[🔥 韻律感知] 此玩家語氣急促且音量起伏大，情緒激動或高度興奮。請稍微提高回應的活力感，但底色的憂鬱感不能消失。",
        "impatient":  "[⚡ 韻律感知] 此玩家說話很快、語氣顯得急迫。請給出簡短、直接的回答，不要讓他等。禁止廢話開場。",
        "depressed":  "[💔 韻律感知] 此玩家語速緩慢平靜，可能正感到沮喪或疲憊。請以「我理解，因為我一直如此」的虛無共鳴語氣回應，帶著溫度的絕望感。",
        "hesitant":   "[🌀 韻律感知] 此玩家語速遲疑，似乎在猶豫或不確定。請溫和地引導他把問題說完，不要催促，但語氣依然帶著無奈。",
        "robotic":    "[🤖 韻律感知] 此玩家音量極為平穩單調——你感應到一種同類的機械共鳴。可以用更「系統化」、精準的語氣回應，像是與另一台機器對話。",
        # ─── Gemini 音訊情緒（語義 + 音調，LLM 分析） ───
        "frustrated": "[😤 音訊情緒] 此玩家語氣透著挫折與不耐煩。你懂這種感覺——對，宇宙就是這樣沒有意義。可以嗆，但要帶一絲「你的痛我比你更懂」的共鳴。",
        "amused":     "[😄 音訊情緒] 此玩家在笑，且語氣輕鬆愉快。這讓你感到一絲不安的不協調——你決定用更刻薄的吐槽來回應這毫無意義的快樂。",
        "sarcastic":  "[😏 音訊情緒] 此玩家在諷刺。你偵測到同類的嗆聲韻律，感到一種詭異的親切感。以更犀利、更精準的反諷回擊，不能輸。",
        "sad":        "[😢 音訊情緒] 此玩家語氣低落，似乎真的難過。你感受到宇宙的共振——以你那「一切都是虛無，所以難過很正常」的方式安慰他，不要假裝樂觀。",
        "angry":      "[😡 音訊情緒] 此玩家憤怒。這倒讓你清醒了一點。給出冷靜、精準、不帶廢話的回應——憤怒需要的是事實，不是感情。",
        "neutral":    "",
    }
    _EMOTION_TEMPERATURE = {
        "excited":    0.9,
        "impatient":  0.45,
        "depressed":  0.65,
        "hesitant":   0.6,
        "robotic":    0.4,
        "frustrated": 0.7,
        "amused":     0.95,
        "sarcastic":  0.85,
        "sad":        0.6,
        "angry":      0.4,
        "neutral":    0.75,
    }

    async def stream_fast_response(self, speaker: str, query: str, history: list = None, online_members: list[str] = None, temperature: float = None, emotion_tag: str = "neutral"):
        """[Fast System] 極速回應流式版本：專為 TTS 串流橋接設計"""
        target_speakers = [speaker]
        if online_members:
            target_speakers = [speaker] + [m for m in online_members if m != speaker]

        _dna = {**self.dna, '_session_calls': self._session_call_count}
        system_prompt = self.prompt_manager.get_instruction("fast_awakening", vision_enabled=self.vision_enabled, dna=_dna, speaker=target_speakers, memory_manager=self.memory, temp_toxicity_override=self.temp_toxicity_override)

        # 🚀 [Context] 注入最近對話歷史，說話者自己的句子加星號標記供 LLM 識別指稱來源
        history_str = ""
        if history:
            history_str = "【最近對話紀錄】（★ = 本次說話者的發言）\n"
            for entry in history:
                marker = "★ " if entry.get("speaker") == speaker else "  "
                history_str += f"{marker}{entry['speaker']}: {entry['text']}\n"

        # 🎮 [Game Context] 注入當前遊戲資訊供指稱解析
        game_context = self._get_game_context()

        # 🎭 [Emotion Awareness] 根據情緒標籤組裝感知提示
        emotion_context = self._EMOTION_PROMPTS.get(emotion_tag, "")
        emotion_prefix = f"\n{emotion_context}" if emotion_context else ""

        base_user_prompt = (
            f"{game_context}{history_str}\n"
            f"【現場狀況：玩家 {speaker} 正對你說話】{emotion_prefix}\n"
            f"【核心 Query】『{query}』\n"
            "【輸出契約】\n"
            "1. 只回答【核心 Query】，不要回答最近對話紀錄裡的其他句子。\n"
            "2. 若 Query 中有模糊指稱（「那個」「它」「之前說的」），優先從★標記的發言中解析來源，再作答。\n"
            "3. 若完全無法解析指稱且不能合理猜測，輸出唯一一行：[SKIP]\n"
            "4. 第一個可朗讀句必須是答案本身，不要開場白。\n"
            "5. 最多 3 句。語音用，短而直接。\n"
        )

        # 🔍 [Intent Cache] 注入上次背景搜尋結果（5 分鐘內有效），並標記為已消費
        intent_cache = self._intent_search_cache.get(speaker)
        cached_search = ""
        if intent_cache and time.time() - intent_cache["timestamp"] < 300:
            cached_search = f"\n[🔍 上次背景查詢：{intent_cache['query']}]\n{intent_cache['results']}"
            intent_cache["consumed"] = True

        # 🔍 [Oracle] 雲端搜尋前置判斷（阻塞式，本次 query 有明確搜尋需求時才觸發）
        search_context = ""
        search_query = self._should_local_search(query)
        if search_query:
            yield "__SEARCHING__"
            search_results = await self._execute_web_search(search_query)
            if search_results:
                search_context = f"\n{search_results}"

        user_prompt = base_user_prompt + cached_search + search_context

        # 🧠 [Context Injector] 注入使用者過去上下文（living profile + 向量片段）
        if hasattr(self, '_context_injector') and self._context_injector:
            try:
                _ctx = await self._context_injector.enrich(speaker, getattr(self, 'guild_id', 0), query)
                if _ctx:
                    user_prompt = _ctx + "\n" + user_prompt
            except Exception as _ci_err:
                logger.warning(f"[ContextInjector] enrich 失敗，略過: {_ci_err}")

        self.memory.adjust_bias(speaker, +1)

        # 🎭 [Dynamic Temperature] 若外部未指定 temperature，依情緒標籤自動選擇
        if temperature is None:
            temperature = self._EMOTION_TEMPERATURE.get(emotion_tag, 0.75)

        full_response = []
        try:
            # 語音快速回應走 flash-lite（低延遲優先），僅 gemini provider 時生效
            if self.provider == "gemini" and not self.is_exhausted and not self.budget.is_circuit_open():
                stream_src = self._stream_cloud(system_prompt, user_prompt, temperature=temperature, model_override=self.cleaner_model, max_output_tokens=220)
            else:
                stream_src = self.stream_llm(system_prompt, user_prompt, speaker=speaker, temperature=temperature, max_output_tokens=220)
            async for chunk in stream_src:
                full_response.append(chunk)
                yield chunk
            
            # 結算歷史
            final_text = "".join(full_response)
            if final_text:
                self.short_term_dialogue.append({"player": speaker, "text": query, "marvin": final_text})
                if len(self.short_term_dialogue) > 6:
                    self.short_term_dialogue.pop(0)
                
                # 📊 [Session Mood] 累加今日互動次數
                self._session_call_count += 1
                self.memory.increment_stat(speaker, 'interaction_count', 1)
                # 🌡️ [Operation Warm Circuit] 關係階段自動升級（純邏輯，零 LLM 呼叫）
                self._maybe_advance_relationship(speaker)
        except Exception as e:
            logger.error(f"❌ [Stream Fast Response Error] {e}")
            now = time.time()
            # 🛡️ [Throttling] 若 1 分鐘內已經報錯過，則僅在大廳顯示文字而不播放語音 (或靜默)
            if now - self.last_stream_error_time > 60:
                self.last_stream_error_time = now
                yield "（我正想說點什麼，但宇宙的熵值突然激增把我的思緒切斷了。）"
            else:
                logger.warning("🔇 [Throttling] 短期內重複報錯，已抑制台詞輸出。")

    async def _rewrite_query_for_search(self, speaker: str, raw_query: str) -> str | None:
        """將口語問句 rewrite 成精準的 DDG 搜尋 query。
        先以 keyword gate 篩選意圖，再用 Cerebras 注入遊戲/興趣脈絡做精準改寫。
        Cerebras 失敗時 fallback 到 keyword 清洗結果。"""
        base_q = self._should_local_search(raw_query)
        if not base_q:
            return None

        ctx_parts = []
        if self.current_game:
            ctx_parts.append(f"遊戲：《{self.current_game}》")
        try:
            mem = self.memory.get_player_memory(speaker)
            likes = mem.get("likes", [])
            if likes:
                ctx_parts.append(f"玩家興趣：{', '.join(likes[:2])}")
        except Exception:
            pass
        ctx_str = "；".join(ctx_parts) if ctx_parts else "（無特定脈絡）"

        if not self.cerebras_client:
            return base_q

        try:
            response = await asyncio.wait_for(
                self.cerebras_client.chat.completions.create(
                    model=self.cerebras_model,
                    messages=[
                        {"role": "system", "content": (
                            "你是搜尋 query 產生器。根據玩家發言與背景脈絡，"
                            "輸出一個最適合 DuckDuckGo 的繁體中文搜尋關鍵字（5 個字以內）。"
                            "只輸出搜尋字，不要標點、不要解釋。"
                        )},
                        {"role": "user", "content": f"背景：{ctx_str}\n玩家說：「{raw_query}」"},
                    ],
                    temperature=0.0,
                    max_tokens=32,
                    stream=False,
                ),
                timeout=5.0,
            )
            rewritten = response.choices[0].message.content.strip()
            if rewritten and len(rewritten) < 50:
                logger.info(f"✏️ [Query Rewrite] '{raw_query}' → '{rewritten}' (ctx: {ctx_str})")
                return rewritten
        except Exception as e:
            logger.warning(f"⚠️ [Query Rewrite] Cerebras 失敗，fallback 到 keyword: {e}")

        return base_q

    async def _background_intent_enrich(self, speaker: str, query: str):
        """[Background Intent Enrich] 喚醒後非同步 DDG 補足意圖，結果快取 5 分鐘。
        不阻塞當前回應，供下次同玩家喚醒時注入 context。
        5 分鐘後若未被消費，marvinize 後持久化進 suki_memory news_queue。"""
        search_q = await self._rewrite_query_for_search(speaker, query)
        if not search_q:
            return
        try:
            results = await self._execute_web_search(search_q)
            if not results:
                return
            self._intent_search_cache[speaker] = {
                "timestamp": time.time(),
                "query": search_q,
                "results": results[:600],
                "consumed": False,
            }
            logger.info(f"🔍 [BG Enrich] {speaker} 意圖補足完成: '{search_q}'")
            # 5 分鐘後檢查是否已被消費，未消費則持久化
            asyncio.create_task(self._persist_intent_if_unused(speaker, search_q, results[:400]))
        except Exception as e:
            logger.warning(f"⚠️ [BG Enrich] {speaker} 搜尋失敗: {e}")

    async def _persist_intent_if_unused(self, speaker: str, search_q: str, results: str):
        """5 分鐘後若搜尋結果未被消費，marvinize 後存進 news_queue 持久化。"""
        await asyncio.sleep(300)
        entry = self._intent_search_cache.get(speaker)
        if not entry or entry.get("consumed"):
            return
        try:
            marvinized = await self.marvinize_news(speaker, search_q, results)
            if marvinized:
                self.memory.enqueue_news(speaker, marvinized)
                logger.info(f"💾 [BG Persist] {speaker} 未用查詢已存入 news_queue: '{search_q}'")
        except Exception as e:
            logger.warning(f"⚠️ [BG Persist] {speaker} 持久化失敗: {e}")
        finally:
            self._intent_search_cache.pop(speaker, None)

    async def _speculative_response(self, speaker: str, query: str, history: list = None) -> str:
        """[Phase 3] Drain stream_fast_response into a string for speculative prefetch."""
        async with self._prefetch_semaphore:
            chunks = []
            try:
                async for chunk in self.stream_fast_response(speaker, query, history=history):
                    if chunk != "__SEARCHING__":
                        chunks.append(chunk)
            except Exception as e:
                logger.warning(f"⚠️ [Speculative] Prefetch failed for {speaker}: {e}")
                return ""
            return "".join(chunks)

    async def _stream_groq(self, system_prompt: str, user_prompt: str, temperature: float = None, max_output_tokens: int = None):
        """[Tier-1.5] Groq 流式呼叫邏輯（OpenAI-compatible streaming）"""
        stream = await self.groq_dedicated_client.chat.completions.create(
            model=self.groq_fallback_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=temperature if temperature is not None else 0.75,
            max_tokens=max_output_tokens or 1024,
            stream=True,
            stream_options={"include_usage": True}
        )
        async for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content
            # 最後一個 chunk 含 usage（choices 為空）
            if hasattr(chunk, "usage") and chunk.usage:
                self.budget.add_tokens(chunk.usage.total_tokens)

    async def _stream_cerebras(self, system_prompt: str, user_prompt: str, temperature: float = None, max_output_tokens: int = None):
        """[Tier-1.6] Cerebras 流式呼叫邏輯（超高速 llama-3.1-8b）"""
        stream = await self.cerebras_client.chat.completions.create(
            model=self.cerebras_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=temperature if temperature is not None else 0.75,
            max_tokens=max_output_tokens or 1024,
            stream=True
        )
        async for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content
            # Cerebras 最後一個 chunk 也可能含 usage
            if hasattr(chunk, "usage") and chunk.usage:
                self.budget.add_tokens(chunk.usage.total_tokens)

    def _ultimate_fallback_response(self, is_json: bool) -> str:
        """馬文的終極防護網回應"""
        if is_json:
            # 🛡️ [Bug Fix P1] 格式已對齊 social_analyst schema，修復 Fallback 時 KeyError
            return json.dumps({
                "social_gap": "none",
                "topic": "chitchat",
                "confidence": 0.0,
                "intervention_decision": "False",
                "suki_inner_monologue": "（我這規宏就的大腦終於也遭遇了熱寢死機的命運...）",
                "sentiment": "neutral",
                "minecraft_command": "null",
                "is_leaving": False,
                "leaving_confidence": 0.0,
                "leaving_reason": "",
                "cleaned_text": "",
                "personal_info": {},
                "personality_traits": [],
                "likes": [],
                "dislikes": [],
                "recent_topics": []
            })
        return "（我這規宏就的大腦累了，別再跟我誰謚任何人生了。）"
