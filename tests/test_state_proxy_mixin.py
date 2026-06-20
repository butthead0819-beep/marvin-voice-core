"""
StateProxyMixin — VoiceController 的「狀態存取 property 代理」抽到獨立檔（減肥）。

這些 property 把 stream_mode / radio_mode / stream_* / is_playing_audio /
tts_queue_duration 等代理到 MusicCog / _mixer（不在則用 _X_local fallback）。
全是 self 存取的 descriptor，以 mixin 併入後經 MRO 正常解析，行為零改動。

注：這是把委派 shim「搬出去」（保留 facade），不是刪 facade 改呼叫端——後者是
高風險 interface 遷移，且 facade 的解耦有其價值。
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

MOD = "cogs.voice_controller_state_proxy"

PROXY_PROPS = ["voice_client", "is_playing_audio", "tts_queue_duration",
               "stream_mode", "radio_mode", "stream_queue", "_last_search"]


def test_mixin_in_mro():
    from cogs.voice_controller import VoiceController
    from cogs.voice_controller_state_proxy import StateProxyMixin
    assert StateProxyMixin in VoiceController.__mro__


@pytest.mark.parametrize("name", PROXY_PROPS)
def test_property_moved_to_state_proxy_module(name):
    from cogs.voice_controller import VoiceController
    prop = VoiceController.__dict__.get(name)
    # VoiceController 自身不該再定義它（已搬到 mixin）
    assert prop is None, f"{name} 還留在 VoiceController.__dict__"
    prop = getattr(VoiceController, name)
    assert isinstance(prop, property)
    assert prop.fget.__module__ == MOD


def test_stream_mode_getter_falls_back_to_local_when_no_musiccog():
    # 行為不變驗證：沒有 MusicCog 時讀 _stream_mode_local
    from cogs.voice_controller import VoiceController
    vc = VoiceController.__new__(VoiceController)
    vc.bot = MagicMock()
    vc.bot.cogs.get.return_value = None
    vc._stream_mode_local = True
    assert vc.stream_mode is True
    vc.bot.cogs.get.return_value = None
    vc._stream_mode_local = False
    assert vc.stream_mode is False
