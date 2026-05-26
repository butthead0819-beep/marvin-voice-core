"""返場 callback 投遞的純邏輯（proactive group-memory callback, T3）。

feature flag + 措辭模板。async glue（peek_shareable_callback → cooldown gate →
playback_lock TTS → consume_callback on success）在 cogs/voice_controller.py。

flag 預設 OFF：未設或設成假值時 callback 不發聲（dormant）→ merge/重啟不改變現有行為。
要測時設 env `CALLBACK_ON_JOIN=true`。eng-review 的 kill switch。
"""
import os

_TRUE_VALUES = {"1", "true", "yes", "on"}


def is_join_callback_enabled() -> bool:
    """返場 callback 是否開啟（feature flag，預設 OFF）。每次讀 env，允許 runtime 切換。"""
    return os.environ.get("CALLBACK_ON_JOIN", "").strip().lower() in _TRUE_VALUES


def format_callback_line(text: str) -> str:
    """把一則 callback 記憶包成返場台詞（模板）；空字串回空（呼叫端跳過）。

    目前走模板（零 LLM latency on join hot path）；LLM 措辭潤飾留後續 polish。
    """
    text = (text or "").strip()
    if not text:
        return ""
    return f"歡迎回來，你上次說要{text}，後來呢？"


def format_topic_callback_line(text: str) -> str:
    """MemoryCallbackAgent v3：對話中被主題拉出的 callback 措辭（模板）。

    跟 format_callback_line（join 場景「歡迎回來」）不同；這是 mid-conversation 觸發，
    語氣應該像「順著當前話題接一句」。空字串回空（handler 跳過）。
    """
    text = (text or "").strip()
    if not text:
        return ""
    return f"對了，你之前說要{text}，現在呢？"
