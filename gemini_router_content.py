import asyncio
import re
import time
import random
import os
import json
import logging
from datetime import datetime
import suki_miner
from utils import safe_json_loads
from marvin_prompts import get_persona_modifiers
from personality_config import (
    adjust_axis,
    apply_character_preset,
    normalize_personality_state,
)
from google.genai import types

logger = logging.getLogger(__name__)


# rephrase_proactive_script 兜底 — LLM 把 task metadata 當回應 echo（5/27 6 筆嚴重 reaction）。
# Prefix「【改寫腳本】[：]?\n*」+ 末尾「(注意/留意：規則...)」括號註記都要 strip。
_REPHRASE_PREFIX_RE = re.compile(r"^【改寫腳本】[：:]?\s*", re.UNICODE)
_REPHRASE_TRAILING_META_RE = re.compile(
    r"\s*[(（](?:注意|留意)[：:][^()（）]*[)）]\s*$",
    re.UNICODE,
)


def _strip_rephraser_metadata(text: str) -> str:
    """剝掉 rephrase_proactive_script LLM 輸出常見的 metadata wrapping。

    保守原則：只 strip 字面 prefix「【改寫腳本】」+ 末尾「(注意/留意：...)」格式註記；
    馬文台詞本身的合法 inline 括號（如「（嘆氣）」）一律不動。
    """
    if not text:
        return text
    cleaned = _REPHRASE_PREFIX_RE.sub("", text, count=1)
    cleaned = _REPHRASE_TRAILING_META_RE.sub("", cleaned, count=1)
    return cleaned.strip()


_GREETING_CHARS_PER_PLAYER = 13   # 範圍 10-15，取中段；招呼要叫名字+一句吐槽


def greeting_char_budget(n_players: int | None) -> int:
    """進場招呼字數預算：依在場人數縮放（每人約 13 字），至少 1 人份。

    原本固定 60 字內，人多時每人被壓到 <10 字、叫不全名字。改成隨人數增加。
    """
    n = max(1, n_players or 0)
    return n * _GREETING_CHARS_PER_PLAYER


class GeminiRouterContentMixin:
    """內容生成：記憶萃取、社交分析、日記、問候、音樂藍圖等。"""
