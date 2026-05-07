import json
import os
import time
import logging
from datetime import datetime

logger = logging.getLogger("SukiMemory")

class MemoryManager:
    """
    Marvin 長期記憶倉庫 (Operation Paranoid Android)
    負責維護每個玩家的結構化個人資訊、喜好、厭惡與禁忌。
    """
    def __init__(self, file_path="suki_memory.json"):
        self.file_path = file_path
        self._dirty = False
        self._last_save_time = 0
        self.data = self._load_data()

    def _load_data(self):
        """載入記憶數據"""
        default_data = {"players": {}}
        try:
            if os.path.exists(self.file_path):
                with open(self.file_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            return default_data
        except Exception as e:
            logger.error(f"❌ [Memory] 載入失敗: {e}")
            return default_data

    def _save_data(self, force=False):
        """原子化儲存數據 (Operation Atomic Write)"""
        self._dirty = True
        now = time.time()
        
        # 🚀 [Optimization] 避免頻繁寫入，若距離上次寫入小於 10 秒且非強制，則僅標記為 Dirty
        if not force and (now - self._last_save_time < 10):
            return

        try:
            tmp_file = self.file_path + ".tmp"
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_file, self.file_path)
            self._last_save_time = now
            self._dirty = False
            # logger.debug(f"💾 [Memory] 數據已持久化至磁碟。")
        except Exception as e:
            logger.error(f"❌ [Memory] 儲存失敗: {e}")

    def flush(self):
        """強制立即寫入磁碟 (Operation Force Persist)"""
        if self._dirty:
            self._save_data(force=True)

    def get_player_memory(self, username: str) -> dict:
        """獲取特定玩家的記憶"""
        if username not in self.data["players"]:
            # 初始化全新玩家的記憶槽位
            self.data["players"][username] = {
                "personal_info": {
                    "food": None, "clothing": None, "housing": None, "transport": None,
                    "minecraft_id": None
                },
                "likes": [],
                "dislikes": [],
                "taboos": [],
                "stats": {
                    "interaction_count": 0,
                    "pos_feedback": 0,
                    "neg_feedback": 0,
                    "vul_feedback": 0
                },
                "news_queue": [],
                "song_history": [],
                "suki_impression": "",
                "bias_score": 0,
                "last_interacted_time": time.time(),
                "emotional_highlights": [],
                "behavioral_patterns": {},
                "relationship_stage": "陌生人",
                "relationship_note": "",
                "speech_dna": {},
            }
            self._save_data(force=True)  # 新玩家初始化，建議強制寫入一次確保結構存在
        
        # 🧪 [Optimization] 補齊舊資料或受損資料缺少的欄位 (Operation Structure Repair)
        p = self.data["players"][username]
        
        # 確保基礎物件結構存在
        if not isinstance(p, dict):
            # 如果資料完全壞掉，強制重建
            logger.warning(f"⚠️ [Memory] {username} 的資料格式錯誤 (非 dict)，正在強力修復...")
            p = {
                "personal_info": {"food": None, "clothing": None, "housing": None, "transport": None, "minecraft_id": None},
                "likes": [], "dislikes": [], "taboos": [],
                "stats": {"interaction_count": 0, "pos_feedback": 0, "neg_feedback": 0, "vul_feedback": 0},
                "news_queue": [], "song_history": [], "suki_impression": "", "bias_score": 0,
                "last_interacted_time": time.time(),
                "emotional_highlights": [], "behavioral_patterns": {},
                "relationship_stage": "陌生人", "relationship_note": "",
                "speech_dna": {},
            }
            self.data["players"][username] = p

        # 逐項檢查並補齊缺失欄位
        if "personal_info" not in p or not isinstance(p["personal_info"], dict):
            p["personal_info"] = {"food": None, "clothing": None, "housing": None, "transport": None, "minecraft_id": None}
            
        if "stats" not in p or not isinstance(p["stats"], dict):
            p["stats"] = {"interaction_count": 0, "pos_feedback": 0, "neg_feedback": 0, "vul_feedback": 0}
            
        if "vul_feedback" not in p["stats"]: p["stats"]["vul_feedback"] = 0
        if "news_queue" not in p: p["news_queue"] = []
        if "song_history" not in p: p["song_history"] = []
        if "suki_impression" not in p: p["suki_impression"] = ""
        if "minecraft_id" not in p["personal_info"]: p["personal_info"]["minecraft_id"] = None
        if "bias_score" not in p: p["bias_score"] = 0
        if "likes" not in p: p["likes"] = []
        if "dislikes" not in p: p["dislikes"] = []
        if "taboos" not in p: p["taboos"] = []
        
        # 🌡️ [Operation Warm Circuit] 四層記憶新增欄位
        # Layer 2: 情緒記憶 — 讓馬文情緒波動的歷史瞬間
        if "emotional_highlights" not in p: p["emotional_highlights"] = []
        # Layer 3: 行為記憶 — 玩家的習慣、口頭禪、常問問題
        if "behavioral_patterns" not in p: p["behavioral_patterns"] = {}
        # Layer 4: 關係進化 — 與馬文的關係階段
        if "relationship_stage" not in p: p["relationship_stage"] = "陌生人"
        if "relationship_note" not in p: p["relationship_note"] = ""
        # 🎭 [Operation Impression Show] 說話 DNA — 用於模仿秀
        if "speech_dna" not in p: p["speech_dna"] = {}
        
        # 更新最後互動時間
        p["last_interacted_time"] = time.time()
        return p

    def increment_stat(self, username: str, field: str, delta: float = 1.0):
        """累加玩家統計數據 (Operation RLHF 2.0)"""
        if username not in self.data["players"]:
            self.get_player_memory(username)
        
        stats = self.data["players"][username]["stats"]
        
        # 🚀 [SRE Defensive] 確保數值累加為 float 並處理舊有的 int 型別
        if field in stats:
            stats[field] = float(stats[field]) + float(delta)
        else:
            # 兼容新欄位
            stats[field] = float(delta)
            
        self._save_data()

    def enqueue_news(self, username: str, news_text: str):
        """將搜尋到的個人化新聞存入 Queue (Operation Autonomous Agent)"""
        if username not in self.data["players"]:
            self.get_player_memory(username)
        
        queue = self.data["players"][username]["news_queue"]
        # 限制 Queue 長度為 3，避免過時資訊堆積
        queue.append({"text": news_text, "timestamp": time.time()})
        if len(queue) > 3:
            queue.pop(0)
        self._save_data()

    def pop_news(self, username: str) -> str:
        """獲取並移除最舊的一條新聞"""
        if username not in self.data["players"]:
            return None
        queue = self.data["players"][username]["news_queue"]
        if not queue:
            return None
        news = queue.pop(0)
        self._save_data()
        return news["text"]

    def get_player_impression(self, username: str) -> str:
        """獲取 Marvin 對該玩家的私人偏見 (Operation Persona Injection)連同這悲慘的宇宙。"""
        mem = self.get_player_memory(username)
        return mem.get("suki_impression", "")

    def set_player_impression(self, username: str, impression: str):
        """設定 Marvin 對該玩家的私人偏見 (Operation Persona Injection)"""
        if username not in self.data["players"]:
            self.get_player_memory(username)
        self.data["players"][username]["suki_impression"] = impression
        self._save_data()

    def set_minecraft_id(self, username: str, mc_id: str):
        """綁定 Discord 玩家與 Minecraft ID (Operation GM Mapping)"""
        if username not in self.data["players"]:
            self.get_player_memory(username)
        self.data["players"][username]["personal_info"]["minecraft_id"] = mc_id
        self._save_data()
        logger.info(f"🧱 [Memory] 玩家 {username} 已綁定 Minecraft ID: {mc_id}")

    def update_player_memory(self, username: str, new_info: dict):
        """更新玩家記憶 (合併式更新)"""
        if username not in self.data["players"]:
            self.get_player_memory(username)
            
        player = self.data["players"][username]
        
        # 更新個人資訊 (食衣住行)
        if "personal_info" in new_info:
            for k, v in new_info["personal_info"].items():
                if v is not None:  # 🛡️ [Bug Fix P3] 修復不能儲存空字串或 0 值的問題
                    player["personal_info"][k] = v
                
        # 增量更新清單
        for key in ["likes", "dislikes", "taboos"]:
            if key in new_info and isinstance(new_info[key], list):
                # 排除重複
                current_set = set(player.get(key, []))
                for item in new_info[key]:
                    if item: current_set.add(item)
                player[key] = list(current_set)
        
        self._save_data()
        logger.info(f"🧠 [Memory] 已更新 {username} 的記憶庫。")

    def mark_taboo(self, username: str, topic: str):
        """將特定話題標記為禁忌"""
        if username not in self.data["players"]:
            self.get_player_memory(username)
        
        if topic and topic not in self.data["players"][username]["taboos"]:
            self.data["players"][username]["taboos"].append(topic)
            self._save_data()
            logger.warning(f"🚫 [Memory] 玩家 {username} 將話題『{topic}』列為禁忌。")

    def get_missing_info_categories(self, username: str) -> list:
        """回傳玩家尚未提供資訊的類別"""
        memory = self.get_player_memory(username)
        info = memory.get("personal_info", {})
        taboos = memory.get("taboos", [])
        
        # 排除已列為禁忌的類別
        missing = [k for k, v in info.items() if v is None and k not in taboos]
        return missing
    def get_known_info(self, username: str) -> list:
        """回傳玩家已提供的資訊類別與數值 (Operation Memory Verification)"""
        memory = self.get_player_memory(username)
        info = memory.get("personal_info", {})
        taboos = memory.get("taboos", [])
        
        # 回傳已提供且非禁忌的類別與內容
        known = [(k, v) for k, v in info.items() if v is not None and k not in taboos]
        return known

    def adjust_bias(self, username: str, delta: float):
        """
        🚀 [T-08] 調整玩家偏見分數，限制在 -10 ~ +10 之間。
        櫓正分 = 馬文對此人有更多「複雜情感」和在意。
        負分 = 馬文對此人更冷漠、更敷塞。
        """
        if username not in self.data["players"]:
            self.get_player_memory(username)  # 自動創建基礎紀錄
        current = float(self.data["players"][username].get("bias_score", 0))
        new_score = max(-10.0, min(10.0, current + float(delta)))
        self.data["players"][username]["bias_score"] = new_score
        self._save_data()
        logger.debug(f"🎭 [Bias] {username} 偏見分數調整: {current:.1f} → {new_score:.1f} (delta: {delta:+.1f})")

    def find_shared_interests(self, active_users: list) -> str:
        """比對活躍玩家間的共通點 (Operation Social Graph)"""
        if not active_users or len(active_users) < 2:
            return None

        user_data = {}
        for username in active_users:
            mem = self.get_player_memory(username)
            user_data[username] = {
                "likes": set(mem.get("likes", [])),
                "personal_info": mem.get("personal_info", {})
            }

        shared_points = []
        all_usernames = list(user_data.keys())

        # 🚀 [Chief Architect's Performance Optimization] 使用成對比對搵出交集
        for i in range(len(all_usernames)):
            for j in range(i + 1, len(all_usernames)):
                u1, u2 = all_usernames[i], all_usernames[j]
                
                # 1. 比對 Likes
                common_likes = user_data[u1]["likes"].intersection(user_data[u2]["likes"])
                if common_likes:
                    shared_points.append(f"玩家 {u1} 和 {u2} 有共同愛好: {', '.join(common_likes)}")

                # 2. 比對 Personal Info (食衣住行、地點等)
                p1, p2 = user_data[u1]["personal_info"], user_data[u2]["personal_info"]
                for k, v1 in p1.items():
                    if k in p2 and v1 and v1 == p2[k] and v1 not in ["未提及", "未知", "無"]:
                        shared_points.append(f"玩家 {u1} 和 {u2} 的『{k}』一樣: {v1}")

        return "\n".join(shared_points) if shared_points else None

    def add_song_history(self, username: str, song_title: str):
        """記錄玩家點過的歌曲"""
        if username not in self.data["players"]:
            self.get_player_memory(username)
        history = self.data["players"][username].get("song_history", [])
        # 如果歌已經在清單中，移到最後面（最新）
        if song_title in history:
            history.remove(song_title)
        history.append(song_title)
        # 限制歷史紀錄筆數，例如保留最近 20 首歌
        if len(history) > 20:
            history = history[-20:]
        self.data["players"][username]["song_history"] = history
        self._save_data()

    def get_song_history(self, username: str) -> list:
        """獲取玩家點過的歌曲清單"""
        if username not in self.data["players"]:
            return []
        return self.data["players"][username].get("song_history", [])

    def get_proactive_topics(self) -> list:
        """獲取主動發起話題的預設清單 (Operation Proactive Social)"""
        return self.data.get("proactive_topics", [])

    # ====================================================================
    # 🌡️ [Operation Warm Circuit] Layer 2-4 記憶方法
    # ====================================================================

    def add_emotional_highlight(self, username: str, moment: str, valence: str = "warm"):
        """
        記錄讓馬文情緒波動的瞬間 (Layer 2: 情緒記憶)
        valence: 'warm' | 'surprised' | 'moved' | 'annoyed'
        保留最新 5 筆，超出自動裁切最舊的。
        """
        if not username or not moment:
            return
        mem = self.get_player_memory(username)
        highlights = mem.get("emotional_highlights", [])
        highlights.append({
            "moment": moment,
            "valence": valence,
            "timestamp": time.time()
        })
        # 只保留最新 5 筆
        if len(highlights) > 5:
            highlights = highlights[-5:]
        self.data["players"][username]["emotional_highlights"] = highlights
        self._save_data()
        logger.debug(f"💜 [WarmCircuit] {username} 情緒高光已記錄: {moment[:20]}... ({valence})")

    def update_relationship(self, username: str, stage: str, note: str = ""):
        """
        更新馬文與玩家的關係階段 (Layer 4: 關係進化)
        stage: '陌生人' | '熟人' | '老友' | '摯友'
        """
        if username not in self.data["players"]:
            self.get_player_memory(username)
        p = self.data["players"][username]
        old_stage = p.get("relationship_stage", "陌生人")
        p["relationship_stage"] = stage
        if note:
            p["relationship_note"] = note
        self._save_data()
        if old_stage != stage:
            logger.info(f"💜 [WarmCircuit] {username} 關係進化: {old_stage} → {stage}")

    def update_behavioral_pattern(self, username: str, key: str, value: str):
        """
        更新玩家行為模式記憶 (Layer 3: 行為記憶)
        例如：key='口頭禪', value='不用這樣啦'
        """
        if username not in self.data["players"]:
            self.get_player_memory(username)
        patterns = self.data["players"][username].get("behavioral_patterns", {})
        patterns[key] = value
        self.data["players"][username]["behavioral_patterns"] = patterns
        self._save_data()
        logger.debug(f"🔄 [WarmCircuit] {username} 行為記憶更新: {key} = {value}")

    def get_rich_context(self, username: str) -> str:
        """
        組裝適合注入 prompt 的完整記憶脈絡字串 (Operation Warm Circuit)
        涵蓋情緒高光、行為記憶、關係溫度。
        """
        mem = self.get_player_memory(username)
        lines = []

        # 關係溫度
        stage = mem.get("relationship_stage", "陌生人")
        count = mem.get("stats", {}).get("interaction_count", 0)
        rel_note = mem.get("relationship_note", "")
        lines.append(f"[🤝 關係溫度]：{stage}（已互動 {count} 次）")
        if rel_note:
            lines.append(f"[📝 關係備忘]：{rel_note}")

        # 情緒高光（最新 1 筆，最有溫度）
        highlights = mem.get("emotional_highlights", [])
        if highlights:
            latest = highlights[-1]
            valence_map = {
                "warm": "感到一絲異樣的溫暖",
                "surprised": "出乎意料地",
                "moved": "心底某個角落有點波動",
                "annoyed": "感到加倍的沮喪"  
            }
            valence_desc = valence_map.get(latest.get("valence", "warm"), "有些情緒")
            lines.append(f"[💜 我記得的瞬間]：{latest['moment']}（當時我{valence_desc}）")

        # 🗞️ [Operation Autonomous Agent] 注入新聞話題
        queue = mem.get("news_queue", [])
        if queue:
            latest_news = queue[-1]
            # 如果是最近 24 小時內的新聞，則注入
            if time.time() - latest_news.get("timestamp", 0) < 86400:
                lines.append(f"[🗞️ 最新話題]：{latest_news.get('text', '')}")

        return "\n".join(lines) if lines else ""

    # ── [Operation Impression Show] 說話 DNA 存取 ───────────────────────────

    def get_speech_dna(self, username: str) -> dict:
        """取得玩家的說話 DNA（若無則回傳空 dict）"""
        mem = self.get_player_memory(username)
        return mem.get("speech_dna") or {}

    def update_speech_dna(self, username: str, dna: dict) -> None:
        """更新玩家說話 DNA（由定期分析任務寫入）"""
        mem = self.get_player_memory(username)
        dna["last_updated"] = __import__("datetime").datetime.now().strftime("%Y-%m-%d")
        mem["speech_dna"] = dna
        self._save_data()
        logger.info(f"🎭 [SpeechDNA] {username} 說話 DNA 已更新")

    def get_atmosphere_calibration(self) -> dict:
        """回傳 atmosphere_calibration.suggested_additions，供 AtmosphereTracker 補充話題關鍵字。"""
        return self._data.get("atmosphere_calibration", {}).get("suggested_additions", {})
