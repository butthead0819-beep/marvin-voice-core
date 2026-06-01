"""
Stream guard 例外：當 silent_during_stream=True 但同時 allow_hotswap=True 時，
play_tts 不應提前 mute return，而是讓控制權往下走到 hotswap 判定區塊
(voice_controller.py:5544)，由 _midsong_hotswap_active + is_hotswap_eligible
決定是否走熱切換注入。

Why: 打招呼 / 送客在 stream 中原本被硬 mute（line 5469 早 return），但 hotswap
機制已備好。要讓兩者結合，stream_mode + silent_during_stream + allow_hotswap
這個組合需要例外放行。
"""
from __future__ import annotations

from voice_guard_helpers import _should_mute_for_stream_guard


# ── 基本情境矩陣 ──────────────────────────────────────────────────────────────

def test_non_stream_never_mutes():
    """非 stream 模式，無論 silent_during_stream / allow_hotswap 都不該被此 guard 攔。"""
    assert _should_mute_for_stream_guard(False, True, False) is False
    assert _should_mute_for_stream_guard(False, True, True) is False
    assert _should_mute_for_stream_guard(False, False, False) is False


def test_stream_with_silent_during_stream_mutes():
    """傳統行為：stream_mode + silent_during_stream + 沒開 hotswap → mute。"""
    assert _should_mute_for_stream_guard(True, True, False) is True


def test_stream_without_silent_during_stream_does_not_mute():
    """非主動發言類別（silent_during_stream=False）不該被此 guard 攔。"""
    assert _should_mute_for_stream_guard(True, False, False) is False
    assert _should_mute_for_stream_guard(True, False, True) is False


# ── 新例外：allow_hotswap 救回 ────────────────────────────────────────────────

def test_stream_with_silent_and_hotswap_does_not_mute():
    """新行為：呼叫端 opt-in allow_hotswap → 放行給 hotswap 判定區處理。"""
    assert _should_mute_for_stream_guard(True, True, True) is False