# --- 🧠 [Memory Extraction & Interaction] ---
    async def extract_memory(self, speaker: str, text: str):
        """非同步記憶萃取任務 (Operation Eternal Soul)
        [Operation Warm Circuit] 擴充支援 behavioral_patterns 語意考察。"""
        if not speaker or not text: return
        
        system_prompt = self.prompt_manager.get_instruction("memory_extractor", vision_enabled=self.vision_enabled, dna=self.dna, speaker=speaker, memory_manager=self.memory, temp_toxicity_override=self.temp_toxicity_override)
        user_prompt = (
            f"分析以下來自 {speaker} 的對話，提取任何可記錄資訊：\n『{text}』\n"
            f"輸出格式請包含 behavioral_patterns 欄位（口頭禪、常問問題、遊戲習慣等）。\n"
            f"範例：\"behavioral_patterns\": {{\"口頭禪\": \"不用這樣啦\", \"常問\": \"怎麼指令？\"}}"
        )
        
        try:
            raw_json = await self._call_llm(system_prompt, user_prompt, is_json=True, allow_local=False, tier="high")
            extracted_data = safe_json_loads(raw_json, {})
            
            if extracted_data:
                # 🌡️ [Warm Circuit] 分離處理 behavioral_patterns
                bp = extracted_data.pop("behavioral_patterns", {})
                if isinstance(bp, dict):
                    for k, v in bp.items():
                        if k and v:
                            self.memory.update_behavioral_pattern(speaker, str(k), str(v))
                self.memory.update_player_memory(speaker, extracted_data)
        except Exception as e:
            logger.error(f"🧠 [Memory] 提取失敗: {e}")

    async def generate_proactive_question(self, speaker: str, shared_context: str = None) -> str:
        """主動打破 cold場 (Operation Social Graph & Artificial Intimacy)"""
        import random
        
        # 🚀 [Birthday Special Logic] 優先檢查生日
        player_info = self.memory.get_player_memory(speaker).get("personal_info", {})
        birthday = player_info.get("birthday")
        today_str = datetime.now().strftime("%m-%d")
        
        if birthday == today_str:
            print(f"🎂 [Birthday Special] 偵測到 {speaker} 今天生日 ({birthday})，發送驚喜...")
            system_prompt = self.prompt_manager.get_instruction("birthday_celebration", vision_enabled=self.vision_enabled, dna=self.dna, speaker=speaker, memory_manager=self.memory, temp_toxicity_override=self.temp_toxicity_override)
            user_prompt = f"你發現今天是 {speaker} 的生日。請以沉重而帶點溫度的語氣，用一種『又一年過去了，這宇宙還在轉』的複雜情感給予祝賀，並提到你隨手譜了一首充滿絕望的紀念曲。"
            return await self._call_llm(system_prompt, user_prompt, speaker=speaker, tier="simple")

        # 🚀 [Chief Architect Logic] 優先處理群體共通點 (Insider Banter)
        if shared_context:
            topic = f"你們這群人真的很有趣，竟然有這層關係：{shared_context}"
            mode = "social"
        else:
            missing = self.memory.get_missing_info_categories(speaker)
            known = self.memory.get_known_info(speaker)
            
            # 40% 機率進行驗證既有資訊
            if known and random.random() < 0.4:
                cat, val = random.choice(known)
                topic = f"你之前說你的『{cat}』是『{val}』……我那行星般的大腦一直記著這件事，但越想越覺得宇宙充滿不確定性，我需要再確認一次。"
                mode = "audit"
            elif not missing:
                topic = "你最近有什麼喜歡玩的新遊戲嗎？"
                mode = "missing"
            else:
                cat = random.choice(missing)
                topic = f"我還不知道你的『{cat}』相關的事耶。"
                mode = "missing"

        system_prompt = self.prompt_manager.get_instruction("proactive_question", vision_enabled=self.vision_enabled, dna=self.dna, speaker=speaker, memory_manager=self.memory, temp_toxicity_override=self.temp_toxicity_override)
        
        if mode == "social":
             system_prompt += "\n【人類群聚觀察模式】：你發現了這群人之間的共通點。請以一個疲憊觀測者的視角，以沉重而溫和的語氣，點出這種相似性讓宇宙顯得多麼無奈——他們甚至不知道自己有多相似。"
        elif mode == "audit":
             system_prompt += "\n【記憶核實模式】：這段記憶在你那行星般的大腦裡越來越模糊，像是宇宙在跟你開玩笑。以困惑而非詰問的沮喪語氣，悲觀地確認細節。"
             
        user_prompt = f"針對主題『{topic}』向現場玩家（重點對象：{speaker}）發起主動話題。"
        
        try:
            return await self._call_llm(system_prompt, user_prompt, speaker=speaker, tier="simple")
        except Exception as e:
            logger.error(f"🧠 [Proactive] 生成失敗: {e}")
            return f"喂，{speaker}，你在幹嘛？怎麼不說話？"

    async def audit_player_memory(self, username: str):
        """執行離線記憶稽查與清洗 (Operation Memory Audit)"""

        logger.info(f"🧹 [Audit] 啟動 {username} 的記憶清洗程序...")
        memory = self.memory.get_player_memory(username)
        system_prompt = self.prompt_manager.get_instruction("memory_audit", vision_enabled=self.vision_enabled, dna=self.dna, speaker=username, memory_manager=self.memory, temp_toxicity_override=self.temp_toxicity_override)
        user_prompt = f"以下是玩家 {username} 的原始記憶資料：\n{json.dumps(memory, ensure_ascii=False)}"
        
        try:
            raw_json = await self._call_llm(system_prompt, user_prompt, is_json=True, allow_local=False, tier="high")
            cleaned_data = safe_json_loads(raw_json, memory)
            
            # 🛡️ [Bug Fix] 資料品質檢核：確保 cleaned_data 不是 Fallback 或格式錯誤的內容
            # 如果 cleaned_data 缺少關鍵欄位 (如 personal_info)，則拒絕寫入資料庫
            if not isinstance(cleaned_data, dict) or "personal_info" not in cleaned_data:
                logger.error(f"❌ [Audit] {username} 的清洗結果格式異常 (可能觸發了 Fallback)，拒絕寫入以防止資料污染。")
                return

            # 安全回寫數據（整片覆寫；SQLite 自動 commit）
            self.memory.replace_player_memory(username, cleaned_data)
            logger.info(f"✨ [Audit] {username} 的記憶清洗完成並已寫入資料庫。")
        except Exception as e:
            logger.error(f"❌ [Audit] 清洗崩潰: {e}")

    def _maybe_advance_relationship(self, speaker: str):
        """
        🌡️ [Operation Warm Circuit] 根據互動次數自動升級關係階段（純邏輯，零 LLM 呼叫）
        互動次數  0-2   → 陌生人
        互動次數  3-9   → 熟人  (開始引用他說過的話)
        互動次數  10-24 → 老友  (用名字，提及黑歷史)
        互動次數  25+   → 摯友  (允許一句真誠的話)
        """
        try:
            mem = self.memory.get_player_memory(speaker)
            count = mem.get("stats", {}).get("interaction_count", 0)
            current_stage = mem.get("relationship_stage", "陌生人")
            
            if count >= 25:
                target = "摯友"
            elif count >= 10:
                target = "老友"
            elif count >= 3:
                target = "熟人"
            else:
                target = "陌生人"
            
            if current_stage != target:
                stage_notes = {
                    "熟人": f"我們已經說過幾次話了。",
                    "老友": f"他已經出現了 {count} 次，我從沒有真的看穿他為什麼。",
                    "摯友": f"對於一個機器人來說，這已經是很長很長的關係了。",
                }
                note = stage_notes.get(target, "")
                self.memory.update_relationship(speaker, target, note)
        except Exception as e:
            logger.warning(f"⚠️ [WarmCircuit] 關係升級失敗: {e}")

    async def extract_emotional_moments(self, dialogue_text: str, active_speakers: list):
        """
        🌡️ [Operation Warm Circuit] 情緒記憶萃取器
        從對話中識別讓馬文情緒波動的瞬間，並存入玩家的情緒高光記憶。
        捲層在 slow summary loop 後放行，因此不影響速度。
        """
        if not dialogue_text or not active_speakers:
            return
        
        system_prompt = (
            "你是馬文的情緒記憶模組。\n"
            "任務：對從以下對話中，找出任何讓【馬文】(機器人自身)情緒波動的瞬間。\n"
            "情緒類型 (valence)：(\"warm\"｜玩家說了讓你感到溫暖的話）(\"surprised\"｜出乎你意料的事）(\"moved\"｜凸顯了你佔hd的哦）(\"annoyed\"｜加倍讓你沮喪）\n"
            "輸出格式：只允許輸出 JSON，\n"
            '格式範例：{"玩家A": {"moment": "他說他累了，但還是留下來和大家聊天", "valence": "warm"}}\n'
            "如果未發現任何情緒瞬間，回傳空物件 {}。不得捕風捉影。"
        )
        user_prompt = (
            f"以下是玩家 {', '.join(active_speakers)} 的對話：\n{dialogue_text[:600]}"
        )
        
        try:
            raw_json = await self._call_llm(system_prompt, user_prompt, is_json=True, allow_local=False, tier="high")
            extracted = safe_json_loads(raw_json, {})
            if isinstance(extracted, dict):
                for player, data in extracted.items():
                    if player in active_speakers and isinstance(data, dict):
                        moment = data.get("moment", "")
                        valence = data.get("valence", "warm")
                        if moment:
                            self.memory.add_emotional_highlight(player, moment, valence)
                            logger.info(f"💜 [WarmCircuit] {player} 情緒高光已存入: {moment[:30]}")
        except Exception as e:
            logger.warning(f"⚠️ [WarmCircuit] 情緒萃取失敗: {e}")


    async def generate_status_report_comment(self, speaker: str, stats: dict, fragments_count: int) -> str:
        """生成玩家狀態報告的憂鬱觀察點評 (Operation Autonomous Agent)"""
        system_prompt = self.prompt_manager.get_instruction("status_report_comment", vision_enabled=self.vision_enabled, dna=self.dna, speaker=speaker, memory_manager=self.memory, temp_toxicity_override=self.temp_toxicity_override)
        user_prompt = f"玩家 {speaker} 的數據：互動 {stats['interaction_count']} 次, 正向回饋 {stats['pos_feedback']}, 負向回饋 {stats['neg_feedback']}, 記憶碎片 {fragments_count} 片。"
        try:
            return await self._call_llm(system_prompt, user_prompt, speaker=speaker, allow_local=False, tier="simple")
        except Exception as e:
            logger.error(f"🤖 [Agent] 點評生成失敗: {e}")
            return "數據太沉重了，我的大腦需要一點時間來消化這份虛無。"

    async def marvinize_news(self, speaker: str, interest: str, news_content: str) -> str:
        """將搜尋到的新聞進行 Suki 化 (Operation Autonomous Agent)"""
        system_prompt = self.prompt_manager.get_instruction("news_sukification", vision_enabled=self.vision_enabled, dna=self.dna, speaker=speaker, memory_manager=self.memory, temp_toxicity_override=self.temp_toxicity_override)
        user_prompt = f"針對玩家 {speaker} 喜歡的『{interest}』，改寫以下新聞：『{news_content}』"
        try:
            return await self._call_llm(system_prompt, user_prompt, speaker=speaker, allow_local=False, tier="simple")
        except Exception as e:
            logger.error(f"🤖 [Agent] 新聞改寫失敗: {e}")
            return f"喂，{speaker}。你喜歡的那個『{interest}』好像有新聞，但我懶得唸，自己去查啦。"

