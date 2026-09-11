"""TDD: PR2 clear_queue + play_next 的核心機制（2026-09-11 plan-eng-review，
outside voice round 2 修正後定版）。

見 jackhuang-main-design-queue-control-tools-pr2-20260911.md：

  _pending_stop_after_song：clear_queue 設 True + _stream_user_stopped=True；
  _stream_loop() coroutine 起始 reset False（不管誰起的新 loop，單一 reset 點，
  取代散落多處）；迴圈頂端消費一次（break，略過既有「佇列已空」收尾訊息）；
  _queue_user_song() 入隊成功時一併取消兩個旗標（併發搶救：B 點新歌蓋過 A 剛清空）。

  _play_next_insert_index：front=True 時蓋過所有既有排隊（含別人的 play_next），
  仍尊重 slot-0 安全；用 info['_play_next'] 標記做 FIFO among cut-ins（不會變 LIFO）。

  _queue_user_song(info, front=False)：不做獨立插入路徑——front=True 只換插入
  位置演算法，dedup/ledger/tail bookkeeping 全部共用同一份程式碼（outside voice
  #3/#5 的統一修法）。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _u(name, vid):
    return {"title": vid, "requested_by": name,
            "webpage_url": f"https://youtu.be/{vid}", "url": "x"}


def _m(vid):
    return {"title": vid, "requested_by": "Marvin推薦（為showay）",
            "webpage_url": f"https://youtu.be/{vid}", "url": "x"}


def _make_cog():
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.tts_engine = MagicMock()
    # MagicMock, 不是 None：_stream_loop 的 record_play 走 hasattr(bot,'music_memory')
    # 判斷（值是 None 也算 True），None.record_play() 會炸——用 MagicMock 讓它安全吃下。
    bot.music_memory = MagicMock()
    from cogs.music_cog import MusicCog
    cog = MusicCog(bot)
    # play_stream_song mock 立即 resolve → _played_s≈0 < _MIN_HEALTHY_PLAY_S(3.0)
    # 會觸發既有 403 短播重試邏輯，走到真正的 _resolve_yt_query（真網路呼叫）——
    # 單元測試絕不能碰真網路，直接 mock 掉讓重試路徑安全 no-op。
    cog._resolve_yt_query = AsyncMock(return_value=None)
    return cog


def _done_future(value):
    fut = asyncio.get_event_loop().create_future()
    fut.set_result(value)
    return fut


# ── _play_next_insert_index：純函式 ─────────────────────────────────────────

def test_play_next_index_empty_queue_is_zero():
    cog = _make_cog()
    assert cog._play_next_insert_index([]) == 0


def test_play_next_index_not_streaming_is_zero():
    cog = _make_cog()
    cog.stream_mode = False
    assert cog._play_next_insert_index([_m("1")]) == 0


def test_play_next_index_streaming_nonempty_is_one():
    cog = _make_cog()
    cog.stream_mode = True
    assert cog._play_next_insert_index([_m("1"), _m("2")]) == 1


def test_play_next_index_skips_existing_play_next_run_fifo():
    """已經有 2 首 play_next 插入的歌在 index 1/2 → 第三次插在 index 3，不是搶到最前
    （FIFO among cut-ins，不會變 LIFO，見 outside voice #4）。"""
    cog = _make_cog()
    cog.stream_mode = True
    q = [_m("head"), {**_u("a", "A"), "_play_next": True},
         {**_u("b", "B"), "_play_next": True}, _m("tail")]
    assert cog._play_next_insert_index(q) == 3


# ── _queue_user_song(front=True) 整合：插到最前、蓋過既有排隊、仍是 slot-0 安全 ──

def test_queue_user_song_front_true_inserts_at_index_one_when_streaming():
    cog = _make_cog()
    cog.stream_mode = True
    cog.stream_queue = [_m("m1")]
    cog._queue_user_song(_u("jack", "X"), front=True)
    assert [s["title"] for s in cog.stream_queue] == ["m1", "X"]
    assert cog.stream_queue[1].get("_play_next") is True


def test_queue_user_song_front_true_jumps_ahead_of_regular_user_song():
    """front=True 蓋過一般點歌（不只蓋過 autopilot）——這是它跟一般 play 的差異。"""
    cog = _make_cog()
    cog.stream_mode = True
    cog.stream_queue = [_m("m1")]
    cog._queue_user_song(_u("jack", "A"))              # 一般 play → 排在 m1 之後
    cog._queue_user_song(_u("showay", "B"), front=True)  # play_next → 蓋過 A
    titles = [s["title"] for s in cog.stream_queue]
    assert titles == ["m1", "B", "A"], f"實際 {titles}"


def test_queue_user_song_front_and_regular_share_dedup_ledger():
    """front=True 跟一般 play 共用同一個 _req_ledger 去重（outside voice #5：
    play_next 不能繞過既有 30s 同人同曲防護）。extract_video_id 要求真 11 碼
    videoId 才會命中去重比對，不能用 _u() 預設的單字元假 id。"""
    cog = _make_cog()
    cog.stream_mode = True
    cog.stream_queue = []
    song = {"title": "A", "requested_by": "jack",
            "webpage_url": "https://youtu.be/ABCDEFGHIJK", "url": "x"}
    cog._queue_user_song(dict(song), front=True)
    before = len(cog.stream_queue)
    cog._queue_user_song(dict(song), front=True)  # 30s 內同人同曲重複 play_next
    assert len(cog.stream_queue) == before, "應被 _req_ledger 擋下，不重插"


