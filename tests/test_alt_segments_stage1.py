"""TDD: Alt-Lattice Rescue Stage 1 — alt_segments 序列化修復 + side-channel。

設計文件：~/.gstack/projects/.../jackhuang-main-design-AltLatticeRescue-20260702-075338.md

Stage 1 範圍（本檔只測 Python 端；Swift 端靠重編 + live grep 驗活）：
  - `_run_swift_stt` 對 __META__ 的 alt_segments（[[str]]）原樣透傳
  - per-speaker side-channel 單槽：`_store_alt_segments`
    * 永遠覆蓋（無 alt_segments 也存 None）——舊句 lattice 不得誤掛新 query
"""
from __future__ import annotations

import time

import pytest
from unittest.mock import AsyncMock, MagicMock


def _make_bot():
    bot = MagicMock()
    bot.router = MagicMock()
    bot.router.game_dict_string = ""
    bot.get_cog.return_value = None
    return bot


def _make_engine():
    from discord_voice_engine import DiscordVoiceEngine
    return DiscordVoiceEngine(_make_bot())


def _mock_subprocess(monkeypatch, stdout):
    async def fake_exec(*args, **kwargs):
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(stdout, b""))
        return proc

    import asyncio
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)


# ── META alt_segments 透傳 ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_v2_meta_passes_alt_segments_through(monkeypatch):
    meta_line = ('__META__ {"engine": "speechanalyzer", "segment_count": 2, '
                 '"alt_segments": [["馬文馬文", "播馬"], ["晴天", "青田"]]}')
    _mock_subprocess(monkeypatch, meta_line.encode() + b"\n" + "馬文播晴天".encode())
    engine = _make_engine()

    text, meta = await engine._run_swift_stt("/tmp/x.wav", is_wake_check=False, v2=True)

    assert text == "馬文播晴天"
    assert meta["alt_segments"] == [["馬文馬文", "播馬"], ["晴天", "青田"]]
    assert meta["segment_count"] == 2


# ── per-speaker side-channel 單槽 ────────────────────────────────────────────

def test_store_alt_segments_writes_slot():
    engine = _make_engine()
    segs = [["播"], ["晴天", "青田"]]
    engine._store_alt_segments("阿明", "播晴天", {"alt_segments": segs})

    raw_text, alt_segments, ts = engine._last_alt_segments["阿明"]
    assert raw_text == "播晴天"
    assert alt_segments == segs
    assert abs(ts - time.time()) < 5


def test_store_alt_segments_overwrites_with_none_when_meta_lacks_segments():
    """關鍵不變量：無 alt_segments 也要覆蓋槽位——舊句 lattice 不得誤掛新 query。"""
    engine = _make_engine()
    engine._store_alt_segments("阿明", "播晴天", {"alt_segments": [["晴天"]]})
    engine._store_alt_segments("阿明", "隨便聊聊", {})   # v1/Whisper 路徑無 meta 鍵

    raw_text, alt_segments, _ = engine._last_alt_segments["阿明"]
    assert raw_text == "隨便聊聊"
    assert alt_segments is None


def test_store_alt_segments_tolerates_none_meta():
    engine = _make_engine()
    engine._store_alt_segments("阿明", "你好", None)
    raw_text, alt_segments, _ = engine._last_alt_segments["阿明"]
    assert raw_text == "你好"
    assert alt_segments is None


def test_store_alt_segments_per_speaker_isolation():
    engine = _make_engine()
    engine._store_alt_segments("阿明", "播晴天", {"alt_segments": [["晴天"]]})
    engine._store_alt_segments("狗與露", "播夜曲", {"alt_segments": [["夜曲"]]})

    assert engine._last_alt_segments["阿明"][0] == "播晴天"
    assert engine._last_alt_segments["狗與露"][0] == "播夜曲"
