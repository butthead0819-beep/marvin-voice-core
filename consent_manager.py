"""
Tracks per-member opt-in consent for voice data processing.
Stored locally in consent.json (gitignored — never commit member decisions).

Data that requires consent:
  - Voice transcription sent to Groq (STT cleaning)
  - Conversation context sent to Google Gemini / Cerebras (LLM)
  - Behavioral data written to suki_memory.json
"""
import json
import os
import logging

logger = logging.getLogger(__name__)


class ConsentManager:
    def __init__(self, path: str = "consent.json"):
        self.path = path
        self._data = self._load()

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
        except Exception as e:
            logger.error(f"❌ [Consent] 儲存失敗: {e}")

    def is_consented(self, display_name: str) -> bool:
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