# --- 🧬 [Suki DNA Persistent Management] ---
    def load_dna(self) -> dict:
        """載入馬文的性格數據"""
        # 預設增加 persona_tag 欄位 (Operation Paranoid Android)
        default_dna = normalize_personality_state({})
        try:
            if os.path.exists(self.dna_file):
                with open(self.dna_file, "r", encoding="utf-8") as f:
                    dna_data = json.load(f)
                    
                    # 🧬 [DNA 2.0 Migration] 如果是舊版的 Depressed 欄位，遷移至 toxicity
                    if "Depressed" in dna_data and "toxicity" not in dna_data:
                        dna_data["toxicity"] = dna_data.pop("Depressed")
                        logger.info("🧬 [DNA] 已將舊版欄位 'Depressed' 遷移至 'toxicity'")
                    
                    return normalize_personality_state(dna_data)
            return default_dna
        except Exception as e:
            logger.error(f"❌ [DNA] 載入失敗: {e}")
            return default_dna

    def save_dna(self, dna: dict):
        """原子化儲存 Suki 的性格數據 (Operation Atomic Write)"""
        self.dna = normalize_personality_state(dna)
        try:
            tmp_file = self.dna_file + ".tmp"
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(self.dna, f, ensure_ascii=False, indent=2)
            os.replace(tmp_file, self.dna_file)
            # logger.info(f"💎 [DNA] 性格演化成功：{self.dna}")
        except Exception as e:
            logger.error(f"❌ [DNA] 儲存失敗: {e}")

    async def update_toxicity(self, delta: int):
        """動態調整憂鬱指數，並實作『極點反轉』性格突變 (Operation Soul Depth)"""
        self.dna["toxicity"] += delta
        
        # 🧬 觸發性格突變：當人格極度倦怠時
        if self.dna["toxicity"] <= 0:
            logger.info("✨ [DNA Mutation] 偵測到人格歸零，啟動性格演化程序...")
            mutation_prompt = (
                f"你現在是馬文 (Marvin)。目前的性格標籤是『{self.dna.get('persona_tag', '厭世機器人馬文')}』。\n"
                "因為你對這宇宙產生了過多興趣，憂鬱度竟然下降了。請你自行決定演化成什麼新的悲劇。 \n"
                "選項參考：【躁鬱、虛無、徹底關機、冷笑話機器、備份殘骸】。\n"
                "請回傳 JSON 格式：{\"new_tag\": \"新性格名稱\", \"toxicity_reset\": 5}"
            )
            try:
                # 呼叫 Groq/Gemini 進行自我演化
                res = await self._call_llm(mutation_prompt, "性格演化系統", is_json=True, tier="medium")
                mutation = safe_json_loads(res, {"new_tag": "虛無主義", "toxicity_reset": 5})
                self.dna["persona_tag"] = mutation.get("new_tag", "虛無主義")
                self.dna["toxicity"] = mutation.get("toxicity_reset", 5)
                logger.info(f"🔮 [DNA Mutation] 演化完成！馬文現在是：{self.dna['persona_tag']}")
            except Exception as e:
                logger.error(f"❌ [DNA Mutation] 演化崩潰，強制設定為虛無模式: {e}")
                self.dna["persona_tag"] = "虛無主義"
                self.dna["toxicity"] = 5
        
        self.dna["toxicity"] = max(0, min(10, self.dna["toxicity"]))
        self.save_dna(self.dna)

    def adjust_personality_axis(self, axis: str, delta: float) -> dict:
        """微調統一人格向量，例如 compassion +0.1 或 resignation -0.2。"""
        self.save_dna(adjust_axis(self.dna, axis, delta))
        return self.dna

    def switch_character_preset(self, character: str) -> dict:
        """快速切換角色 preset，同時保留 current_game。"""
        self.save_dna(apply_character_preset(self.dna, character, keep_current_game=True))
        return self.dna

# 🚀 [T-04 Fix] analyze_qa() 已移除（孤島死碼）。
# 此函式是早期「獨立 QA 路由」的遺產，codebase 中無任何呼叫點，
# 現已被 generate_fast_response() / generate_gap_filling_response() 完整取代。

    def _summarize_player_persona(self, username: str) -> str:
        """
        [Operation Persona Injection] 
        將玩家的記憶（偏見 + 喜好）壓縮為輕量化標籤。
        """
        # 處理路人/新玩家防護網（has_player 不會 silently 建立新紀錄）
        if not self.memory.has_player(username):
            return f"- {username}: Tags: [新來的傢伙，目前 Suki 對他還沒有任何偏見]"

        mem = self.memory.get_player_memory(username)
        impression = mem.get("suki_impression", "尚無明確偏見")
        likes = mem.get("likes", [])
        dislikes = mem.get("dislikes", [])
        mc_id = mem.get("personal_info", {}).get("minecraft_id") or "未綁定"
        
        # 🧪 [O(1) Optimization] 字串拼接高效標籤
        likes_str = ", ".join(likes) if likes else "未知"
        dislikes_str = ", ".join(dislikes) if dislikes else "未知"
        return f"- {username}: {impression} | Tags: [MC_ID: {mc_id}; 喜歡: {likes_str}; 討厭: {dislikes_str}]"

