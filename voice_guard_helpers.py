"""voice_guard_helpers — VoiceController.play_tts 早期判斷的純函式。

抽出來方便單測（play_tts 本體依賴 VoiceController 全狀態太重）。
"""
from __future__ import annotations


def _should_mute_for_stream_guard(
    stream_mode: bool,
    silent_during_stream: bool,
    allow_hotswap: bool = False,
) -> bool:
    """Stream guard 的早期 mute 判斷。

    傳統行為：stream_mode + silent_during_stream → 主動發言類別在串流中靜音。
    已經移除 hotswap 機制，此時 allow_hotswap 不再起作用。
    """
    return stream_mode and silent_during_stream
