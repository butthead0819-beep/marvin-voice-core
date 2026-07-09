"""
tests/test_stream_reconnect_wait.py

TDD：語音 WS 短暫斷線（close code 1006）時，音樂串流不該整條收攤。

2026-07-10 實測 bug（bot_main.log 00:49）：一次 1006 語音斷線 → 播放中的歌被腰斬 →
下一首在 discord.py ~2s 重連視窗內撞 `_resolve_playback_device()==None` →
`play_stream_song` 立刻 `stream_mode=False` → 整個佇列「播放完畢」再也沒歌。

修法＝`_await_reconnect_device` 有界輪詢等重連再放棄。這裡只驗那顆純協程 helper 的行為
（play_stream_song 整條牽涉 ffmpeg/mixer，不做端到端）。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from cogs.music_cog import MusicCog


def _fake_self(stream_mode=True):
    """最小 self：helper 只用到 self.stream_mode。"""
    return SimpleNamespace(stream_mode=stream_mode)


class _VC:
    """_resolve_playback_device() 前 n 次回 None（模擬重連視窗），之後回 device。"""

    def __init__(self, none_times, device="DEVICE"):
        self._left = none_times
        self._device = device

    def _resolve_playback_device(self):
        if self._left > 0:
            self._left -= 1
            return None
        return self._device


# ── (1) 重連完成 → 回 device、不放棄 ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_await_reconnect_returns_device_when_reconnects():
    vc = _VC(none_times=2)   # 兩輪還在重連，第三輪 device 回來
    got = await MusicCog._await_reconnect_device(
        _fake_self(), vc, timeout_s=5.0, interval_s=0.01)
    assert got == "DEVICE"


# ── (2) 一直斷 → 逾時回 None（caller 才收攤）─────────────────────────────────

@pytest.mark.asyncio
async def test_await_reconnect_times_out_returns_none():
    vc = _VC(none_times=10_000)   # 永遠 None
    got = await MusicCog._await_reconnect_device(
        _fake_self(), vc, timeout_s=0.05, interval_s=0.01)
    assert got is None


# ── (3) 等待期間被停播（stream_mode False）→ 提早退出，不空等 ─────────────────

@pytest.mark.asyncio
async def test_await_reconnect_aborts_when_stopped():
    vc = _VC(none_times=10_000)
    me = _fake_self(stream_mode=False)   # 已被使用者停播
    got = await MusicCog._await_reconnect_device(
        me, vc, timeout_s=5.0, interval_s=0.01)
    assert got is None


# ── (4) vc 為 None → 直接 None，不炸 ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_await_reconnect_none_vc():
    got = await MusicCog._await_reconnect_device(
        _fake_self(), None, timeout_s=5.0, interval_s=0.01)
    assert got is None
