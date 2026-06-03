"""
TDD：Layer 1 Sink L1-7 — last_dave_error_time 應在 __init__ 就初始化。

問題：原本 __init__ 沒設 last_dave_error_time，依靠
`getattr(self, 'last_dave_error_time', 0)` 在第一次 DAVE 失敗時 defensive 讀。
跟 sink 其他所有 state attribute（last_audio_packet_time、user_buffers 等）
都在 __init__ 初始化的慣例不一致，造成：
- 維護者讀 __init__ 找不到這個 attribute，誤以為是 typo / 漏寫
- 直接讀 self.last_dave_error_time 會 AttributeError

修法：__init__ 補 self.last_dave_error_time = 0，DAVE 處理那邊直接讀
self.last_dave_error_time 不再用 getattr。
"""
from __future__ import annotations

from unittest.mock import patch

import pytest


def test_last_dave_error_time_initialized_to_zero():
    """sink 建立後 last_dave_error_time 應為 0，不該 AttributeError。"""
    from discord_voice_engine import RealtimeVADSink

    with patch("discord.ext.voice_recv.AudioSink.__init__", return_value=None):
        s = RealtimeVADSink(on_speech_cut_callback=lambda *a, **kw: None)

    # 直接 access，不該 AttributeError
    assert s.last_dave_error_time == 0


def test_sink_constructs_without_current_event_loop():
    """無 current event loop 時建構 sink 不應 RuntimeError。

    CI 跑整包時 pytest-asyncio 在前面 async 測試 teardown 後會清掉
    current loop（等同 set_event_loop(None)）。此時建構 sink 若靠
    asyncio.get_event_loop()，在 Python 3.12 會 raise
    'There is no current event loop'，造成一連串 setup ERROR。
    建構不該依賴 current loop 已存在——應抓 running loop，沒有就 fallback。
    """
    import asyncio

    from discord_voice_engine import RealtimeVADSink

    try:
        prev = asyncio.get_event_loop()
    except RuntimeError:
        prev = None
    asyncio.set_event_loop(None)
    try:
        with patch("discord.ext.voice_recv.AudioSink.__init__", return_value=None):
            s = RealtimeVADSink(on_speech_cut_callback=lambda *a, **kw: None)
        assert s.loop is not None
    finally:
        asyncio.set_event_loop(prev if prev is not None else asyncio.new_event_loop())
