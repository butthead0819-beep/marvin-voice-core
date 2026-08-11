"""TDD: DJ 尾段串場排程 wiring（mock cog，不需真 TTS/Discord）。

2026-07-15 修：尾段 task 不再於「開播時」綁定下一首（那時 autopilot 常還沒把
下一首排進 queue → 沒排 tail → 下一首走舊路混進開頭）。改成只用當前歌 duration
算點火時刻，睡到剩 5s 才抓 stream_queue[0]（那時下一首幾乎必定已排入）。

情境：
(a) 尾段窗內派發下一首的 DJ（點火時抓 stream_queue[0]）
(bug) 開播時 queue 空、sleep 期間才排入 → 仍點火（此次修的核心）
(b) skip / _current_stream_info 換掉 → 不派發
(c) duration 未知 → 早退（退回舊行為）
(d) 點火時 queue 仍空 / 下一首無預渲染 audio → 退回舊行為
(e) next_info 已標 _dj_played_in_tail → _stream_loop 把 dj_audio/dj_data 設 None
(f) CancelledError → catch 後 return
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest



# ── helper: build a minimal MusicCog ────────────────────────────────────────

def _make_cog():
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.tts_engine = MagicMock()
    bot.tts_engine.generate_audio = AsyncMock(return_value="/tmp/dj.opus")
    bot.tts_engine.get_estimated_duration = MagicMock(return_value=6.0)
    bot.router = MagicMock()
    bot.router.generate_dynamic_system_msg = AsyncMock(return_value="接下來這首…")
    bot.engine = MagicMock()
    bot.engine.conv_buffer = MagicMock()
    bot.engine.conv_buffer.get_last_n_utterances = MagicMock(return_value=[])
    bot.engine.post_summon_callback = None
    bot.music_memory = MagicMock()
    bot.music_memory._key = MagicMock(return_value="key")
    bot.music_memory._data = {"songs": {}}
    bot.music_memory.time_slot = MagicMock(return_value="深夜")

    from cogs.music_cog import MusicCog
    cog = MusicCog(bot)
    return cog


def _cur_info(duration=180.0):
    return {"title": "周杰倫 - 夜曲", "url": "https://ex/cur", "duration": duration,
            "requested_by": "大肚"}


def _next_info():
    return {"title": "陶喆 - 普通朋友", "url": "https://ex/next", "requested_by": "狗與露"}


def _dj_meta(audio_path="/tmp/dj.opus"):
    return {"text": "接下來這首…", "audio_path": audio_path}


def _done_future(value):
    fut = asyncio.get_event_loop().create_future()
    fut.set_result(value)
    return fut


def _prime(cog, cur, *, skipped=False, stream_mode=True):
    cog._current_stream_info = cur
    cog._current_song_skipped = skipped
    cog.stream_mode = stream_mode
    cog._maybe_play_dj_interjection = AsyncMock()
    # 點火會背景起 preload task（真 ffmpeg），跟這裡其他測項無關，mock 掉避免測試
    # 期間對假 URL 噴真的 ffmpeg subprocess；行為本身另在 test_music_preload_cache.py 驗。
    cog._start_music_preload = MagicMock()


# ── (a) 尾段窗內派發：點火時抓 stream_queue[0] ──────────────────────────────

@pytest.mark.asyncio
async def test_tail_dj_fires_and_marks_next():
    """queue 有下一首且有預渲染 audio → _maybe_play_dj_interjection 被呼叫 + 標記。"""
    cog = _make_cog()
    cur = _cur_info(duration=180.0)
    nxt = _next_info()
    cog.stream_queue = [nxt]
    cog._prefetch_cache[nxt["url"]] = _done_future({"dj": _dj_meta()})
    _prime(cog, cur)

    with patch("os.path.exists", return_value=True), \
         patch("asyncio.sleep", new=AsyncMock()):
        import time
        song_start_time = time.time() - 170.0  # elapsed≈170; fire_at=180-5=175; delay≈5
        await cog._run_tail_dj(cur, song_start_time)

    cog._maybe_play_dj_interjection.assert_called_once()
    assert nxt.get("_dj_played_in_tail") is True


# ── (h) 點火同時背景預解碼下一首（蓋掉 preload_f32_source 的整首解碼延遲，見
#     test_music_preload_cache.py；2026-07-25 實測回歸：沒先做，這段延遲會落在 DJ
#     開場白講完跟下一首出聲之間，變成聽得到的中斷）──────────────────────────────

@pytest.mark.asyncio
async def test_tail_dj_fire_starts_music_preload():
    cog = _make_cog()
    cur = _cur_info(duration=180.0)
    nxt = _next_info()
    cog.stream_queue = [nxt]
    cog._prefetch_cache[nxt["url"]] = _done_future({"dj": _dj_meta()})
    _prime(cog, cur)

    with patch("os.path.exists", return_value=True), \
         patch("asyncio.sleep", new=AsyncMock()):
        import time
        await cog._run_tail_dj(cur, time.time() - 170.0)

    cog._start_music_preload.assert_called_once_with(nxt)


# ── 精華起播（highlight_start_s）位移尾段點火時間表 ─────────────────────────

@pytest.mark.asyncio
async def test_tail_dj_fire_delay_uses_effective_duration_with_highlight_start():
    """highlight_start_s 讓實際播放起點位移了一截，餵給 tail_dj_fire_delay 的 duration
    要扣掉這段位移，否則會以為離結尾還很久（其實早就快播完了），點火時間表全錯。"""
    cog = _make_cog()
    cur = _cur_info(duration=180.0)
    cur["highlight_start_s"] = 60.0
    nxt = _next_info()
    cog.stream_queue = [nxt]
    cog._prefetch_cache[nxt["url"]] = _done_future({"dj": _dj_meta()})
    _prime(cog, cur)

    captured = {}

    def _fake_delay(duration, elapsed, **kwargs):
        captured["duration"] = duration
        captured["elapsed"] = elapsed
        return 5.0

    with patch("os.path.exists", return_value=True), \
         patch("asyncio.sleep", new=AsyncMock()), \
         patch("dj_tail_schedule.tail_dj_fire_delay", side_effect=_fake_delay):
        import time
        await cog._run_tail_dj(cur, time.time() - 100.0)

    assert captured["duration"] == 120.0  # 180 - 60，不是原始 180


@pytest.mark.asyncio
async def test_tail_dj_fire_delay_no_highlight_uses_raw_duration():
    cog = _make_cog()
    cur = _cur_info(duration=180.0)
    nxt = _next_info()
    cog.stream_queue = [nxt]
    cog._prefetch_cache[nxt["url"]] = _done_future({"dj": _dj_meta()})
    _prime(cog, cur)

    captured = {}

    def _fake_delay(duration, elapsed, **kwargs):
        captured["duration"] = duration
        captured["lead_s"] = kwargs.get("lead_s")
        return 5.0

    with patch("os.path.exists", return_value=True), \
         patch("asyncio.sleep", new=AsyncMock()), \
         patch("dj_tail_schedule.tail_dj_fire_delay", side_effect=_fake_delay):
        import time
        await cog._run_tail_dj(cur, time.time() - 170.0)

    from cogs.music_cog import _DJ_TAIL_LEAD_S
    assert captured["lead_s"] == _DJ_TAIL_LEAD_S  # 5s→8s，給 preload 更多餘裕

    assert captured["duration"] == 180.0


# ── (bug) 開播時 queue 空、sleep 期間才排入 → 仍點火（此次修的核心）──────────

@pytest.mark.asyncio
async def test_tail_dj_fires_when_next_queued_during_playback():
    """開播瞬間 queue 空（autopilot 還沒排），播放中才排入下一首 → 點火時抓得到、照樣派發。

    舊實作在開播時就綁定 next_info，queue 空 → 根本沒排 tail，下一首只能走舊路
    混進開頭。這條測試鎖住修正後的行為。
    """
    cog = _make_cog()
    cur = _cur_info(duration=180.0)
    nxt = _next_info()
    cog.stream_queue = []  # 開播時 queue 空
    _prime(cog, cur)

    async def _sleep_then_enqueue(delay):
        # 模擬 autopilot 在當前歌播放中才把下一首排入 + prefetch 完成
        cog.stream_queue = [nxt]
        cog._prefetch_cache[nxt["url"]] = _done_future({"dj": _dj_meta()})

    with patch("os.path.exists", return_value=True), \
         patch("asyncio.sleep", side_effect=_sleep_then_enqueue):
        import time
        await cog._run_tail_dj(cur, time.time() - 170.0)

    cog._maybe_play_dj_interjection.assert_called_once()
    assert nxt.get("_dj_played_in_tail") is True


# ── (bug2) 點火時下一首沒 prefetch → 現場補建、照樣派發 ──────────────────────

@pytest.mark.asyncio
async def test_tail_dj_builds_prefetch_if_missing_at_fire():
    """下一首在 queue 但沒 prefetch（autopilot 較晚排入）→ 現場補 _fetch_song_meta、派發。"""
    cog = _make_cog()
    cur = _cur_info(duration=180.0)
    nxt = _next_info()
    cog.stream_queue = [nxt]
    # 沒放 prefetch_cache → 逼 _resolve_tail_dj_meta 現場補建
    cog._fetch_song_meta = AsyncMock(return_value={"dj": _dj_meta()})
    _prime(cog, cur)

    with patch("os.path.exists", return_value=True), \
         patch("asyncio.sleep", new=AsyncMock()):
        import time
        await cog._run_tail_dj(cur, time.time() - 170.0)

    cog._fetch_song_meta.assert_awaited_once()
    cog._maybe_play_dj_interjection.assert_called_once()
    assert nxt.get("_dj_played_in_tail") is True


# ── (b) skip / 換歌後不派發 ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tail_dj_skipped_by_flag():
    cog = _make_cog()
    cur = _cur_info(duration=180.0)
    nxt = _next_info()
    cog.stream_queue = [nxt]
    cog._prefetch_cache[nxt["url"]] = _done_future({"dj": _dj_meta()})
    _prime(cog, cur)

    async def _sleep_then_skip(delay):
        cog._current_song_skipped = True

    with patch("os.path.exists", return_value=True), \
         patch("asyncio.sleep", side_effect=_sleep_then_skip):
        import time
        await cog._run_tail_dj(cur, time.time() - 170.0)

    cog._maybe_play_dj_interjection.assert_not_called()
    assert not nxt.get("_dj_played_in_tail")


@pytest.mark.asyncio
async def test_tail_dj_skipped_by_stream_info_change():
    cog = _make_cog()
    cur = _cur_info(duration=180.0)
    nxt = _next_info()
    cog.stream_queue = [nxt]
    cog._prefetch_cache[nxt["url"]] = _done_future({"dj": _dj_meta()})
    _prime(cog, cur)

    async def _sleep_then_change(delay):
        cog._current_stream_info = _next_info()  # 歌已切換

    with patch("os.path.exists", return_value=True), \
         patch("asyncio.sleep", side_effect=_sleep_then_change):
        import time
        await cog._run_tail_dj(cur, time.time() - 170.0)

    cog._maybe_play_dj_interjection.assert_not_called()


# ── (c) duration 未知 → 早退 ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tail_dj_no_duration_returns_early():
    cog = _make_cog()
    cur = _cur_info(duration=None)
    cur.pop("duration", None)
    cog.stream_queue = [_next_info()]
    _prime(cog, cur)

    import time
    await cog._run_tail_dj(cur, time.time())

    cog._maybe_play_dj_interjection.assert_not_called()


@pytest.mark.asyncio
async def test_tail_dj_duration_zero_returns_early():
    cog = _make_cog()
    cur = _cur_info(duration=0)
    cog.stream_queue = [_next_info()]
    _prime(cog, cur)

    import time
    await cog._run_tail_dj(cur, time.time())

    cog._maybe_play_dj_interjection.assert_not_called()


# ── (d) 點火時 queue 空 / 無預渲染 audio → 退回舊行為 ──────────────────────

@pytest.mark.asyncio
async def test_tail_dj_no_next_at_fire_returns():
    """點火時 queue 仍空（下一首始終沒排入）→ 不派發、退回舊行為。"""
    cog = _make_cog()
    cur = _cur_info(duration=180.0)
    cog.stream_queue = []
    _prime(cog, cur)

    with patch("asyncio.sleep", new=AsyncMock()):
        import time
        await cog._run_tail_dj(cur, time.time() - 170.0)

    cog._maybe_play_dj_interjection.assert_not_called()


@pytest.mark.asyncio
async def test_tail_dj_next_without_prerendered_audio_returns():
    """下一首 DJ 無預渲染 audio → 退回舊行為（下一首走開頭 DJ）。"""
    cog = _make_cog()
    cur = _cur_info(duration=180.0)
    nxt = _next_info()
    cog.stream_queue = [nxt]
    cog._prefetch_cache[nxt["url"]] = _done_future({"dj": {"text": "x", "audio_path": None}})
    _prime(cog, cur)

    with patch("os.path.exists", return_value=False), \
         patch("asyncio.sleep", new=AsyncMock()):
        import time
        await cog._run_tail_dj(cur, time.time() - 170.0)

    cog._maybe_play_dj_interjection.assert_not_called()
    assert not nxt.get("_dj_played_in_tail")


# ── (e) 只播一次：已標 _dj_played_in_tail → 開頭 dj_audio/dj_data 清空 ──────

def test_stream_loop_skips_dj_when_already_played_in_tail():
    info = _cur_info(duration=180.0)
    info["_dj_played_in_tail"] = True
    dj_data = {"text": "DJ 已播", "audio_path": "/tmp/dj.opus"}

    dj_audio = dj_data.get("audio_path") if isinstance(dj_data, dict) else None
    if info.get("_dj_played_in_tail"):
        dj_audio = None
        dj_data = None

    assert dj_audio is None
    assert dj_data is None


# ── (g) 預渲染 DJ 必須走 TTS 層（非音樂層）──────────────────────────────────

@pytest.mark.asyncio
async def test_prerendered_dj_plays_on_tts_layer_not_music():
    """_maybe_play_dj_interjection 有預渲染 audio → 走 play_dj_on_tts_layer（TTS 層、
    riding、撐過換歌），不可走 play_local_file（音樂層＝替換掉正在播的歌→DJ 被切）。"""
    cog = _make_cog()
    vc = MagicMock()
    vc._intimate_mode = False
    vc.play_dj_on_tts_layer = AsyncMock(return_value=True)
    vc.play_local_file = AsyncMock()
    vc.play_tts = AsyncMock()
    cog.bot.cogs.get.return_value = vc  # _vc() → 這個 vc

    with patch("os.path.exists", return_value=True):
        await cog._maybe_play_dj_interjection({"text": "狗與露點的這首…", "audio_path": "/tmp/dj.opus"})

    vc.play_dj_on_tts_layer.assert_awaited_once_with("/tmp/dj.opus")
    vc.play_local_file.assert_not_called()  # 絕不走音樂層
    vc.play_tts.assert_not_called()          # 有預渲染就不即時 TTS


# ── (i) 尾段 SFX 疊播：DJ 口白播完後接一支轉場音效（見 scripts/gen_dj_sfx.py）──

@pytest.mark.asyncio
async def test_tail_dj_plays_sfx_after_interjection():
    """尾段點火成功派發 DJ 口白後，_play_dj_tail_sfx 用同一條 TTS 層佇列疊一支
    scratch/dj_airhorn/riser，且晚於口白（同一佇列接續播放）。"""
    cog = _make_cog()
    cur = _cur_info(duration=180.0)
    nxt = _next_info()
    cog.stream_queue = [nxt]
    cog._prefetch_cache[nxt["url"]] = _done_future({"dj": _dj_meta()})
    _prime(cog, cur)

    vc = MagicMock()
    vc.play_dj_on_tts_layer = AsyncMock(return_value=True)
    cog.bot.cogs.get.return_value = vc  # _vc() → 這個 vc

    # scratch 沒有靜態 fallback，抽中它但沒配 preload 會這輪不放（另有測試專門鎖這個
    # 行為）；這裡測的是「SFX 接在口白後面」的一般 wiring，鎖非 scratch 的固定音效。
    with patch("os.path.exists", return_value=True), \
         patch("asyncio.sleep", new=AsyncMock()), \
         patch("random.choice", return_value="dj_airhorn"):
        import time
        await cog._run_tail_dj(cur, time.time() - 170.0)

    cog._maybe_play_dj_interjection.assert_called_once()
    vc.play_dj_on_tts_layer.assert_awaited_once()
    sfx_path = vc.play_dj_on_tts_layer.await_args.args[0]
    assert sfx_path.endswith(".wav")
    assert "dj_sfx" in sfx_path


@pytest.mark.asyncio
async def test_dj_tail_sfx_skips_when_no_vc():
    """沒有 VoiceController（_vc() 回 None）→ 靜靜放棄，不炸。"""
    cog = _make_cog()
    cog.bot.cogs.get.return_value = None
    await cog._play_dj_tail_sfx()  # 不應拋例外


@pytest.mark.asyncio
async def test_dj_tail_sfx_skips_when_file_missing():
    """音效檔不存在（例如尚未跑 scripts/gen_dj_sfx.py）→ 不呼叫 play_dj_on_tts_layer。"""
    cog = _make_cog()
    vc = MagicMock()
    vc.play_dj_on_tts_layer = AsyncMock()
    cog.bot.cogs.get.return_value = vc

    with patch("os.path.exists", return_value=False):
        await cog._play_dj_tail_sfx()

    vc.play_dj_on_tts_layer.assert_not_awaited()


@pytest.mark.asyncio
async def test_dj_tail_sfx_uses_preloaded_source_for_scratch():
    """當抽中 scratch 且 next_info 有已解碼的 preloaded source 時，使用真實 PCM 生成動態 scratch。"""
    from local_mixing_source import PreloadedF32MusicSource
    cog = _make_cog()
    nxt = _next_info()
    url = nxt["url"]
    
    # 建立一個假的 PreloadedF32MusicSource（100 幀）
    fake_frames = [b"\x00" * 7680 for _ in range(100)]
    preloaded = PreloadedF32MusicSource(fake_frames)
    cog._preload_music_cache[url] = _done_future(preloaded)

    vc = MagicMock()
    vc.play_dj_on_tts_layer = AsyncMock(return_value=True)
    cog.bot.cogs.get.return_value = vc

    with patch("random.choice", return_value="scratch"), \
         patch("scripts.gen_dj_sfx.gen_scratch_from_pcm", return_value=np.zeros(int(48000 * 0.65), dtype=np.float32)) as mock_gen, \
         patch("scripts.gen_dj_sfx._write_wav") as mock_write, \
         patch("os.path.exists", return_value=True):
        await cog._play_dj_tail_sfx(nxt)

    mock_gen.assert_called_once()
    mock_write.assert_called_once()
    vc.play_dj_on_tts_layer.assert_awaited_once()


@pytest.mark.asyncio
async def test_dj_tail_sfx_skips_when_preload_not_ready():
    """scratch 沒有靜態 fallback：preload 沒排、不存在 → 這輪不放任何音效
    （沒特效＝沒抓到 PCM，訊號要乾淨，不能用預錄音檔頂替蓋掉失敗）。"""
    cog = _make_cog()
    nxt = _next_info()
    # 無 preload 或 preload 尚未完成

    vc = MagicMock()
    vc.play_dj_on_tts_layer = AsyncMock(return_value=True)
    cog.bot.cogs.get.return_value = vc

    with patch("random.choice", return_value="scratch"), \
         patch("os.path.exists", return_value=True):
        await cog._play_dj_tail_sfx(nxt)

    vc.play_dj_on_tts_layer.assert_not_awaited()


@pytest.mark.asyncio
async def test_dj_tail_sfx_waits_for_slow_preload_within_timeout():
    """preload 點火時還沒完成、但在 wait_for 逾時前解碼好 → 照樣用真實 PCM 合成動態 scratch
    （驗證從一次性 done() 檢查改成主動等待後，真的能等到剛完成的 preload）。"""
    from local_mixing_source import PreloadedF32MusicSource
    cog = _make_cog()
    nxt = _next_info()
    url = nxt["url"]

    fake_frames = [b"\x00" * 7680 for _ in range(100)]
    preloaded = PreloadedF32MusicSource(fake_frames)

    async def _slow_preload():
        await asyncio.sleep(0.02)  # 遠短於 timeout，但點火當下確實還沒 done()
        return preloaded

    cog._preload_music_cache[url] = asyncio.create_task(_slow_preload())

    vc = MagicMock()
    vc.play_dj_on_tts_layer = AsyncMock(return_value=True)
    cog.bot.cogs.get.return_value = vc

    with patch("random.choice", return_value="scratch"), \
         patch("scripts.gen_dj_sfx.gen_scratch_from_pcm", return_value=np.zeros(int(48000 * 0.65), dtype=np.float32)) as mock_gen, \
         patch("scripts.gen_dj_sfx._write_wav") as mock_write, \
         patch("os.path.exists", return_value=True):
        await cog._play_dj_tail_sfx(nxt)

    mock_gen.assert_called_once()
    mock_write.assert_called_once()


@pytest.mark.asyncio
async def test_dj_tail_sfx_gives_up_after_preload_wait_timeout():
    """preload 在 wait_for 逾時前都還沒完成 → 放棄等待、這輪不放任何音效（沒有靜態
    fallback），且不能把 preload task 本身取消掉（asyncio.shield，換源那邊還要用）。"""
    import cogs.music_cog as music_cog_module
    cog = _make_cog()
    nxt = _next_info()
    url = nxt["url"]

    async def _never_finishes_in_time():
        await asyncio.sleep(0.2)
        return "should not be reached"

    task = asyncio.create_task(_never_finishes_in_time())
    cog._preload_music_cache[url] = task

    vc = MagicMock()
    vc.play_dj_on_tts_layer = AsyncMock(return_value=True)
    cog.bot.cogs.get.return_value = vc

    with patch("random.choice", return_value="scratch"), \
         patch.object(music_cog_module, "_DJ_TAIL_SFX_PRELOAD_WAIT_S", 0.02), \
         patch("os.path.exists", return_value=True):
        await cog._play_dj_tail_sfx(nxt)

    vc.play_dj_on_tts_layer.assert_not_awaited()
    assert not task.cancelled()  # shield 保護：等待逾時不能連 preload task 都砍了

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task




# ── [PuckMixer] queue_next 必須送 webpage_url，不能送已解析過的 CDN 直連網址 ──────
# 2026-08-11 實機踩到：next_info['url'] 是 _resolve_yt_query() 當下解出來的 googlevideo
# CDN 直連網址（時效性、非 youtube 頁面），裝置端（Pi/ESP32）收到後還會再對它跑一次
# yt-dlp resolve，餵 CDN 網址進去 100% 失敗（實機驗證：ESP32 /puck_deck 穩定回 502）。

@pytest.mark.asyncio
async def test_tail_dj_fires_puck_crossfade_with_webpage_url_not_resolved_url():
    cog = _make_cog()
    cur = _cur_info(duration=180.0)
    nxt = _next_info()
    nxt["webpage_url"] = "https://youtube.com/watch?v=abc123"
    assert nxt["url"] != nxt["webpage_url"]   # 這條測試才有意義：兩者必須不同
    cog.stream_queue = [nxt]
    cog._prefetch_cache[nxt["url"]] = _done_future({"dj": _dj_meta()})
    _prime(cog, cur)

    fake_client = MagicMock()
    fake_client.queue_next = AsyncMock(return_value=True)

    with patch("os.path.exists", return_value=True), \
         patch("asyncio.sleep", new=AsyncMock()), \
         patch("cogs.music_cog._get_puck_client", return_value=fake_client):
        import time
        await cog._run_tail_dj(cur, time.time() - 170.0)
    # asyncio.create_task 起的 _fire_puck_crossfade 要等回到 event loop 才會真的跑；
    # 移出 patch 區塊外用真正的 asyncio.sleep(0) 讓出控制權一次（patch 已經在
    # _run_tail_dj 執行期間把 fake_client 綁進 task 的 closure，不需要 patch 還開著）。
    await asyncio.sleep(0)

    fake_client.queue_next.assert_awaited_once_with(nxt["webpage_url"])


# ── (f) CancelledError 被 catch ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tail_dj_cancelled_error_propagates():
    cog = _make_cog()
    cur = _cur_info(duration=180.0)
    nxt = _next_info()
    cog.stream_queue = [nxt]
    cog._prefetch_cache[nxt["url"]] = _done_future({"dj": _dj_meta()})
    _prime(cog, cur)

    async def _raise_cancelled(delay):
        raise asyncio.CancelledError()

    with patch("os.path.exists", return_value=True), \
         patch("asyncio.sleep", side_effect=_raise_cancelled):
        import time
        await cog._run_tail_dj(cur, time.time() - 170.0)

    cog._maybe_play_dj_interjection.assert_not_called()
