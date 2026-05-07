import json
import os
import time
from datetime import datetime
import logging

logger = logging.getLogger("SukiBudget")

class SukiBudget:
    """
    Suki 預算與熔斷器 (Operation Budget Monitor)
    負責追蹤每日的 API Token 消耗量，並管理警告水位線。
    """
    def __init__(self, file_path="suki_budget.json", max_tokens=500000):
        self.file_path = file_path
        self.max_tokens = max_tokens
        self.data = self._load_data()
        self._check_reset()

    def _load_data(self):
        """讀取預算數據"""
        default_data = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "total_tokens": 0,
            "has_warned_80": False,
            "has_warned_95": False
        }
        try:
            if os.path.exists(self.file_path):
                with open(self.file_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            return default_data
        except Exception as e:
            logger.error(f"❌ [Budget] 讀取失敗: {e}")
            return default_data

    def _save_data(self):
        """原子化儲存數據 (Operation Atomic Write)"""
        try:
            tmp_file = self.file_path + ".tmp"
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_file, self.file_path)
        except Exception as e:
            logger.error(f"❌ [Budget] 儲存失敗: {e}")

    def _check_reset(self):
        """偵測日期變更，自動歸零"""
        today = datetime.now().strftime("%Y-%m-%d")
        if self.data["date"] != today:
            logger.info(f"📅 [Budget] 偵測到日期變更 ({self.data['date']} -> {today})，預算歸零。")
            self.data = {
                "date": today,
                "total_tokens": 0,
                "has_warned_80": False,
                "has_warned_95": False
            }
            self._save_data()

    def add_tokens(self, count: int) -> dict:
        """累加消耗的 Token 並返回觸發的警報狀態"""
        self._check_reset()
        self.data["total_tokens"] += count
        
        status = {
            "trigger_80": False,
            "trigger_95": False,
            "is_exhausted": self.data["total_tokens"] >= self.max_tokens,
            "current_percentage": (self.data["total_tokens"] / self.max_tokens) * 100
        }

        # 檢測水位線 (使用旗標避免重複觸發)
        if status["current_percentage"] >= 80 and not self.data["has_warned_80"]:
            status["trigger_80"] = True
            self.data["has_warned_80"] = True
            logger.warning(f"⚠️ [Budget] 跨越 80% 水位線 ({self.data['total_tokens']} tokens)")

        if status["current_percentage"] >= 95 and not self.data["has_warned_95"]:
            status["trigger_95"] = True
            self.data["has_warned_95"] = True
            logger.error(f"🚨 [Budget] 跨越 95% 水位線 ({self.data['total_tokens']} tokens)")

        self._save_data()
        return status

    def is_circuit_open(self) -> bool:
        """熔斷器是否已開啟"""
        self._check_reset()
        return self.data["total_tokens"] >= self.max_tokens

    def get_info(self):
        return {
            "used": self.data["total_tokens"],
            "max": self.max_tokens,
            "percentage": (self.data["total_tokens"] / self.max_tokens) * 100
        }

    @property
    def tokens(self):
        """用於相容性：回傳已使用的 Token 數"""
        return self.data["total_tokens"]

    @property
    def total_limit(self):
        """用於相容性：回傳總額度"""
        return self.max_tokens
