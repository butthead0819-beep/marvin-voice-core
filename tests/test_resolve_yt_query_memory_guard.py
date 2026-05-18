"""
Tests for memory pressure guard on yt-dlp resolve path.

Context: 5/18 22:05 incident — '馬文，播放陶喆的普通朋友' 失敗，traceback
顯示 yt_dlp.extractor.lazy_extractors.real_class → importlib.import_module
→ 讀 yt_dlp/extractor/youtube/_clip.py → EDEADLK。本質是 macOS 在記憶體吃緊
時對 importlib file read 回 EDEADLK，與 STT 同 root cause。

Guard 策略：critical 時跳過 yt-dlp 呼叫，避免浪費 200ms retry + 讓 user
拿到 discoverable 錯誤訊息。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_controller():
    """Build a minimal VoiceController with just enough state for _resolve_yt_query."""
    # voice_controller has heavy imports; only need the method itself
    from cogs.voice_controller import VoiceController
    bot = MagicMock()
    bot.cogs = {}
    # __init__ has a lot of side effects; bypass it
    vc = VoiceController.__new__(VoiceController)
    vc.bot = bot
    return vc


@pytest.mark.asyncio
async def test_resolve_yt_query_skips_when_memory_critical(monkeypatch):
    """When memory_guard reports critical, _resolve_yt_query must short-circuit
    to None without calling yt_dlp at all (avoid EDEADLK importlib chain)."""
    vc = _make_controller()

    with patch("cogs.voice_controller.is_memory_critical", return_value=True), \
         patch("yt_dlp.YoutubeDL") as mock_ydl:
        result = await vc._resolve_yt_query("陶喆 普通朋友")

    assert result is None
    mock_ydl.assert_not_called()  # never enters the extractor lazy-load path


@pytest.mark.asyncio
async def test_resolve_yt_query_proceeds_when_memory_ok(monkeypatch):
    """When memory is fine, _resolve_yt_query proceeds normally."""
    vc = _make_controller()

    fake_info = {
        "entries": [
            {
                "url": "https://stream.example/a",
                "title": "普通朋友",
                "uploader": "陶喆",
                "webpage_url": "https://youtube.com/watch?v=x",
                "duration": 240,
            }
        ]
    }
    fake_ydl = MagicMock()
    fake_ydl.__enter__ = MagicMock(return_value=fake_ydl)
    fake_ydl.__exit__ = MagicMock(return_value=False)
    fake_ydl.extract_info = MagicMock(return_value=fake_info)

    with patch("cogs.voice_controller.is_memory_critical", return_value=False), \
         patch("yt_dlp.YoutubeDL", return_value=fake_ydl), \
         patch("music_search.pick_best_music_candidate", side_effect=lambda entries: entries[0]):
        result = await vc._resolve_yt_query("陶喆 普通朋友")

    assert result is not None
    assert result["title"] == "普通朋友"