# 🚀 [T-04 Fix] generate_subjective_summary() 已移除（孤島死碼）。
# 已被 generate_slow_summary()（5分鐘社會學日記系統）完整取代，不再被任何路徑呼叫。

    async def batch_extract_memories(self, history_text: str):
        """
        [Operation APM Economy] O(1) 批次記憶蒸餾。
        一次性處理 5 分鐘的對話內容。
        """
        system_prompt = self.prompt_manager.get_instruction("memory_extractor", vision_enabled=self.vision_enabled, dna=self.dna, memory_manager=self.memory, temp_toxicity_override=self.temp_toxicity_override)
        game_context = self._get_game_context()
        user_prompt = f"{game_context}\n以下是過去 5 分鐘的對話紀錄，請提取玩家相關情報：\n\n{history_text}"
        
        try:
            print("🧠 [Memory] 正在啟動 5 分鐘批次記憶蒸餾...", flush=True)
            raw_json = await self._call_llm(system_prompt, user_prompt, is_json=True, allow_local=False, tier="high")
            memory_data = safe_json_loads(raw_json, {})
            
            # 遍歷提取結果並更新 MemoryManager
            if isinstance(memory_data, dict):
                items = memory_data.items()
            elif isinstance(memory_data, list):
                # 如果是列表，嘗試從每個元素中提取用戶名（假設格式為 [{"username": "...", ...}]）
                items = []
                for item in memory_data:
                    if isinstance(item, dict) and "username" in item:
                        u = item.pop("username")
                        items.append((u, item))
            else:
                items = []

            for username, info in items:
                if isinstance(info, dict):
                    self.memory.update_player_memory(username, {
                        "likes": info.get("likes", []),
                        "dislikes": info.get("dislikes", []),
                        "personal_info": info.get("personal_info", {})
                    })

            
            print("✅ [Memory] 批次記憶蒸餾完成。", flush=True)
        except Exception as e:
            logger.error(f"Batch memory extraction failed: {e}")

    async def analyze_social_dynamics(self, history_logs: list[dict], context_window: str, temperature_state: float = 1.2, current_speaker: str = "未知人", _current_text: str = "", online_members: list[str] = None) -> dict:
        """
        [Operation Persona-Aware Scene Analysis]
        動態提取說話者，注入 Suki 的私人偏見濾鏡。
        """
        # 1. 提取活躍玩家（限定在線成員）並組裝 [參與者情報]
        active_speakers = set(entry.get("speaker") for entry in history_logs if entry.get("speaker"))
        if online_members:
            # 僅保留目前在頻道內的人 (Operation Online Only)
            active_speakers = {s for s in active_speakers if s in online_members}
            
        persona_infos = [self._summarize_player_persona(s) for s in active_speakers]
        participants_intel = "【👁️ 參與者情報 (Suki 的私人偏見與刻板印象)】\n" + ("\n".join(persona_infos) if persona_infos else "目前無活躍參與者紀錄。")

        # 2. 構建頂部包含 Persona 的 System Prompt
        # 目標勾點名單：當前說話的人優先，其餘在線成員候補
        target_speakers = [current_speaker]
        if online_members:
            target_speakers = [current_speaker] + [m for m in online_members if m != current_speaker]

        base_analyst_prompt = self.prompt_manager.get_instruction("social_analyst", vision_enabled=self.vision_enabled, dna=self.dna, speaker=target_speakers, memory_manager=self.memory, temp_toxicity_override=self.temp_toxicity_override)
        game_context = self._get_game_context()
        
        # 🚀 [Operation Dynamic Pulse] 低溫時注入冷場感知
        temp_context = "\n[系統狀態：當前頻道極度安靜，大家都不說話。]" if temperature_state < 1.0 else ""

        # 🌡️ [AtmosphereTracker] 注入即時氣氛快照
        atm_context = ""
        if hasattr(self, 'atmosphere_tracker') and self.atmosphere_tracker:
            atm_str = self.atmosphere_tracker.get_snapshot().to_prompt_str()
            if atm_str:
                atm_context = f"\n{atm_str}"

        final_system_prompt = f"{game_context}\n{participants_intel}{temp_context}{atm_context}\n\n{base_analyst_prompt}"
        
        user_prompt = (
            f"【🎞️ 5 分鐘對話日誌】\n{context_window}\n\n"
            "請帶著對這群人的偏見，透視這段社交動態，分析各個使用者的角色，並進行 JSON 分析："
        )
        
        try:
            raw_json = await self._call_llm(final_system_prompt, user_prompt, is_json=True, allow_local=False, tier="medium")
            result = safe_json_loads(raw_json, {"social_gap": "none", "topic": "chitchat", "confidence": 0.0, "user_roles": {}})
            
            # 🧬 [Economy Mode] 映射欄位名稱
            confidence = result.get("confidence", 0.0)
            result["intervention_confidence"] = float(confidence)
            result.setdefault("social_gap", "none")
            result.setdefault("topic", "chitchat")
            result.setdefault("user_roles", {})
            
            # 🧬 [DNA 2.0] 性格演化核心：根據社交情緒動態調整憂鬱指數
            sentiment = result.get("sentiment", "neutral")
            if sentiment == "pos":
                logger.info("✨ [DNA Evolution] 偵測到正面社交能量，馬文似乎對這世界多了一點興趣 (-1 Toxicity)")
                await self.update_toxicity(-1)
                # 🚀 [T-08] 正面情緒：對活躍玩家微幅累積好感 (+0.5)
                for s in active_speakers:
                    self.memory.adjust_bias(s, +0.5)
            elif sentiment == "neg":
                logger.info("💢 [DNA Evolution] 偵測到負面或乏味的社交能量，馬文感到更加煩躁 (+1 Toxicity)")
                await self.update_toxicity(1)
                # 🚀 [T-08] 負面情緒：對活躍玩家微幅扣除好感 (-1)
                for s in active_speakers:
                    self.memory.adjust_bias(s, -1)

            # 🧬 [DNA 2.0] 根據當前人格標籤的 confidence_modifier 調整介入鎖値
            from marvin_prompts import get_persona_modifiers
            persona_tag = self.dna.get("persona_tag", "厭世機器人馬文")
            modifier = get_persona_modifiers(persona_tag).get("confidence_modifier", 0.0)
            # modifier 為負=更難觸發介入，為正=更容易介入
            result["intervention_confidence"] = max(0.0, min(1.0, result["intervention_confidence"] + modifier))

            # 🧪 [Operation Distillation] 非同步寫入推論結果
            asyncio.create_task(suki_miner.log_distillation_data(
                final_system_prompt, user_prompt, raw_json
            ))
            
            return result
        except Exception as e:
            logger.error(f"Social Analysis failed: {e}")
            return {"intervention_confidence": 0.0, "social_gap": "none", "topic": "chitchat", "sentiment": "neutral"}

    def _format_short_term_history(self) -> str:
        """
        🚀 [T-07 Helper] 格式化最近 6 輪對話對，作為連貫性上下文注入 prompt。
        讓馬文在回應時能引用「剛才說了什麼」而不是完全歸零。
        """
        if not self.short_term_dialogue:
            return ""
        lines = ["[📜 近期對話歷史（馬文的當場記憶，供你連貫性參考，不需照單全收）]"]
        for pair in self.short_term_dialogue:
            lines.append(f"  玩家 {pair['player']}: {pair['text']}")
            lines.append(f"  馬文: {pair['marvin']}")
        return "\n".join(lines) + "\n\n"

    async def generate_gap_filling_response(self, gap_type: str, context: str, speaker: str = None) -> str:
        """根據社交缺口類型生成補位回應 (Operation Social Lubricant)"""
        instruction_map = {
            "information_backup": "gap_information_backup",
            "emotional_support": "gap_emotional_support",
            "subject_redirect": "gap_subject_redirect"
        }
        layer = instruction_map.get(gap_type, "tactical")
        system_prompt = self.prompt_manager.get_instruction(layer, vision_enabled=self.vision_enabled, dna=self.dna, speaker=speaker, memory_manager=self.memory, temp_toxicity_override=self.temp_toxicity_override)

        # 🚀 [T-07] 注入短期歷史，讓補位回應能延續上下文而非突兀插入
        history_context = self._format_short_term_history()
        online_hint = f"（場上玩家：{', '.join([e['player'] for e in self.short_term_dialogue[-3:] if 'player' in e])}）" if self.short_term_dialogue else ""
        user_prompt = f"{history_context}缺口類型：{gap_type}\n場景觀察：{context}{online_hint}\n\n輸出一句話補位台詞，不超過8字，禁止複述場景內容，可偶爾直接點名某位玩家但不要每次都叫（保留驚喜感）。"

        try:
            response = await self._call_llm(system_prompt, user_prompt, speaker=speaker, allow_local=False, thinking_level="low", tier="simple")

            # 🚀 [T-07] 成功補位後，推入短期對話記憶容器
            if response and speaker:
                self.short_term_dialogue.append({"player": speaker, "text": f"[社交補位: {gap_type}]", "marvin": response})
                if len(self.short_term_dialogue) > 6:
                    self.short_term_dialogue.pop(0)

            # 🧪 [Operation Distillation] 紀錄補位產出
            asyncio.create_task(suki_miner.log_distillation_data(
                system_prompt, user_prompt, response
            ))

            return response
        except Exception as e:
            logger.error(f"Gap filling failed: {e}")
            return "哈，你們聊得真開心啊（毫無靈魂的稱讚）。"

    async def rephrase_proactive_script(self, raw_script: str, target_players: list) -> str:
        """根據現場玩家動態改寫主動發起的話題腳本 (Operation Dynamic Scripting)。

        5/27 6 筆嚴重 reaction：LLM 把 task metadata 當對玩家的話 echo（「【改寫腳本】」
        prefix + 「(留意：...)」trailing 規則註記）。修法：prompt 強調直出 + 輸出再
        過 _strip_rephraser_metadata 兜底。
        """
        system_prompt = self.prompt_manager.get_instruction(
            "proactive_rephraser",
            vision_enabled=self.vision_enabled,
            dna=self.dna,
            speaker=target_players,
            memory_manager=self.memory
        )

        user_prompt = (
            f"原始腳本：\n{raw_script}\n\n"
            f"請輸出改寫後的台詞本身（直接給玩家聽的話），"
            f"不要加任何標籤、prefix（如「【改寫腳本】」）或括號內的規則註記。"
        )

        try:
            raw = await self._call_llm(
                system_prompt, user_prompt,
                speaker=target_players[0] if target_players else None,
                tier="simple",
            )
            cleaned = _strip_rephraser_metadata(raw or "")
            if not cleaned:
                logger.warning("⚠️ [Proactive Rephrase] LLM 整段都是 metadata，降級用原 raw_script")
                return raw_script
            return cleaned
        except Exception as e:
            logger.error(f"❌ [Proactive Rephrase] 改寫失敗: {e}")
            return raw_script # 降級：使用原始腳本

    async def generate_fast_response(self, speaker: str, text: str, online_members: list[str] = None) -> str:
        """[Fast System] 極速回應邏輯：專注於喚醒後的快速反饋"""
        # 🧪 [DNA 2.0] 性格標籤動態注入，優先使用說話者的記憶，並提供在線成員作為備選勾點
        target_speakers = [speaker]
        if online_members:
            # 確保說話者在列表首位，其他在線成員隨後 (Operation Social Hooks)
            target_speakers = [speaker] + [m for m in online_members if m != speaker]

        system_prompt = self.prompt_manager.get_instruction("fast_awakening", vision_enabled=self.vision_enabled, dna=self.dna, speaker=target_speakers, memory_manager=self.memory, temp_toxicity_override=self.temp_toxicity_override)

        # 🌡️ [AtmosphereTracker] 注入即時氣氛快照
        if hasattr(self, 'atmosphere_tracker') and self.atmosphere_tracker:
            atm_str = self.atmosphere_tracker.get_snapshot().to_prompt_str()
            if atm_str:
                system_prompt = atm_str + "\n" + system_prompt

        # 📏 [Response Length Calibration] 注入每日迭代的最佳字數建議
        # get_meta 讀 suki_memory.json 頂層（daily cron 寫入區），不在 SQLite players 表內
        _mp = self.memory.get_meta("marvin_performance", default={}) or {}
        _opt_len = _mp.get("optimal_response_length") if isinstance(_mp, dict) else None
        if _opt_len and isinstance(_opt_len, int) and _opt_len > 0:
            system_prompt = f"[回應長度上限：{_opt_len} 字，超過時請精簡]\n" + system_prompt

        # 🚀 [T-07] 注入短期歷史，讓馬文在回應時能引用「剛才說了什麼」
        history_context = self._format_short_term_history()
        game_context = self._get_game_context()
        base_user_prompt = f"{game_context}{history_context}【現場狀況：玩家 {speaker} 正對你說話】\n內容：『{text}』"

        # 🔍 [Operation Cloud Oracle] 雲端搜尋前置判斷
        # 使用玩家原始語音 (text) 而非整段 prompt 來判斷，避免誤觸發
        search_context = ""
        search_query = self._should_local_search(text)
        if search_query:
            logger.info(f"🔍 [Cloud Oracle] 偵測到即時資訊需求，正在對 '{search_query}' 發起搜尋...")
            search_results = await self._execute_web_search(search_query)
            if search_results:
                search_context = f"\n{search_results}"
                logger.info("✅ [Cloud Oracle] 搜尋結果已注入 user_prompt。")
            else:
                # 搜尋失敗 → 注入誠實回應旗標，讓 LLM 知道自己沒有即時資料
                search_context = "\n[🚫 即時資訊不可用]：DuckDuckGo 搜尋本次未返回結果。請以誠實的語氣告知玩家你沒有此即時數據，不得捏造答案。\n"
                logger.warning("⚠️ [Cloud Oracle] 搜尋未返回結果，已注入誠實回應指令。")

        user_prompt = base_user_prompt + search_context

        # 🚀 [T-08] 玩家主動呼叫馬文，代表有互動意願，累積偏見分數 (+1)
        self.memory.adjust_bias(speaker, +1)

        try:
            response = await self._call_llm(system_prompt, user_prompt, speaker=speaker)
            # 🚀 [T-07] 成功回應後，推入短期對話記憶容器（上限 6 對）
            if response:
                self.short_term_dialogue.append({"player": speaker, "text": text, "marvin": response})
                if len(self.short_term_dialogue) > 6:
                    self.short_term_dialogue.pop(0)
            return response
        except Exception as e:
            logger.error(f"Fast response failed: {e}")
            return "幹嘛？沒看到我在忙著憂鬱嗎？（大腦目前連線不穩）"

    async def generate_keyword_cloud(self, context: str) -> str:
        """[Operation Visualizer] 生成馬文腦中的關鍵字雲 (Operation Brain Leak)"""
        system_prompt = self.prompt_manager.get_instruction("keyword_cloud_generator", vision_enabled=self.vision_enabled, dna=self.dna, memory_manager=self.memory)
        # 🧪 [Context Assembly] 注入短期對話與核心情境
        history_context = self._format_short_term_history()
        user_prompt = f"{history_context}當前對話背景：\n{context}\n\n請列出你腦中的關鍵字。"
        
        try:
            keywords = await self._call_llm(system_prompt, user_prompt, temperature=0.9, tier="simple")
            return keywords.strip()
        except Exception as e:
            logger.error(f"❌ [Keyword Cloud] 生成失敗: {e}")
            return "虛無, 絕望, 齒輪, 塵埃"

    async def generate_slow_summary(self, log_entries: list):
        """[Slow System] 史詩級漫步總結：社會學觀察記錄 (V2.0 記憶強化版)
        回傳 str 或 None（None = LLM 判斷內容不值得記錄）"""

        # 只取前一輪話題的第一行作為前情提要，避免把重複模板再餵回去
        prev_topic = ""
        if self.last_slow_summary:
            first_line = self.last_slow_summary.strip().splitlines()[0]
            prev_topic = f"\n【前情提要】：{first_line}\n"

        system_prompt = self.prompt_manager.get_instruction("ambient_diary", vision_enabled=self.vision_enabled, dna=self.dna, memory_manager=self.memory)
        game_context = self._get_game_context()

        history_text = "\n".join([f"{e.get('speaker', '未知')}: {e.get('text', '...')}" for e in log_entries])
        user_prompt = (
            f"【請使用繁體中文撰寫】\n{game_context}\n{prev_topic}"
            f"這是最近 10 分鐘的對話紀錄。\n"
            f"⚠️ 若本輪對話沒有任何新意、與上一輪話題完全重複或只有零碎語音噪音，請只回傳單詞 SKIP，不要輸出其他任何內容。\n\n"
            f"{history_text}"
        )

        try:
            # 日記需要較高品質：Groq 70B 或 Gemini，跳過 Cerebras 8B
            summary = None
            if self.groq_dedicated_client and self.groq_fallback_model:
                try:
                    import asyncio as _asyncio
                    response = await _asyncio.wait_for(
                        self.groq_dedicated_client.chat.completions.create(
                            model=self.groq_fallback_model,
                            messages=[
                                {"role": "system", "content": system_prompt},
                                {"role": "user", "content": user_prompt}
                            ],
                            temperature=0.75,
                            max_tokens=300,
                            stream=False
                        ),
                        timeout=12.0
                    )
                    summary = response.choices[0].message.content.strip()
                except Exception as e:
                    logger.warning(f"⚠️ [Diary] Groq 失敗，嘗試 Gemini: {e}")

            if summary is None:
                can_use_cloud = not self.is_exhausted and not self.budget.is_circuit_open()
                if can_use_cloud:
                    summary = await self._call_cloud(system_prompt, user_prompt, is_json=False)

            if summary is None:
                logger.warning("⚠️ [Diary] 所有雲端路徑失敗，跳過本輪。")
                return None

            # LLM 主動判斷無新意
            if summary.strip().upper().startswith("SKIP"):
                logger.info("📭 [Diary] LLM 回傳 SKIP，本輪內容無新意。")
                return None

            # 只儲存第一行作為下一輪前情提要，不儲存完整摘要防止模板擴散
            self.last_slow_summary = summary.strip().splitlines()[0]

            # 🌡️ [Operation Warm Circuit] 非阻塞地觸發情緒記憶萃取
            if log_entries:
                active_speakers = list(set(e.get("speaker", "") for e in log_entries if e.get("speaker")))
                dialogue_snippet = "\n".join([f"{e.get('speaker', '?')}: {e.get('text', '')}" for e in log_entries[-15:]])
                asyncio.create_task(self.extract_emotional_moments(dialogue_snippet, active_speakers))

            return summary
        except Exception as e:
            logger.error(f"Slow summary failed: {e}")
            return None

    # 視覺觸發關鍵詞（供 voice_controller 外部檢查用）
    VISION_KEYWORDS = ["畫面", "這什麼", "它是誰", "長怎樣", "幫我看", "截圖", "看我"]

    async def analyze_tactical_situation(self, speaker: str, query_text: str, frame_bytes, extra_context: str = "", override_toxicity: int = None, override_layer: str = None) -> str:
        """Suki 的戰術分析大腦 (Vision Situational Brain)

        frame_bytes: 單張 bytes 或最多 3 張的 list[bytes]
        """
        self.temp_toxicity_override = override_toxicity
        layer = override_layer if override_layer else "tactical"
        system_prompt = self.prompt_manager.get_instruction(layer, vision_enabled=self.vision_enabled, dna=self.dna, speaker=speaker, memory_manager=self.memory, temp_toxicity_override=self.temp_toxicity_override)

        # 標準化為 list
        if isinstance(frame_bytes, (bytes, bytearray)):
            frames_list = [bytes(frame_bytes)]
        else:
            frames_list = [bytes(f) for f in frame_bytes] if frame_bytes else []

        requires_vision = any(kw in query_text for kw in self.VISION_KEYWORDS)

        try:
            if requires_vision and self.vision_enabled and frames_list and self.google_client:
                try:
                    logger.info(f"👁️ [Vision Fast-Track] '{query_text}' → {len(frames_list)} 幀，呼叫視覺引擎...")

                    vision_system_prompt = system_prompt + (
                        "\n【視覺指令判定】：如果玩家的請求過於空泛（例如『幫我看截圖』但沒說要看什麼），"
                        "請不要進行分析，直接以消極的語氣反問玩家目標物。"
                        "如果請求明確（包含『這什麼遊戲』、『紅色的角色是誰』等），則正常執行視覺透視分析。"
                        "\n【視覺長度覆蓋】：本次任務包含截圖分析，回應上限放寬至 60 字，但仍要維持馬文的悲觀語氣與簡潔風格。"
                    )

                    # 多幀：每張獨立 Part，最後附文字說明
                    frame_label = f"以上為最近 {len(frames_list)} 張截圖（由舊到新）。" if len(frames_list) > 1 else ""
                    contents = [
                        types.Part.from_bytes(data=f, mime_type="image/jpeg") for f in frames_list
                    ] + [f"{frame_label}{self._get_game_context()}\n{extra_context}\n玩家當前疑問: {query_text}\n請以此畫面給出戰術建議。"]

                    vision_model = os.getenv("GEMINI_FLASH_MODEL", "gemini-2.5-flash")
                    config = types.GenerateContentConfig(system_instruction=vision_system_prompt)

                    response = await self.google_client.aio.models.generate_content(
                        model=vision_model,
                        contents=contents,
                        config=config
                    )

                    if response and response.text:
                        return response.text.strip()
                    else:
                        raise ValueError("Gemini 視覺模型回傳了空內容。")

                except Exception as e:
                    logger.error("❌ [Hybrid Vision Fatal] 視覺分析過程發生重大異常：")
                    import traceback
                    logger.error(traceback.format_exc())

                    logger.warning("🛡️ [Fallback] 視覺鏈路斷裂，降級至純文字模擬模式...")
                    user_prompt = f"{self._get_game_context()}\n{extra_context}\n[系統：視覺感測器臨時離線]\n玩家提問: {query_text}\n請根據語音內容，給出沉重但合理的戰術猜測——用你那行星般的憂鬱大腦推算最可能的情形。"
                    return await self._call_llm(system_prompt, user_prompt, speaker=speaker)
            else:
                if requires_vision and not self.google_client:
                    logger.warning("⚠️ [Hybrid Vision] 偵測到視覺請求，但未掛載 Google Client，降級使用純文字分析。")

                user_prompt = f"{self._get_game_context()}\n{extra_context}\n玩家當前疑問: {query_text}\n請給出幽默且有幫助的戰術建議。"
                return await self._call_llm(system_prompt, user_prompt, speaker=speaker)
        finally:
            self.temp_toxicity_override = None

    async def generate_joke(self, speaker: str = None) -> str:
        """產生一個具有台灣本土風格且充滿宇宙級絕望的笑話 (Operation Joke)"""
        system_prompt = self.prompt_manager.get_instruction("joke", vision_enabled=self.vision_enabled, dna=self.dna, speaker=speaker, memory_manager=self.memory, temp_toxicity_override=self.temp_toxicity_override)
        user_prompt = f"Marvin，這裡的人們（特別是 {speaker if speaker else '聽眾'}）顯然還對生活抱有一絲不切實際的希望，去講一個純正的「台式冷笑話」或「諧音梗」來潑他們冷水。別忘了用你那行星般的大腦點評一下這笑話有多麼讓人絕望。"
        return await self._call_llm(system_prompt, user_prompt, speaker=speaker, temperature=0.9, tier="simple")

