"""request_volume_swap — 語音調音量後該不該排一次中途熱切換套用新音量。

串流烤死 ffmpeg volume → 改 stream_volume 只能次首生效；hotswap 開啟時改走 second-stream
重 render 讓新音量即時生效。此測試鎖住「閘」：只有真的在串流 + hotswap 開時才標 pending，
否則退回次首生效（不標）。實際 ffmpeg arming 走實機驗證，這裡只測純決策。
"""
from __future__ import annotations

from cogs.voice_controller import VoiceController


def _ctrl(*, stream_mode, has_source, url):
    c = VoiceController.__new__(VoiceController)
    c.stream_mode = stream_mode
    c._stream_position_source = object() if has_source else None
    c._current_stream_url = url
    c._pending_volume_swap = False
    return c


def test_marks_pending_when_streaming_and_hotswap_on(monkeypatch):
    monkeypatch.setenv("MARVIN_MIDSONG_HOTSWAP_ENABLED", "true")
    c = _ctrl(stream_mode=True, has_source=True, url="http://x")
    c.request_volume_swap()
    assert c._pending_volume_swap is True


def test_no_pending_when_hotswap_disabled(monkeypatch):
    monkeypatch.setenv("MARVIN_MIDSONG_HOTSWAP_ENABLED", "false")
    c = _ctrl(stream_mode=True, has_source=True, url="http://x")
    c.request_volume_swap()
    assert c._pending_volume_swap is False


def test_no_pending_when_not_streaming(monkeypatch):
    monkeypatch.setenv("MARVIN_MIDSONG_HOTSWAP_ENABLED", "true")
    c = _ctrl(stream_mode=False, has_source=True, url="http://x")
    c.request_volume_swap()
    assert c._pending_volume_swap is False


def test_no_pending_when_no_stream_source(monkeypatch):
    monkeypatch.setenv("MARVIN_MIDSONG_HOTSWAP_ENABLED", "true")
    c = _ctrl(stream_mode=True, has_source=False, url="http://x")
    c.request_volume_swap()
    assert c._pending_volume_swap is False
