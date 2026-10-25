"""緊急停止（2026-10-25 使用者定）：喊「馬文閉嘴」→ Marvin 全靜音＋音樂停，只有 /marvin_estop 解除才恢復。

純核心：喊停語判斷 + 狀態（存 emergency_stop.json，重啟/回台後維持）。接線在 cogs/voice_controller_estop.py。
"""
from __future__ import annotations

import json
import logging
import os
import re

ESTOP_STATE_PATH = "emergency_stop.json"

logger = logging.getLogger(__name__)

# 句首叫名字 + 停嘴詞，整句就是這個（可重複「馬文閉嘴馬文閉嘴」、可帶語助詞）。
# 不收「麻煩」（「麻煩安靜一點」是對朋友說的）、不收「停止播放」（那是停歌）。
_ESTOP_RE = re.compile(
    r"(?:[\s,，、。.!！~～嗯恩啊阿呃欸誒唉哎喂]*(?:馬文|瑪文)[\s,，、]*(?:你)?(?:給我)?"
    r"(?:緊急停止|閉嘴|安靜(?:一?點)?|停下來|(?:不要|別)再?(?:講|說|吵)了?)"
    r"(?:啦|了|喔|哦|啊|好嗎|好不好|拜託|[\s,，、。.!！~～])*)+"
)


def is_estop_phrase(text: str) -> bool:
    return bool(text) and _ESTOP_RE.fullmatch(text.strip()) is not None


class EmergencyStop:
    def __init__(self, path: str = ESTOP_STATE_PATH):
        self.path = path
        self.active: bool = False
        self.by: str | None = None
        self.since: float | None = None
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            self.active = bool(d.get("active"))
            self.by = d.get("by")
            self.since = d.get("since")
        except FileNotFoundError:
            pass
        except Exception:
            logger.exception("🛑 [EStop] 狀態檔讀取失敗，當作未停止")

    def _save(self) -> None:
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"active": self.active, "by": self.by, "since": self.since}, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except Exception:
            logger.exception("🛑 [EStop] 狀態檔寫入失敗（記憶體狀態仍有效）")

    def engage(self, by: str, now: float) -> bool:
        """回傳是否從未停止→停止（已停止回 False，不覆寫 by/since）。"""
        if self.active:
            return False
        self.active, self.by, self.since = True, by, now
        self._save()
        return True

    def release(self, by: str, now: float) -> bool:
        """回傳是否從停止→解除。"""
        if not self.active:
            return False
        self.active, self.by, self.since = False, by, now
        self._save()
        return True