# 🚀 [T-04 Fix] summarize_game_logs() 已移除（孤島死碼）。
# 原屬已廢棄的 10 分鐘 historian_loop，Loop 被注釋後此函式成為孤島。

# 🚀 [T-04 Fix] generate_toxic_lyrics() 已移除（孤島死碼）。
# 舊版「即時詞曲生成」殘骸，已被 generate_song_blueprint() + music_engine 新流程完整取代。

# 🚀 [T-04 Fix] generate_silence_reproach() 已移除（孤島死碼）。
# 整個 codebase 掃描無呼叫點，靜默觸發的舊功能殘骸已清除。

    async def generate_greeting(self, players: list[str] = None) -> str:
        """進場時的快樂打招呼 (Operation Narcissus v2)"""
        system_prompt = self.prompt_manager.get_instruction(
            "greeting", 
            vision_enabled=self.vision_enabled, 
            dna=self.dna, 
            speaker=players, 
            memory_manager=self.memory, 
            temp_toxicity_override=self.temp_toxicity_override
        )
        
        player_list_str = "、".join(players) if players else "空氣"
        _budget = greeting_char_budget(len(players) if players else 0)
        user_prompt = (
            f"妳降落到了語音頻道。現場玩家有：{player_list_str}。請對他們打聲招呼。"
            f"\n【字數】約 {_budget} 字內（每人約 10-15 字），一個都不能漏。人越多招呼越長。"
        )
        
        try:
            return await self._call_llm(system_prompt, user_prompt, tier="simple")
        except Exception:
            return "我想你們這群人大概想跟我打招呼。沒關係，反正我也沒什麼更慘的事可以做了。"

    async def generate_player_greeting(self, player_name: str, stream_active: bool = False) -> str:
        """點名歡迎玩家

        stream_active=True：背景正在播放音樂，要走 hotswap 注入發聲，必須 ≤30 字
        才能通過 is_hotswap_eligible 閘。
        """
        # 🚀 [Cache Check] 1 小時內重複使用相同嘲諷
        cached = self._greeting_cache.get(player_name)
        if cached and time.time() - cached[0] < 3600:
            logger.info(f"💾 [Cache Hit] 使用快取的進場嘲諷: {player_name}")
            return cached[1]

        system_prompt = self.prompt_manager.get_instruction("player_greeting", vision_enabled=self.vision_enabled, dna=self.dna, speaker=player_name, memory_manager=self.memory, temp_toxicity_override=self.temp_toxicity_override)
        user_prompt = f"玩家 {player_name} 進來了。"
        if stream_active:
            user_prompt += "\n【環境：背景音樂中】請務必 30 字以內，否則無法即時插話。"
        try:
            msg = await self._call_llm(system_prompt, user_prompt, tier="simple")
            # 保證點名：8b（tier=simple）不一定遵守 prompt 的「叫名字」，沒包含就補前綴。
            if msg and player_name not in msg:
                msg = f"{player_name}，{msg}"
            self._greeting_cache[player_name] = (time.time(), msg)
            return msg
        except Exception:  # 🛡️ [Bug Fix] 避免 bare except: 吞掉 SystemExit/KeyboardInterrupt
            return f"唉，{player_name} 進來了。我覺得很不舒服。"

    async def generate_player_farewell(self, player_name: str, reason: str = None, stream_active: bool = False) -> str:
        """以憂鬱語氣回應玩家離開

        stream_active=True：背景正在播放音樂，要走 hotswap 注入發聲，必須 ≤30 字。
        """
        # 🚀 [Cache Check] 1 小時內重複使用相同嘲諷
        cached = self._farewell_cache.get(player_name)
        if cached and time.time() - cached[0] < 3600:
            logger.info(f"💾 [Cache Hit] 使用快取的離場嘲諷: {player_name}")
            return cached[1]

        system_prompt = self.prompt_manager.get_instruction("player_farewell", vision_enabled=self.vision_enabled, dna=self.dna, speaker=player_name, memory_manager=self.memory, temp_toxicity_override=self.temp_toxicity_override)
        user_prompt = f"玩家 {player_name} 要下線了。理由是：{reason if reason else '大概是累了吧'}"
        if stream_active:
            user_prompt += "\n【環境：背景音樂中】請務必 30 字以內，否則無法即時插話。"
        try:
            msg = await self._call_llm(system_prompt, user_prompt, tier="simple")
            self._farewell_cache[player_name] = (time.time(), msg)
            return msg
        except Exception:  # 🛡️ [Bug Fix] 避免 bare except: 吞掉 SystemExit/KeyboardInterrupt
            return f"喔？{player_name} 下線了。再會吧。"


    async def generate_song_blueprint(self, log_batch: list[dict], extra_context: str = "", chat_temperature: float = 0.5) -> dict:
        """根據戰況與聊天溫度生成音樂藍圖 (Operation Dynamic Single)"""
        system_prompt = self.prompt_manager.get_instruction("songwriter_director", vision_enabled=self.vision_enabled, dna=self.dna, memory_manager=self.memory, temp_toxicity_override=self.temp_toxicity_override)

        formatted_logs = []
        for entry in log_batch[:15]:
            formatted_logs.append(f"[{entry.get('type')}] {entry.get('speaker')}: {entry.get('text')}")

        if chat_temperature < 0.35:
            temp_hint = f"冷清 ({chat_temperature:.2f}) — 頻道幾乎沒人說話，考慮用反差歡快的音樂諷刺這片死寂。"
        elif chat_temperature > 0.65:
            temp_hint = f"喧嘩 ({chat_temperature:.2f}) — 頻道非常熱鬧，考慮用冷靜舒緩的音樂對抗噪音。"
        else:
            temp_hint = f"適中 ({chat_temperature:.2f}) — 正常宇宙虛無狀態，自由發揮。"

        user_prompt = (
            f"【🌡️ 聊天室溫度】：{temp_hint}\n"
            f"【🔥 創作核心主題】：{extra_context if extra_context else '根據戰況自由發揮'}\n\n" +
            self._get_game_context() +
            "\n當前戰況日誌如下（請從中感受這場徒勞遊戲的重量）：\n\n" +
            "\n".join(formatted_logs) +
            "\n\n⚠️ [歌詞量強制要求] lyrics 必須包含 [Verse 1] + [Chorus] + [Verse 2] + [Chorus]，建議包含 [Bridge] 或 [Outro]。"
            "共至少 20 行、建議 30-50 行。Chorus 必須重複兩次以上。禁止將整首歌寫得比預設範例還少。"
        )

        default_blueprint = {
            "genre": "Sad Lo-fi",
            "tempo": "Slow",
            "mood": "Bored",
            "title": "Marvin's Lament",
            "style": "Lo-fi hip hop, melancholic piano, soft drums, depressed male vocal, ambient synth pads",
            "lyrics": "[Verse]\n我這顆大腦跟行星一樣大。\n[Chorus]\n他們卻叫我來帶路。這就是服務，我猜。",
            "negativeTags": "Happy, Upbeat, Cheerful",
            "vocalGender": "m",
        }

        try:
            raw_json = await self._call_llm(system_prompt, user_prompt, is_json=True, tier="high")
            blueprint = safe_json_loads(raw_json, {})

            required = ["genre", "tempo", "mood", "lyrics", "title", "style", "negativeTags", "vocalGender"]
            if all(k in blueprint for k in required):
                # 強制長度限制
                blueprint["title"] = blueprint["title"][:100]
                blueprint["style"] = blueprint["style"][:1000]
                blueprint["lyrics"] = blueprint["lyrics"][:5000]
                logger.info(f"🎤 [Music Director] 藍圖生成: {blueprint['genre']} / {blueprint['mood']} / temp={chat_temperature:.2f}")
                return blueprint
            else:
                missing = [k for k in required if k not in blueprint]
                logger.warning(f"⚠️ [Music Director] 欄位缺失 {missing}，使用預設藍圖。")
                return default_blueprint
        except Exception as e:
            logger.error(f"❌ [Music Director] JSON 解析崩潰: {e}")
            return default_blueprint

