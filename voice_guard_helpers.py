"""voice_guard_helpers — VoiceController.play_tts 早期判斷的純函式。

抽出來方便單測（play_tts 本體依賴 VoiceController 全狀態太重）。
"""
from __future__ import annotations


def _should_mute_for_stream_guard(
    stream_mode: bool,
    silent_during_stream: bool,
    allow_hotswap: bool,
) -> bool:
    """Stream guard 的早期 mute 判斷。

    傳統行為：stream_mode + silent_during_stream → 主動發言類別在串流中靜音
    （不打斷音樂）。

    Hotswap 例外：呼叫端 opt-in allow_hotswap → 放行到下游 hotswap 判定區
    (voice_controller.py:5544)，由 _midsong_hotswap_active + is_hotswap_eligible
    決定是否走熱切換注入。短句通過 → stream 中也能發聲；長句仍 fallback 到原
    靜音行為（hotswap 區內部自行 return）。
    """
    return stream_mode and silent_during_stream and not allow_hotswap
