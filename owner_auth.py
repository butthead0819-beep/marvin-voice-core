"""營運者（owner）身分判定——全 bot 單一來源。

MARVIN_OWNER_ID 優先，其次 LOCAL_USER_ID（main_discord / voice_controller 既有用法），
都沒設就用既有預設值（與 wake_sample_collector.py / owner_song_voice_samples.py 一致）。
"""
import os

_DEFAULT_OWNER_ID = "876758076831723580"


def owner_id() -> int:
    raw = os.getenv("MARVIN_OWNER_ID") or os.getenv("LOCAL_USER_ID") or _DEFAULT_OWNER_ID
    try:
        return int(raw)
    except ValueError:
        return int(_DEFAULT_OWNER_ID)


def is_owner(user_id) -> bool:
    try:
        return int(user_id) == owner_id()
    except (TypeError, ValueError):
        return False


OWNER_ONLY_MESSAGE = "🔒 這個指令只有營運者能用。"