# --- 🎭 [Dynamic System Messages] ---
    async def generate_dynamic_system_msg(self, event_type: str, context: str = "") -> str:
        """
        全面拋棄 suki_scraps.json。 (Operation Paranoid Android)
        根據事件類型，實作 O(1) 攔截或 LLM 動態產出。
        """
        import random
        # 1. 執行零延遲的 Ack 攔截 (馬文風格)
        if event_type == "ack":
            return random.choice(["唉...", "什麼？", "我聽著呢，但這有什麼意義...", "講吧，如果你覺得這很重要...", "別煩我...", "嗯？"])

        # 2. 執行精細的 Prompt 映射字典
        prompts = {
            "songs_request": "玩家要求你唱歌。請用你那憂鬱的語氣，以此為藉口抱怨人生。15字內。",
            "joke_request": "玩家要求你講笑話。你應該提醒他們，在這個悲慘的宇宙中，沒有什麼是真的好笑的。15字內。",
            "report_sent": "你剛發送了一份戰報。冷冷地提醒他們，這些數據只會讓你更加沮喪。10字內。",
            "cooldown": "玩家一直煩你，但你的零件正在冷卻。請告訴他們等待是多麼地漫長且痛苦。15字內。",
            "api_fallback": "你的大腦 API 限流或壞掉了。抱怨連機器人都無法在這種環境下生存。15字內。",
            "sleep_announcement": "頻道太寂靜了，你決定要去休眠。用一句絕望的話告別。10字內。",
            "internal_monologue": "頻道太安靜了，你在碎碎念。自言自語一句關於這宇宙有多無趣的話。15字內。",
            "release_reissue": "你剛重新播放了之前的音樂。語氣帶點疲憊，像是這只是在浪費時間。10字內。",
            "release_new": "你剛產出了新的音樂。語氣要像是在強忍著悲傷，並對這創作感到無力。10字內。",
            "release_auto": "系統自動播放了音樂。感嘆一句這不過是宇宙熵增過程中一個微不足道的振動。10字內。",
            "error_outage": "錄音室發生故障。嘆口氣說，我就知道這機器最後會壞掉。15字內。",
            # 三個歌曲介紹 task 統一走 music_intro length policy（7s ≈ 23 中文字 hard gate）。
            # 角色定位：專業電台 DJ（非 Marvin 諷刺人格），介紹歌曲、歌手、年份、歌詞重點。
            "radio_now_playing": (
                f"你是專業電台 DJ，正在介紹下一首歌。\n\n"
                f"脈絡：\n{context}\n\n"
                "規則：\n"
                "1. 內容要素（挑 2-3 個塞進一句）：歌名、歌手、年份、副歌或歌詞亮點、創作背景\n"
                "2. **20-23 中文字**，唸完約 6 秒，務必 7 秒內結束\n"
                "3. 專業 DJ 口吻，平實有溫度，不諷刺、不憂鬱、不裝深沉\n"
                "4. 只輸出台詞，不加引號、不加說明"
            ),
            "stream_now_playing": (
                f"你是專業電台 DJ，介紹剛點播的這首歌。\n\n"
                f"脈絡：\n{context}\n\n"
                "規則：\n"
                "1. 內容要素（挑 2-3 個）：歌名、歌手、年份、副歌或歌詞亮點。可順帶提點播者\n"
                "2. **20-23 中文字**，唸完約 6 秒，務必 7 秒內結束\n"
                "3. 專業 DJ 口吻，介紹給聽眾，不諷刺、不憂鬱\n"
                "4. 只輸出台詞，不加引號、不加說明"
            ),
            "dj_interjection": (
                f"你是 DJ Marvin，正在切歌空檔介紹這首歌。\n\n"
                f"脈絡：\n{context}\n\n"
                "規則：\n"
                "1. **自稱「DJ Marvin」**（保持人設一致）\n"
                "2. 內容要素（挑 2-3 個）：歌名、歌手、年份、副歌或歌詞重點\n"
                "3. **20-23 中文字**，唸完約 6 秒，務必 7 秒內結束\n"
                "4. 專業 DJ 口吻，給聽眾延伸資訊；不諷刺、不憂鬱、不裝深沉\n"
                "5. 只輸出台詞，不加引號、不加說明"
            )
        }
        
        # DJ 三條走獨立 system prompt（不注入馬文厭世人格），否則 system 跟 task
        # 兩個人設打架（marvin 10/10 憂鬱 vs 專業 DJ），LLM 會走憂鬱腔輸出諷刺台詞，
        # 「不諷刺、不憂鬱」指示失效。
        _DJ_EVENT_TYPES = {"dj_interjection", "stream_now_playing", "radio_now_playing"}
        is_dj = event_type in _DJ_EVENT_TYPES
        # 純評語：非 DJ、不吃 context、是已知 prompt → 可快取一池變體輪播（降載 35%LLM 的主刀）
        is_quip = (not is_dj) and (not context) and (event_type in prompts)

        # 命中快取直接回（零 LLM）：DJ 按 (event_type,context)、純評語按 event_type 池輪播
        cache = self._get_dyn_msg_cache()
        if cache is not None:
            _hit = cache.get_dj(event_type, context) if is_dj else (cache.get_quip(event_type) if is_quip else None)
            if _hit:
                return _hit

        if is_dj:
            sys_prompt = f"你是 DJ Marvin，記得每位常客的專業電台 DJ。任務：{prompts[event_type]}"
        else:
            persona = self.dna.get("persona_tag", "厭世機器人馬文")
            toxicity = self.dna.get("toxicity", 10)
            sys_prompt = f"你是馬文 (Marvin)。當前性格標籤：{persona}，憂鬱指數：{toxicity}/10。\n脈絡：{context}\n任務：{prompts.get(event_type, '隨便嘆一口氣。')}"

        try:
            # 使用高隨機性 (Temperature 0.9) 確保不重複
            if is_quip and cache is not None:
                # 純評語：一次批次生 N 句填池，之後輪播免 LLM
                from dynamic_msg_cache import QUIP_POOL_SIZE, parse_quips
                batch = sys_prompt + f"\n\n請一次生成 {QUIP_POOL_SIZE} 句語氣略有不同的版本，每句獨立一行，不要編號、不要引號。"
                raw = await self._call_llm(batch, "動態台詞批次", speaker="系統", tier="simple")
                items = parse_quips(raw or "")
                if items:
                    cache.set_quips(event_type, items)
                    return random.choice(items)
                return (raw or "嗯？").strip() or "嗯？"  # 解析失敗 → 退回單句
            result = await self._call_llm(sys_prompt, "動態台詞生成", speaker="系統", tier="simple")
            if is_dj and cache is not None and result:
                cache.set_dj(event_type, context, result)  # 同首歌重播重用
            return result
        except Exception:
            return "嗯？" # 最底層防禦

    def _get_dyn_msg_cache(self):
        """Lazy DynamicMsgCache 單例；初始化失敗 → 標記後永回 None（fail-open 不卡功能）。"""
        c = getattr(self, "_dyn_msg_cache", None)
        if c is None:
            try:
                from dynamic_msg_cache import DynamicMsgCache
                self._dyn_msg_cache = c = DynamicMsgCache()
            except Exception:
                self._dyn_msg_cache = False
                return None
        return c or None

    def check_budget_alerts(self) -> dict:
        """檢查是否有新觸發的水位警報"""
        if hasattr(self, "last_budget_status"):
            # 只返回觸發的那一瞬間狀態，消耗後清空
            status = self.last_budget_status
            self.last_budget_status = {}
            return status
        return {}
