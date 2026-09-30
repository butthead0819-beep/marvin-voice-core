"""TDD: DJ 尾段串場「講話窗口」依口白長度拉開（插空白）＋拿掉 dj_story 截斷。

2026-09-30 使用者定案：
1. DJ 串場不再被 truncate_for_tts("dj_story", ...) 截斷（LLM 字數降不下來，改善窗口
   而不是砍字）。長度上限只剩既有 _is_qualified_dj_script（≤120 字，超過退回模板）。
2. 曲2 最多只疊口白最後 _DJ_TAIL_NEXT_OVERLAP_S(=8.0) 秒；口白比「曲1 尾 8s + 曲2 頭
   8s」長的部分，在兩首之間留空白（見 cogs/music_cog_tail_dj.py::_wait_dj_tail_window）。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cogs.music_cog_tail_dj import (
    _DJ_TAIL_GAP_MAX_S,
    _DJ_TAIL_GAP_POLL_S,
    _DJ_TAIL_NEXT_OVERLAP_S,
)


def _make_bare_cog(stream_mode=True):
    from cogs.music_cog import MusicCog
    cog = MusicCog.__new__(MusicCog)
    cog.stream_mode = stream_mode
    return cog


def _monotonic_sequence(values):
    it = iter(values)

    def _next(*_a, **_kw):
        try:
            return next(it)
        except StopIteration:
            return values[-1]
    return _next


# ── _wait_dj_tail_window ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_waits_until_overlap_threshold_then_returns_positive():
    """剩餘序列 [12.0, 10.0, 7.9] → sleep 2 次、回傳 > 0。"""
    cog = _make_bare_cog()
    vc = MagicMock()
    vc._mixer.tts_load_seconds = MagicMock(side_effect=[12.0, 10.0, 7.9])

    sleep_mock = AsyncMock()
    with patch("cogs.music_cog_tail_dj.asyncio.sleep", sleep_mock), \
         patch("cogs.music_cog_tail_dj.time.monotonic", side_effect=_monotonic_sequence(
             [0.0, 0.2, 0.4, 0.6])):
        waited = await cog._wait_dj_tail_window(vc)

    assert sleep_mock.await_count == 2
    assert waited > 0


@pytest.mark.asyncio
async def test_already_below_overlap_no_sleep():
    """一開始就 ≤ 8.0 → sleep 0 次、回傳 0.0。"""
    cog = _make_bare_cog()
    vc = MagicMock()
    vc._mixer.tts_load_seconds = MagicMock(return_value=5.0)

    sleep_mock = AsyncMock()
    with patch("cogs.music_cog_tail_dj.asyncio.sleep", sleep_mock), \
         patch("cogs.music_cog_tail_dj.time.monotonic", side_effect=_monotonic_sequence([0.0, 0.0])):
        waited = await cog._wait_dj_tail_window(vc)

    sleep_mock.assert_not_awaited()
    assert waited == 0.0


@pytest.mark.asyncio
async def test_gap_max_caps_the_wait():
    """剩餘一直是 30.0、monotonic 推進超過上限 → 在上限內結束，不無窮迴圈。"""
    cog = _make_bare_cog()
    vc = MagicMock()
    vc._mixer.tts_load_seconds = MagicMock(return_value=30.0)

    # monotonic：起點 0.0，之後每輪推進超過 _DJ_TAIL_GAP_POLL_S，最終超過 GAP_MAX。
    ticks = [0.0]
    t = 0.0
    while t < _DJ_TAIL_GAP_MAX_S + 1.0:
        t += _DJ_TAIL_GAP_POLL_S
        ticks.append(t)

    sleep_mock = AsyncMock()
    with patch("cogs.music_cog_tail_dj.asyncio.sleep", sleep_mock), \
         patch("cogs.music_cog_tail_dj.time.monotonic", side_effect=_monotonic_sequence(ticks)):
        waited = await cog._wait_dj_tail_window(vc)

    assert sleep_mock.await_count < len(ticks), "應在上限內結束，不是無窮迴圈"
    assert waited <= _DJ_TAIL_GAP_MAX_S + _DJ_TAIL_GAP_POLL_S


@pytest.mark.asyncio
async def test_stream_mode_false_stops_immediately():
    """stream_mode=False 且剩餘 30.0 → sleep 0 次（被停播不留空白）。"""
    cog = _make_bare_cog(stream_mode=False)
    vc = MagicMock()
    vc._mixer.tts_load_seconds = MagicMock(return_value=30.0)

    sleep_mock = AsyncMock()
    with patch("cogs.music_cog_tail_dj.asyncio.sleep", sleep_mock), \
         patch("cogs.music_cog_tail_dj.time.monotonic", side_effect=_monotonic_sequence([0.0, 0.0])):
        waited = await cog._wait_dj_tail_window(vc)

    sleep_mock.assert_not_awaited()
    assert waited == 0.0


@pytest.mark.asyncio
async def test_no_mixer_or_no_vc_returns_immediately():
    """vc._mixer is None / vc is None → sleep 0 次、回傳 0.0。"""
    cog = _make_bare_cog()
    sleep_mock = AsyncMock()

    vc = MagicMock()
    vc._mixer = None
    with patch("cogs.music_cog_tail_dj.asyncio.sleep", sleep_mock):
        waited = await cog._wait_dj_tail_window(vc)
    assert waited == 0.0
    sleep_mock.assert_not_awaited()

    with patch("cogs.music_cog_tail_dj.asyncio.sleep", sleep_mock):
        waited = await cog._wait_dj_tail_window(None)
    assert waited == 0.0
    sleep_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_tts_load_seconds_exception_fail_open():
    """tts_load_seconds 丟例外 → 不往外拋、回傳 0.0。"""
    cog = _make_bare_cog()
    vc = MagicMock()
    vc._mixer.tts_load_seconds = MagicMock(side_effect=RuntimeError("boom"))

    sleep_mock = AsyncMock()
    with patch("cogs.music_cog_tail_dj.asyncio.sleep", sleep_mock), \
         patch("cogs.music_cog_tail_dj.time.monotonic", side_effect=_monotonic_sequence([0.0, 0.0])):
        waited = await cog._wait_dj_tail_window(vc)

    sleep_mock.assert_not_awaited()
    assert waited == 0.0


# ── wiring: _stream_loop_prepare_and_announce 呼叫 _wait_dj_tail_window ────────

def _make_wiring_cog():
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None

    from cogs.music_cog import MusicCog
    cog = MusicCog(bot)
    cog.stream_queue = []
    cog.stream_history = []
    cog._personal_shuffle = None
    cog._wait_dj_tail_window = AsyncMock(return_value=0.0)
    cog._play_audiophile_guide_preroll = AsyncMock(return_value=False)
    cog._maybe_play_dj_interjection = AsyncMock()
    cog._splice_owner_voice_clip = AsyncMock(side_effect=lambda audio, info: audio)
    cog._autorecommend_seed = MagicMock(return_value=None)
    return cog


def _wiring_vc():
    vc = MagicMock()
    vc.active_text_channel = None
    vc.voice_client = None
    vc.get_online_members = MagicMock(return_value=[])
    return vc


@pytest.mark.asyncio
async def test_prepare_and_announce_waits_when_dj_played_in_tail():
    cog = _make_wiring_cog()
    vc = _wiring_vc()
    info = {"url": "https://ex/cur", "title": "夜曲", "_dj_played_in_tail": True}

    await cog._stream_loop_prepare_and_announce(info, vc, "夜曲", "大肚")

    cog._wait_dj_tail_window.assert_awaited_once_with(vc)


@pytest.mark.asyncio
async def test_prepare_and_announce_no_wait_when_not_played_in_tail():
    cog = _make_wiring_cog()
    vc = _wiring_vc()
    info = {"url": "https://ex/cur", "title": "夜曲"}

    await cog._stream_loop_prepare_and_announce(info, vc, "夜曲", "大肚")

    cog._wait_dj_tail_window.assert_not_awaited()


# ── dj_story 截斷拿掉：≤120 字合格口白原文完整保留 ────────────────────────────

@pytest.mark.asyncio
async def test_long_qualified_dj_script_not_truncated(monkeypatch, tmp_path):
    """真實字速估算下（0.3s/字，舊 dj_story gate 會砍到 ~56 字），約 90 字多句口白要原文保留。"""
    from tests.test_dj_story_context import _exclude, _info, _make_cog

    _exclude(monkeypatch, "quick")
    cog = _make_cog(est_per_char=0.3, tmp_path=tmp_path)
    long_text = (
        "今天早上的風有點涼，大家出門記得多帶一件外套，別跟我一樣只穿短袖就衝出去。"
        "說到這種涼涼的天氣，就很適合來一首慢慢的歌，讓心情跟著節奏放鬆一下。"
        "接下來這首周杰倫的夜曲，送給點歌的大肚，也送給還在路上的你。"
    )
    assert 60 < len(long_text) <= 120
    cog.bot.router.generate_dynamic_system_msg = AsyncMock(return_value=long_text)

    result = await cog._fetch_dj_interjection_raw(_info())

    assert result is not None
    assert result["text"] == long_text
