"""
MusicProxyMixin — VoiceController 對 MusicCog 的純委派 method shim 抽到獨立檔
（收尾 ④）。這些方法 body 只是 `mc = self.bot.cogs.get('MusicCog'); 轉呼 mc.X()`，
以 mixin 併入後仍在 VoiceController 實例上，外部呼叫者（vc.start_radio 等）零影響。

同樣是把 shim「搬出去」保留 facade，不是刪 facade 改呼叫端。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

MOD = "cogs.voice_controller_music_proxy"

MOVED = [
    "start_radio", "stop_radio", "play_radio_song", "_resolve_yt_query",
    "_auto_recommend", "play_stream_song", "_fetch_song_meta",
    "_extract_song_cover", "_delayed_cleanup", "_t2_discovery_candidates",
]


def test_mixin_in_mro():
    from cogs.voice_controller import VoiceController
    from cogs.voice_controller_music_proxy import MusicProxyMixin
    assert MusicProxyMixin in VoiceController.__mro__


@pytest.mark.parametrize("name", MOVED)
def test_shim_moved(name):
    from cogs.voice_controller import VoiceController
    fn = getattr(VoiceController, name)
    assert fn.__module__ == MOD


@pytest.mark.asyncio
async def test_start_radio_noop_when_no_musiccog():
    # 行為不變：沒有 MusicCog 時 shim 安靜 no-op（不 raise）
    from cogs.voice_controller import VoiceController
    vc = VoiceController.__new__(VoiceController)
    vc.bot = MagicMock()
    vc.bot.cogs.get.return_value = None
    await vc.start_radio("測試")  # 不應 raise


@pytest.mark.asyncio
async def test_start_radio_delegates_to_musiccog():
    from cogs.voice_controller import VoiceController
    vc = VoiceController.__new__(VoiceController)
    vc.bot = MagicMock()
    mc = MagicMock()
    mc.start_radio = AsyncMock()
    vc.bot.cogs.get.return_value = mc
    await vc.start_radio("測試")
    mc.start_radio.assert_awaited_once_with("測試")
