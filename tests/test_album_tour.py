"""TDD：/tour 專輯巡禮（docs/PLAN_audiophile_music_tour.md Phase 4.2）。

使用者定案（2026-09-28）：
  - 曲目來源：Gemini grounded 查證（fetch_album_tracklist，L1/L2 guard + 每首再過 YouTube 解析）
  - 鎖定範圍：只鎖點歌（語音 play / play_next、/marvin_play、/guide_song）；停 / 跳照常可用
  - 每首都導聆，JIT：播第 N 首時背景渲染第 N+1 首；佇列裡最多一首還沒開播的巡禮曲

不新增 IntentContext.mode——mode 一換，所有沒宣告該 mode 的 agent 全部不出價（聊天/查詢
都會死），跟「只鎖點歌」相反。改在點歌唯一咽喉 `_handle_voice_music_command` 擋。
等待用 asyncio.sleep 真讓出（busy-spin 凍 event loop 事故教訓）。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from discord import app_commands

_real_sleep = asyncio.sleep


def _make_cog():
    from cogs.music_cog import MusicCog
    bot = MagicMock()
    cog = MusicCog(bot)
    cog.radio_mode = False
    vc = MagicMock()
    vc.active_text_channel = MagicMock()
    vc.active_text_channel.send = AsyncMock()
    vc._play_ack = AsyncMock()
    cog._vc = MagicMock(return_value=vc)
    return cog, vc


def _make_interaction(username="狗與露", in_voice=True):
    inter = MagicMock()
    inter.user.display_name = username
    inter.response.defer = AsyncMock()
    msg = MagicMock()
    msg.edit = AsyncMock()
    inter.followup.send = AsyncMock(return_value=msg)
    inter.guild.voice_client = MagicMock() if in_voice else None
    return inter, msg


# ── 指令註冊與入口 ──────────────────────────────────────────────────────────

def test_tour_registered_in_commands_mixin():
    from cogs.music_cog import MusicCog
    cmd = getattr(MusicCog, "tour")
    assert isinstance(cmd, app_commands.Command)
    assert cmd.name == "tour"
    assert cmd.callback.__module__ == "cogs.music_cog_commands"


@pytest.mark.asyncio
async def test_tour_starts_runner_with_tracks():
    cog, _ = _make_cog()
    cog._fetch_album_tracks = AsyncMock(return_value=["愛在西元前", "簡單愛"])
    cog._run_album_tour = AsyncMock()
    inter, msg = _make_interaction("狗與露")

    await cog.tour.callback(cog, inter, artist="周杰倫", album="范特西")
    await _real_sleep(0)

    cog._fetch_album_tracks.assert_awaited_once_with("周杰倫", "范特西")
    cog._run_album_tour.assert_awaited_once_with("周杰倫", ["愛在西元前", "簡單愛"], "狗與露")
    assert cog._album_tour is not None
    assert cog._album_tour["album"] == "范特西"
    assert cog._album_tour_task is not None
    content = msg.edit.call_args.kwargs["content"]
    assert "愛在西元前" in content and "簡單愛" in content


@pytest.mark.asyncio
async def test_tour_no_tracks_reports_and_does_not_start():
    cog, _ = _make_cog()
    cog._fetch_album_tracks = AsyncMock(return_value=[])
    cog._run_album_tour = AsyncMock()
    inter, msg = _make_interaction()

    await cog.tour.callback(cog, inter, artist="周杰倫", album="不存在")

    cog._run_album_tour.assert_not_awaited()
    assert getattr(cog, "_album_tour", None) is None
    assert "查不到" in msg.edit.call_args.kwargs["content"]


@pytest.mark.asyncio
async def test_tour_refused_while_tour_active():
    cog, _ = _make_cog()
    cog._album_tour = {"artist": "A", "album": "B"}
    cog._fetch_album_tracks = AsyncMock()
    inter, _ = _make_interaction()

    await cog.tour.callback(cog, inter, artist="周杰倫", album="范特西")

    cog._fetch_album_tracks.assert_not_awaited()


@pytest.mark.asyncio
async def test_tour_requires_bot_in_voice():
    cog, _ = _make_cog()
    cog._fetch_album_tracks = AsyncMock()
    inter, _ = _make_interaction(in_voice=False)

    await cog.tour.callback(cog, inter, artist="周杰倫", album="范特西")

    cog._fetch_album_tracks.assert_not_awaited()


# ── 只鎖點歌 ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_album_tour_reject_only_when_tour_active():
    cog, vc = _make_cog()
    assert await cog._album_tour_reject("狗與露") is False
    vc.active_text_channel.send.assert_not_awaited()

    cog._album_tour = {"artist": "周杰倫", "album": "范特西"}
    assert await cog._album_tour_reject("狗與露") is True
    vc.active_text_channel.send.assert_awaited_once()
    assert "巡禮" in vc.active_text_channel.send.await_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("cmd", ["play", "play_next"])
async def test_voice_song_request_blocked_during_tour(cmd):
    cog, vc = _make_cog()
    cog._album_tour = {"artist": "周杰倫", "album": "范特西"}
    cog._resolve_and_prepare = AsyncMock(return_value=(None, "", "", ""))

    await cog._handle_voice_music_command("jack", "放陶喆天天", cmd)

    cog._resolve_and_prepare.assert_not_awaited()


@pytest.mark.asyncio
async def test_voice_song_request_not_blocked_without_tour():
    cog, vc = _make_cog()
    cog._resolve_and_prepare = AsyncMock(return_value=(None, "", "", ""))

    await cog._handle_voice_music_command("jack", "放陶喆天天", "play")

    cog._resolve_and_prepare.assert_awaited_once()


@pytest.mark.asyncio
async def test_voice_skip_not_blocked_during_tour():
    cog, vc = _make_cog()
    cog._album_tour = {"artist": "周杰倫", "album": "范特西"}
    cog.stream_mode = True
    cog._record_song_skip = MagicMock()

    await cog._handle_voice_music_command("jack", "", "skip")

    cog._record_song_skip.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("command,kwargs", [
    ("marvin_play", {"query": "陶喆 天天"}),
    ("guide_song", {"song_name": "陶喆 天天"}),
])
async def test_slash_song_request_blocked_during_tour(command, kwargs):
    cog, _ = _make_cog()
    cog._album_tour = {"artist": "周杰倫", "album": "范特西"}
    cog._resolve_yt_query = AsyncMock()
    cog._queue_user_song = MagicMock()
    inter, _ = _make_interaction()

    await getattr(cog, command).callback(cog, inter, **kwargs)

    cog._resolve_yt_query.assert_not_awaited()
    cog._queue_user_song.assert_not_called()


# ── JIT 巡禮 runner ─────────────────────────────────────────────────────────

def _runner_cog(events, *, unresolvable=()):
    cog, _ = _make_cog()
    cog.stream_mode = False
    cog.stream_queue = []
    cog._current_stream_info = None
    cog._album_tour = {"artist": "周杰倫", "album": "范特西"}

    async def _resolve(q):
        if any(u in q for u in unresolvable):
            return None
        return {"title": q, "url": f"https://ex/{q}", "webpage_url": f"https://yt/{q}"}

    async def _prep(info):
        events.append(("prepare", info["title"]))
        info["_audiophile_guide"] = True

    def _queue(info, **kw):
        events.append(("queue", info["title"], info.get("_lane"), info.get("requested_by")))
        cog.stream_queue.append(info)

    def _ensure():
        cog.stream_mode = True
        return True

    cog._resolve_yt_query = AsyncMock(side_effect=_resolve)
    cog._prepare_audiophile_guide = AsyncMock(side_effect=_prep)
    cog._queue_user_song = MagicMock(side_effect=_queue)
    cog._ensure_stream_loop = MagicMock(side_effect=_ensure)
    return cog


def _player_sleep(cog, events):
    """假 sleep＝模擬 stream loop：每次被叫就「播完目前這首、開播佇列下一首」。"""
    async def _sleep(d, *a, **kw):
        events.append(("sleep", d))
        if cog.stream_queue:
            cog._current_stream_info = cog.stream_queue.pop(0)
            events.append(("play", cog._current_stream_info["title"]))
        else:
            cog._current_stream_info = None
        await _real_sleep(0)
    return _sleep


def _idx(events, ev):
    return events.index(ev)


@pytest.mark.asyncio
async def test_runner_jit_renders_next_while_current_waits_and_keeps_order():
    events = []
    cog = _runner_cog(events)
    tracks = ["愛在西元前", "爸我回來了", "簡單愛"]

    with patch("asyncio.sleep", new=_player_sleep(cog, events)):
        await cog._run_album_tour("周杰倫", tracks, "狗與露")

    queued = [e for e in events if e[0] == "queue"]
    assert [q[1] for q in queued] == [f"周杰倫 {t}" for t in tracks]
    assert all(q[2] == "album_tour" and q[3] == "狗與露" for q in queued)
    # JIT：第 2 首在第 1 首開播前就先渲染好（提前一首）
    assert _idx(events, ("prepare", "周杰倫 爸我回來了")) < _idx(events, ("play", "周杰倫 愛在西元前"))
    # 但佇列裡最多一首未開播巡禮曲：第 2 首要等第 1 首開播後才入隊
    q2 = next(i for i, e in enumerate(events) if e[:2] == ("queue", "周杰倫 爸我回來了"))
    assert _idx(events, ("play", "周杰倫 愛在西元前")) < q2
    # 等待都是真的讓出（非 0 秒 busy-spin）
    assert all(e[1] > 0 for e in events if e[0] == "sleep")
    # 全部播完 → 巡禮狀態解除（鎖放開）
    assert cog._album_tour is None


@pytest.mark.asyncio
async def test_runner_skips_unresolvable_track():
    events = []
    cog = _runner_cog(events, unresolvable=("爸我回來了",))

    with patch("asyncio.sleep", new=_player_sleep(cog, events)):
        await cog._run_album_tour("周杰倫", ["愛在西元前", "爸我回來了", "簡單愛"], "狗與露")

    assert [e[1] for e in events if e[0] == "queue"] == ["周杰倫 愛在西元前", "周杰倫 簡單愛"]
    assert cog._album_tour is None


@pytest.mark.asyncio
async def test_runner_track_exception_does_not_kill_tour():
    events = []
    cog = _runner_cog(events)
    orig = cog._prepare_audiophile_guide.side_effect

    async def _boom(info):
        if "爸我回來了" in info["title"]:
            raise RuntimeError("TTS 爆了")
        await orig(info)

    cog._prepare_audiophile_guide = AsyncMock(side_effect=_boom)
    with patch("asyncio.sleep", new=_player_sleep(cog, events)):
        await cog._run_album_tour("周杰倫", ["愛在西元前", "爸我回來了", "簡單愛"], "狗與露")

    assert [e[1] for e in events if e[0] == "queue"] == ["周杰倫 愛在西元前", "周杰倫 簡單愛"]
    assert cog._album_tour is None


@pytest.mark.asyncio
async def test_runner_ends_when_stream_stops_instead_of_waiting_forever():
    """stream loop 不在了（stream_mode False）→ 佇列不會再消化，runner 別永遠等、鎖別卡死。"""
    events = []
    cog = _runner_cog(events)

    async def _dead_sleep(d, *a, **kw):
        events.append(("sleep", d))
        cog.stream_mode = False
        await _real_sleep(0)

    with patch("asyncio.sleep", new=_dead_sleep):
        await asyncio.wait_for(
            cog._run_album_tour("周杰倫", ["愛在西元前", "爸我回來了"], "狗與露"), timeout=2.0)

    assert cog._album_tour is None


# ── 停止巡禮 ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stop_album_tour_cancels_task_clears_state_and_tour_songs():
    cog, _ = _make_cog()
    task = asyncio.create_task(_real_sleep(10))
    cog._album_tour = {"artist": "周杰倫", "album": "范特西"}
    cog._album_tour_task = task
    other = {"title": "autopilot 歌", "requested_by": "Marvin自動"}
    cog.stream_queue = [{"title": "t2", "_lane": "album_tour"}, other]

    cog._stop_album_tour("測試")
    await _real_sleep(0)

    assert task.cancelled()
    assert cog._album_tour is None
    assert cog.stream_queue == [other]


def test_stop_album_tour_noop_without_tour():
    cog, _ = _make_cog()
    cog.stream_queue = [{"title": "x"}]
    cog._stop_album_tour("測試")  # 不應拋例外
    assert cog.stream_queue == [{"title": "x"}]


@pytest.mark.asyncio
async def test_stop_stream_ends_tour_even_before_stream_started():
    """巡禮剛開始、第一首還在渲染（stream_mode 還是 False）時喊停，也要收掉巡禮。"""
    cog, _ = _make_cog()
    cog.stream_mode = False
    cog._stop_album_tour = MagicMock()

    await cog.stop_stream(reason="語音指令停止")

    cog._stop_album_tour.assert_called_once()


# ── 接線 ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fetch_album_tracks_wires_shared_deps():
    cog, _ = _make_cog()
    shared = MagicMock()
    cog._song_knowledge_store = shared

    with patch("audiophile_fetcher.fetch_album_tracklist", new=AsyncMock(return_value=["A"])) as f:
        out = await cog._fetch_album_tracks("周杰倫", "范特西")

    assert out == ["A"]
    args, kw = f.await_args.args, f.await_args.kwargs
    assert args == ("周杰倫", "范特西")
    assert kw["store"] is shared
    assert kw["free_client"] is cog.bot.router.google_client
    assert kw["paid_client"] is cog.bot.router.google_paid_client
    assert kw["guard"] is not None


@pytest.mark.asyncio
async def test_runner_holds_lock_until_last_track_actually_played():
    """最後一首入隊後不能馬上放開鎖——要等它開播、播完（離開 current）才解除。"""
    events = []
    cog = _runner_cog(events)
    lock_when_playing = {}
    base = _player_sleep(cog, events)

    async def _sleep(d, *a, **kw):
        await base(d)
        if cog._current_stream_info is not None:
            lock_when_playing[cog._current_stream_info["title"]] = cog._album_tour is not None

    with patch("asyncio.sleep", new=_sleep):
        await cog._run_album_tour("周杰倫", ["愛在西元前", "簡單愛"], "狗與露")

    assert lock_when_playing.get("周杰倫 簡單愛") is True
    assert cog._album_tour is None
