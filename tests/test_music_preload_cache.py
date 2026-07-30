"""TDD: DJ Tail 點火時背景預解碼下一首音樂，蓋掉 preload_f32_source 的整首解碼延遲。

背景（2026-07-25 實測回歸）：`preload_f32_source` 消除了 Mac mixer 的中段爆音，但代價是
換源前要等整首解碼完——這段延遲原本落在「DJ 開場白講完」跟「下一首音樂真的出聲」中間，
變成聽得到的中斷。修法＝在 DJ Tail 既有的 5 秒尾段窗口點火當下，就背景先解碼好下一首，
`play_stream_song` 真正要換源時直接用解碼好的結果，零等待。

只測純邏輯（cache 記帳 + 「有預解碼就用、沒有就退回現場建」的決策），不碰真 ffmpeg/mixer
（跟 test_stream_reconnect_wait.py 同一個原則：play_stream_song 整條牽涉 ffmpeg/mixer，
不做端到端）。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from cogs.music_cog import MusicCog


def _fake_self():
    return SimpleNamespace(_preload_music_cache={})


# ── _start_music_preload：cache 記帳 ────────────────────────────────────────

@pytest.mark.asyncio
async def test_start_music_preload_creates_task_for_url():
    me = _fake_self()
    MusicCog._start_music_preload(me, {"url": "https://ex/a"})
    assert "https://ex/a" in me._preload_music_cache
    assert isinstance(me._preload_music_cache["https://ex/a"], asyncio.Task)
    me._preload_music_cache["https://ex/a"].cancel()


@pytest.mark.asyncio
async def test_start_music_preload_idempotent_same_url():
    me = _fake_self()
    MusicCog._start_music_preload(me, {"url": "https://ex/a"})
    task1 = me._preload_music_cache["https://ex/a"]
    MusicCog._start_music_preload(me, {"url": "https://ex/a"})
    task2 = me._preload_music_cache["https://ex/a"]
    assert task1 is task2   # 不重複起 task
    task1.cancel()


def test_start_music_preload_no_url_noop():
    me = _fake_self()
    MusicCog._start_music_preload(me, {"title": "沒有 url"})
    assert me._preload_music_cache == {}


@pytest.mark.asyncio
async def test_start_music_preload_caps_cache_size():
    """一首完整解碼是幾十 MB，最多留 2 個未被領取的，超過就砍最舊的、防洩漏。"""
    me = _fake_self()
    MusicCog._start_music_preload(me, {"url": "https://ex/a"})
    MusicCog._start_music_preload(me, {"url": "https://ex/b"})
    MusicCog._start_music_preload(me, {"url": "https://ex/c"})
    assert len(me._preload_music_cache) <= 2
    for t in me._preload_music_cache.values():
        t.cancel()


# ── _resolve_music_source：有預解碼就用、沒有就退回現場建 ───────────────────

@pytest.mark.asyncio
async def test_resolve_uses_completed_preload_and_pops_cache():
    me = _fake_self()
    fut = asyncio.get_event_loop().create_future()
    fut.set_result("PRELOADED_SOURCE")
    me._preload_music_cache["https://ex/a"] = fut

    factory_called = []
    preloaded, fresh = await MusicCog._resolve_music_source(
        me, "https://ex/a", lambda: factory_called.append(1) or "FRESH")

    assert preloaded == "PRELOADED_SOURCE"
    assert fresh is None
    assert factory_called == []          # 有預解碼就不現場建
    assert "https://ex/a" not in me._preload_music_cache   # 領走就清掉


@pytest.mark.asyncio
async def test_resolve_waits_for_inflight_preload():
    """點火沒多久就換到下一首（極端情況）：preload 還沒完成，await 等它，不是直接放棄。"""
    me = _fake_self()

    async def _slow():
        await asyncio.sleep(0.01)
        return "PRELOADED_SOURCE"

    me._preload_music_cache["https://ex/a"] = asyncio.create_task(_slow())
    preloaded, fresh = await MusicCog._resolve_music_source(me, "https://ex/a", lambda: "FRESH")
    assert preloaded == "PRELOADED_SOURCE"
    assert fresh is None


@pytest.mark.asyncio
async def test_resolve_falls_back_to_fresh_when_no_cache_entry():
    me = _fake_self()
    preloaded, fresh = await MusicCog._resolve_music_source(me, "https://ex/nope", lambda: "FRESH")
    assert preloaded is None
    assert fresh == "FRESH"


@pytest.mark.asyncio
async def test_resolve_falls_back_to_fresh_when_preload_failed():
    """預解碼 ffmpeg 失敗（例如網路斷）不可讓整首歌播不出來，退回現場建。"""
    me = _fake_self()

    async def _boom():
        raise RuntimeError("ffmpeg died")

    me._preload_music_cache["https://ex/a"] = asyncio.create_task(_boom())
    preloaded, fresh = await MusicCog._resolve_music_source(me, "https://ex/a", lambda: "FRESH")
    assert preloaded is None
    assert fresh == "FRESH"
    assert "https://ex/a" not in me._preload_music_cache