# ── _pending_stop_after_song / _stream_user_stopped 生命週期 ────────────────

def test_queue_user_song_cancels_pending_stop_and_watchdog_suppression():
    """併發搶救（outside voice #1+#2）：A 清空後 B 立刻點歌，兩個旗標都要清，
    不能只清 _pending_stop_after_song 漏了 _stream_user_stopped（否則 watchdog
    永久啞掉，2026-08-01 事故重演）。"""
    cog = _make_cog()
    cog.stream_mode = True
    cog.stream_queue = []
    cog._pending_stop_after_song = True
    cog._stream_user_stopped = True
    cog._queue_user_song(_u("jack", "A"))
    assert cog._pending_stop_after_song is False
    assert cog._stream_user_stopped is False


@pytest.mark.asyncio
async def test_stream_loop_resets_pending_stop_on_fresh_start():
    """跨 session 殘留 True（例如上次 clear_queue 剛好卡在很邊緣的時機）不該讓
    新起的 _stream_loop() 一首播完就莫名停止。"""
    cog = _make_cog()
    cog.play_stream_song = AsyncMock()
    cog._auto_recommend = AsyncMock()
    cog._last_resort_replay = AsyncMock(return_value=False)
    cog._pending_stop_after_song = True  # 殘留的舊旗標
    song = _u("jack", "A")
    cog.stream_queue = [song]
    cog.stream_mode = True
    cog._prefetch_cache[song["url"]] = _done_future(None)

    with patch("cogs.music_cog._get_puck_client", return_value=None):
        await cog._stream_loop()

    # 沒被舊旗標誤停：正常播完一首後因佇列空 + 無 autopilot 補位而 break（自然結束），
    # 不是被 pending-stop 攔在第一首之前。play_stream_song 應該真的被呼叫過一次。
    cog.play_stream_song.assert_awaited_once()


@pytest.mark.asyncio
async def test_stream_loop_pending_stop_true_breaks_after_current_song():
    cog = _make_cog()
    # flag 要在 _stream_loop() 已經跑起來之後才設（模擬 clear_queue 在歌播放中被
    # 呼叫）——若在呼叫前就設，會被 _stream_loop() 開頭的 fresh-start reset 洗掉，
    # 那是另一條測試（見 test_stream_loop_resets_pending_stop_on_fresh_start）。
    async def _play_then_set_pending_stop(*a, **kw):
        cog._pending_stop_after_song = True
    cog.play_stream_song = AsyncMock(side_effect=_play_then_set_pending_stop)
    cog._auto_recommend = AsyncMock()
    cog._last_resort_replay = AsyncMock(return_value=False)
    song = _u("jack", "A")
    cog.stream_queue = [song, _u("jack", "B")]  # 佇列裡還有第二首
    cog.stream_mode = True
    cog._prefetch_cache[song["url"]] = _done_future(None)

    with patch("cogs.music_cog._get_puck_client", return_value=None):
        await cog._stream_loop()

    # 只播了第一首（A）就停，B 沒被播到——pending-stop 在第二輪迴圈頂端攔下。
    cog.play_stream_song.assert_awaited_once()
    assert cog.stream_mode is False
    assert cog._pending_stop_after_song is False  # 消費後歸位


@pytest.mark.asyncio
async def test_stream_loop_pending_stop_skips_duplicate_queue_empty_message():
    """pending-stop 觸發的收尾不該跟 clear_queue 自己的 ack 重複發「佇列已空」
    （outside voice round2 #6）。flag 一樣要在 loop 起來後才設，理由同上一條測試。"""
    cog = _make_cog()
    async def _play_then_set_pending_stop(*a, **kw):
        cog._pending_stop_after_song = True
    cog.play_stream_song = AsyncMock(side_effect=_play_then_set_pending_stop)
    cog._auto_recommend = AsyncMock()
    cog._last_resort_replay = AsyncMock(return_value=False)
    song = _u("jack", "A")
    cog.stream_queue = [song]
    cog.stream_mode = True
    cog._prefetch_cache[song["url"]] = _done_future(None)

    vc = MagicMock()
    vc.active_text_channel = AsyncMock()
    vc.active_text_channel.send = AsyncMock()
    vc.voice_client = None
    vc.get_online_members = MagicMock(return_value=[])
    vc.stt_logger = MagicMock()
    cog.bot.cogs.get.return_value = vc

    with patch("cogs.music_cog._get_puck_client", return_value=None):
        await cog._stream_loop()

    # send 會被叫（貼歌曲卡是合法的，song A 真的播了）——要擋的只是「佇列已空」
    # 那則重複的收尾文字訊息，不是全部 send 呼叫。
    texts = [c.args[0] for c in vc.active_text_channel.send.call_args_list if c.args]
    assert not any("佇列已空" in t for t in texts)
