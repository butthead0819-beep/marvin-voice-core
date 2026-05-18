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
