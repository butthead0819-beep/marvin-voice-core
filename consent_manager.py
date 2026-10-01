"""
Tracks per-member opt-in consent for voice data processing.
Stored locally in consent.json (gitignored — never commit member decisions).

Data that requires consent:
  - Wake phrase / rescue audio sent to Google Gemini
  - Voice transcription and conversation context sent to AI services (Google Gemini, Groq, Mistral, SambaNova, Together AI, OpenRouter)
  - Personal memory data stored locally on operator's machine
"""
import json
import os
import logging

logger = logging.getLogger(__name__)


def consent_notice(mention: str) -> str:
    """首次進語音頻道的資料使用聲明——內容必須與 PRIVACY.md §4 一致。"""
    return (
        f"🔐 **【資料使用聲明】** {mention}\n"
        "馬文在你說話時會：\n"
        "• 你喊「馬文」的那一句語音、以及馬文沒聽懂需要重新判斷的語音，會送到 **Google Gemini**\n"
        "• 轉成的文字連同對話記憶，會送到這些 AI 服務產生回應：**Google Gemini、Groq、Mistral、SambaNova、Together AI、OpenRouter**\n"
        "• 存在營運者電腦上的記憶資料（個人化記憶）\n\n"
        "請確認是否同意。若不同意，馬文不會處理你的語音。\n"
        "同意後可隨時用 `/marvin_optout` 撤回。"
    )


class ConsentManager:
    def __init__(self, path: str = "consent.json"):
        self.path = path
        self._data = self._load()
        self._mtime: float = self._get_mtime()

    def _get_mtime(self) -> float:
        try:
            return os.path.getmtime(self.path)
        except OSError:
            return 0.0

    def _reload_if_changed(self):
        mtime = self._get_mtime()
        if mtime != self._mtime:
            self._data = self._load()
            self._mtime = mtime

    def _load(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"consented": {}, "seen_notice": {}}

    def _save(self):
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
            self._mtime = self._get_mtime()
        except Exception as e:
            logger.error(f"❌ [Consent] 儲存失敗: {e}")

    def is_consented(self, display_name: str) -> bool:
        self._reload_if_changed()
        return bool(self._data.get("consented", {}).get(display_name))

    def set_consent(self, display_name: str, granted: bool):
        self._data.setdefault("consented", {})[display_name] = granted
        self._save()
        logger.info(f"🔐 [Consent] {display_name} → {'同意' if granted else '拒絕'}")

    def has_seen_notice(self, display_name: str) -> bool:
        return bool(self._data.get("seen_notice", {}).get(display_name))

    def mark_seen(self, display_name: str):
        self._data.setdefault("seen_notice", {})[display_name] = True
        self._save()
