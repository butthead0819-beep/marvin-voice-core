"""_query_quality_gate 短句放行——誤刪除／按讚控制卡按鈕語音化.

2026-10-13 showay 喚醒說「馬文，誤點刪除」，cleaner 吐出「誤刪除」(3字) 被
too_short gate 擋掉；第二次 STT 成「馬文 5. 刪除」又被擋。SongFeedbackAgent
加了 misclick_delete / like_song 兩個 intent，但 gate 要先放行才輪得到 bus
判斷。比照 test_text_input_bypasses_audio_guards.py 的假 self 呼叫方式。
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from cogs.voice_controller import VoiceController


@pytest.mark.parametrize("query", ["誤刪除", "5. 刪除", "按讚"])
def test_quality_gate_allows_misclick_and_like_short_commands(query):
    fake = MagicMock()
    fake._strip_wake_word = lambda t: t
    should, reason = VoiceController._query_quality_gate(fake, query)
    assert should is True, f"{query!r} 被擋：{reason}"
