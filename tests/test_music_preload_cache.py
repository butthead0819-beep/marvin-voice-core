"""TDD: DJ Tail 點火時背景預解碼下一首音樂，蓋掉 preload_f32_source 的整首解碼延遲。

背景（2026-07-25 實測回歸）：`preload_f32_source` 消除了 Mac mixer 的中段爆音，但代價是
換源前要等整首解碼完——這段延遲原本落在「DJ 開場白講完」跟「下一首音樂真的出聲」中間，
變成聽得到的中斷。修法＝在 DJ Tail 既有的 5 秒尾段窗口點火當下，就背景先解碼好下一首，
`play_stream_song` 真正要換源時直接用解碼好的結果，零等待。

只測純邏輯（cache 記帳 + 「有預解碼就用、沒有就退回現場建」的決策），不碰真 ffmpeg/mixer
（跟 test_stream_reconnect_wait.py 同一個原則：play_stream_song 整條牽涉 ffmpeg/mixer，
不做端到端）。

2026-07-30 補：手動 skip 實測發現「下一首有 DJ 開場白」的轉場（play_stream_song 的
use_mix 分支）完全沒吃到這個 preload——那條分支自己現場組 filter_complex 音源，根本
不查 _preload_music_cache。_pick_preload_s16_source 抽出「有 DJ 音檔就疊 DJ Mix 版
preload、沒有就退回純音樂版」的純邏輯，讓 preload 也能覆蓋這條分支。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

from cogs.music_cog import MusicCog, _PRELOAD_STAGGER_S


async def _noop_measure(*_a, **_k):
    return None


def _fake_self():
    return SimpleNamespace(_preload_music_cache={}, _stream_norm_gain={},
                           stream_volume=1.0, _DJ_INTERJECTION_VOLUME=0.3,
                           _measure_norm_gain_bg=_noop_measure)


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


# ── _pick_preload_s16_source：DJ Mix 轉場也要吃到 preload ───────────────────

def test_pick_preload_source_uses_dj_mix_when_audio_exists(tmp_path):
    audio = tmp_path / "dj.mp3"
    audio.write_bytes(b"x")
    me = _fake_self()
    me._build_dj_mix_s16_source = lambda url, path: ("DJ_SRC", url, path)
    result = MusicCog._pick_preload_s16_source(me, "https://ex/a", str(audio))
    assert result == ("DJ_SRC", "https://ex/a", str(audio))


def test_pick_preload_source_falls_back_plain_when_no_dj_audio_path():
    me = _fake_self()
    me._build_dj_mix_s16_source = lambda *a: (_ for _ in ()).throw(AssertionError("不該呼叫"))
    with patch("discord.FFmpegPCMAudio", return_value="PLAIN_SRC") as m:
        result = MusicCog._pick_preload_s16_source(me, "https://ex/a", None)
    assert result == "PLAIN_SRC"
    m.assert_called_once()


def test_pick_preload_source_falls_back_plain_when_dj_audio_file_missing():
    """meta 給的 audio_path 檔案其實不存在（例如渲染失敗但欄位沒清乾淨）→ 別當 DJ Mix 用。"""
    me = _fake_self()
    me._build_dj_mix_s16_source = lambda *a: (_ for _ in ()).throw(AssertionError("不該呼叫"))
    with patch("discord.FFmpegPCMAudio", return_value="PLAIN_SRC"):
        result = MusicCog._pick_preload_s16_source(me, "https://ex/a", "/no/such/file.mp3")
    assert result == "PLAIN_SRC"


# ── _preload_next_music：跟 DJ Tail 共用 _resolve_tail_dj_meta 拿下一首 DJ 音檔 ──

@pytest.mark.asyncio
async def test_preload_next_music_passes_dj_audio_path_when_available(monkeypatch):
    me = _fake_self()
    me._resolve_tail_dj_meta = MagicMock(
        return_value=_async_return({"audio_path": "/dj/audio.mp3"}))
    calls = []
    me._start_music_preload = lambda info, dj_audio_path=None: calls.append((info, dj_audio_path))
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)
    await MusicCog._preload_next_music(me, {"url": "https://ex/a"})
    assert calls == [({"url": "https://ex/a"}, "/dj/audio.mp3")]


@pytest.mark.asyncio
async def test_preload_next_music_passes_none_when_no_dj_meta(monkeypatch):
    me = _fake_self()
    me._resolve_tail_dj_meta = MagicMock(return_value=_async_return(None))
    calls = []
    me._start_music_preload = lambda info, dj_audio_path=None: calls.append((info, dj_audio_path))
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)
    await MusicCog._preload_next_music(me, {"url": "https://ex/a"})
    assert calls == [({"url": "https://ex/a"}, None)]


@pytest.mark.asyncio
async def test_preload_next_music_staggers_before_heavy_work(monkeypatch):
    """2026-07-30 實測回歸：preload 一開播就搶跑，跟這首歌自己的 LoudNorm 三點取樣
    （_measure_norm_gain_bg，也是一開播就打）同時搶好幾條網路連線，疑似讓取樣讀到劣化
    音訊、autogain 誤判成大聲把音量壓低（使用者確認調大音量能聽到，回歸在改動之後才有）。
    修法＝preload 先讓一小段時間過去，別跟它正面搶頻寬。"""
    me = _fake_self()
    calls = []

    async def _fake_sleep(secs):
        calls.append(("sleep", secs))

    async def _fake_resolve(info):
        calls.append(("resolve", info))
        return None

    async def _fake_measure(url, info=None):
        calls.append(("measure", url, info))

    me._resolve_tail_dj_meta = _fake_resolve
    me._measure_norm_gain_bg = _fake_measure
    me._start_music_preload = lambda info, dj_audio_path=None: calls.append(("start", dj_audio_path))
    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
    await MusicCog._preload_next_music(me, {"url": "https://ex/a"})
    assert calls == [
        ("sleep", _PRELOAD_STAGGER_S),
        ("measure", "https://ex/a", {"url": "https://ex/a"}),
        ("resolve", {"url": "https://ex/a"}),
        ("start", None),
    ]


async def _instant_sleep(_secs):
    return None


# ── DJ Mix 分支接上跟非 DJ 分支同一套 autogain ──────────────────────────────
# 背景（2026-07-30 實測回歸）：DJ Mix 分支（下一首有開場白）從沒套用過
# _measure_norm_gain_bg/_stream_norm_gain 這套響度校正，只靠 ffmpeg 自己的
# single-pass loudnorm filter，準度不夠導致「二十二」這類歌明顯偏小聲。改用
# 跟非 DJ 分支一樣的預先量測常數增益，取代 ffmpeg 內建 loudnorm。

@pytest.mark.asyncio
async def test_preload_next_music_skips_measure_when_already_measured(monkeypatch):
    me = _fake_self()
    me._stream_norm_gain = {"https://ex/a": 1.5}
    calls = []

    async def _fake_sleep(_secs):
        calls.append("sleep")

    async def _fake_measure(*a, **k):
        calls.append("measure")

    async def _fake_resolve(info):
        return None

    me._measure_norm_gain_bg = _fake_measure
    me._resolve_tail_dj_meta = _fake_resolve
    me._start_music_preload = lambda info, dj_audio_path=None: None
    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
    await MusicCog._preload_next_music(me, {"url": "https://ex/a"})
    assert "measure" not in calls   # 已經量過，別重量


def test_dj_mix_volume_uses_measured_gain_when_available():
    me = _fake_self()
    me.stream_volume = 0.5
    me._stream_norm_gain = {"https://ex/a": 2.0}
    assert MusicCog._dj_mix_volume(me, "https://ex/a") == 1.0


def test_dj_mix_volume_defaults_to_one_when_unmeasured():
    me = _fake_self()
    me.stream_volume = 0.5
    me._stream_norm_gain = {}
    assert MusicCog._dj_mix_volume(me, "https://ex/a") == 0.5


@pytest.mark.asyncio
async def test_measure_norm_gain_bg_uses_explicit_info_over_current_stream_info(monkeypatch):
    """_preload_next_music 幫『下一首』(尚未成為 current) 量測時，不能誤用
    self._current_stream_info（那是現在正在播的別首歌）的 duration，否則
    sample_positions 算出來的取樣點對不上這首歌實際長度。"""
    me = _fake_self()
    me._current_stream_info = {"duration": 999}   # 別首歌，若誤用會用錯 duration
    calls = []

    class _FakeProc:
        async def communicate(self):
            return b"", b""

    async def _fake_exec(*args, **kwargs):
        calls.append(args)
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    await MusicCog._measure_norm_gain_bg(
        me, "https://ex/next", info={"duration": 60, "webpage_url": "https://ex/next"})
    assert calls   # 有嘗試量測
    ss_values = [float(a[a.index("-ss") + 1]) for a in calls]
    # duration=60 的取樣點必定 < 60；若誤用 999 會算出 >= 250 的位置
    assert all(v < 60 for v in ss_values)


async def _async_return(value):
    return value
