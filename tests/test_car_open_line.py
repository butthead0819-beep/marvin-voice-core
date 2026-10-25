"""
tests/test_car_open_line.py
TDD：上車開場白真的念出來（_execute_car_open 改走 _speak_car_open_line 的
protected TTS + 直呼 MusicCog，不再借 inject_text「放一首X」繞一次 filler +
music ack）。見 car_http_app.py::_execute_car_open docstring。
"""
from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from music_recommender import Candidate


def _cand(title="晴天"):
    return Candidate(anchor_title=title, anchor_artist="周杰倫", lane="long_tail",
                      mode="direct", target_member=None, score=1.0)


class _FakeCarOpen:
    def __init__(self, line, song):
        self.line = line
        self.song = song


def _make_vc(*, play_tts_delay: float = 0.0, play_tts_raises: bool = False):
    vc = MagicMock()
    vc._tts_interrupted = True
    vc._protected_during_calls: list[bool] = []

    @contextlib.contextmanager
    def _protected_tts_window():
        prev = getattr(vc, "_tts_protected", False)
        vc._tts_protected = True
        try:
            yield
        finally:
            vc._tts_protected = prev
    vc._protected_tts_window = _protected_tts_window
    vc._tts_protected = False

    async def _play_tts(text, *, already_in_channel=False, **kw):
        vc._protected_during_calls.append(vc._tts_protected)
        if play_tts_delay:
            await asyncio.sleep(play_tts_delay)
        if play_tts_raises:
            raise RuntimeError("tts boom")
        return None
    vc.play_tts = AsyncMock(side_effect=_play_tts)

    mc = MagicMock()
    mc._handle_voice_music_command = AsyncMock()
    vc.bot = MagicMock()
    vc.bot.cogs.get.side_effect = lambda name: mc if name == "MusicCog" else None
    vc._mc = mc
    return vc


@pytest.mark.asyncio
async def test_line_and_song_both_fire_no_inject_text(monkeypatch):
    import car_http_app

    async def _fake_resolve_query(song, *, pool_provider, resolve_fn):
        return "晴天 周杰倫"
    monkeypatch.setattr("car_open.resolve_car_open_query", _fake_resolve_query)
    fake_inject = AsyncMock()
    monkeypatch.setattr(car_http_app, "inject_text", fake_inject)

    vc = _make_vc()
    car_open = _FakeCarOpen("上車了，來首歌", _cand())

    await car_http_app._execute_car_open(vc, car_open, "jack", lambda: [_cand()])

    vc.play_tts.assert_awaited_once()
    assert vc.play_tts.await_args.args[0] == "上車了，來首歌"
    vc._mc._handle_voice_music_command.assert_awaited_once_with(
        "jack", "放一首晴天 周杰倫", "play", play_ack=False)
    fake_inject.assert_not_awaited()


@pytest.mark.asyncio
async def test_line_finishes_before_music_command_fires(monkeypatch):
    import car_http_app

    order: list[str] = []

    async def _fake_resolve_query(song, *, pool_provider, resolve_fn):
        order.append("resolved")
        return "晴天"
    monkeypatch.setattr("car_open.resolve_car_open_query", _fake_resolve_query)

    vc = _make_vc(play_tts_delay=0.02)

    async def _record_play_tts(text, *, already_in_channel=False, **kw):
        await asyncio.sleep(0.02)
        order.append("line_done")
    vc.play_tts = AsyncMock(side_effect=_record_play_tts)

    async def _record_music_cmd(*a, **kw):
        order.append("music_cmd")
    vc._mc._handle_voice_music_command = AsyncMock(side_effect=_record_music_cmd)

    car_open = _FakeCarOpen("上車了", _cand())
    await car_http_app._execute_car_open(vc, car_open, "jack", lambda: [_cand()])

    assert order.index("line_done") < order.index("music_cmd")


@pytest.mark.asyncio
async def test_play_tts_exception_does_not_block_song(monkeypatch):
    import car_http_app

    async def _fake_resolve_query(song, *, pool_provider, resolve_fn):
        return "晴天"
    monkeypatch.setattr("car_open.resolve_car_open_query", _fake_resolve_query)

    vc = _make_vc(play_tts_raises=True)
    car_open = _FakeCarOpen("上車了", _cand())

    await car_http_app._execute_car_open(vc, car_open, "jack", lambda: [_cand()])

    vc._mc._handle_voice_music_command.assert_awaited_once_with(
        "jack", "放一首晴天", "play", play_ack=False)


@pytest.mark.asyncio
async def test_empty_line_skips_play_tts_song_still_queued(monkeypatch):
    import car_http_app

    async def _fake_resolve_query(song, *, pool_provider, resolve_fn):
        return "晴天"
    monkeypatch.setattr("car_open.resolve_car_open_query", _fake_resolve_query)

    vc = _make_vc()
    car_open = _FakeCarOpen("", _cand())

    await car_http_app._execute_car_open(vc, car_open, "jack", lambda: [_cand()])

    vc.play_tts.assert_not_awaited()
    vc._mc._handle_voice_music_command.assert_awaited_once_with(
        "jack", "放一首晴天", "play", play_ack=False)


@pytest.mark.asyncio
async def test_resolve_returns_none_no_music_command_but_line_awaited(monkeypatch):
    import car_http_app

    async def _fake_resolve_query(song, *, pool_provider, resolve_fn):
        return None
    monkeypatch.setattr("car_open.resolve_car_open_query", _fake_resolve_query)

    vc = _make_vc()
    car_open = _FakeCarOpen("上車了", _cand())

    await car_http_app._execute_car_open(vc, car_open, "jack", lambda: [_cand()])

    vc.play_tts.assert_awaited_once()
    vc._mc._handle_voice_music_command.assert_not_awaited()

    # 沒有 pending task 殘留警告：所有 task 都已完成。
    pending = [t for t in asyncio.all_tasks() if not t.done() and t is not asyncio.current_task()]
    assert pending == []


@pytest.mark.asyncio
async def test_protected_tts_true_during_play_tts(monkeypatch):
    import car_http_app

    async def _fake_resolve_query(song, *, pool_provider, resolve_fn):
        return "晴天"
    monkeypatch.setattr("car_open.resolve_car_open_query", _fake_resolve_query)

    vc = _make_vc()
    car_open = _FakeCarOpen("上車了", _cand())

    await car_http_app._execute_car_open(vc, car_open, "jack", lambda: [_cand()])

    assert vc._protected_during_calls == [True]
    # 離開 window 後恢復成進入前的值（本例進入前是 False）。
    assert vc._tts_protected is False
