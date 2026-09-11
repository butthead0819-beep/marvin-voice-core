"""TDD: PR2 T3 — `_resolve_and_prepare` 抽取 + `cmd=="play"` 改用它（REGRESSION：
UX 時序/內容零改動）+ 新增 `cmd=="play_next"` / `cmd=="clear_queue"`。

見 jackhuang-main-design-queue-control-tools-pr2-20260911.md。既有
tests/test_music_command_dedup.py 只驗證呼叫次數/dedup，沒有任何測試斷言
「🔍 正在搜尋」狀態訊息的內容或時序（outside voice round2 #6 的盲區）——這裡補上。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_cog():
    """比照 tests/test_music_command_dedup.py::_make_cog。"""
    bot = MagicMock()
    bot.guilds = []
    bot.music_memory = None
    _discord_vc = MagicMock()
    _discord_vc.is_connected.return_value = True
    bot.voice_clients = [_discord_vc]

    vc_mock = MagicMock()
    placeholder = MagicMock()
    placeholder.edit = AsyncMock()
    placeholder.delete = AsyncMock()
    vc_mock.active_text_channel = AsyncMock()
    vc_mock.active_text_channel.send = AsyncMock(return_value=placeholder)
    vc_mock.stt_logger = MagicMock()
    vc_mock._play_ack = AsyncMock()
    vc_mock._extract_music_search_query = MagicMock(return_value="陶喆天天")
    vc_mock._mixer = None

    def _cogs_get(name):
        if name == 'VoiceController':
            return vc_mock
        return None

    bot.cogs.get.side_effect = _cogs_get

    from cogs.music_cog import MusicCog
    cog = MusicCog(bot)
    cog.stream_mode = False
    cog.radio_mode = False
    cog.stream_queue = []
    cog.stream_history = []
    cog._vc_mock = vc_mock
    return cog


# ── cmd=="play"：REGRESSION，UX 內容/時序零改動 ─────────────────────────────

@pytest.mark.asyncio
async def test_play_shows_searching_status_before_resolve_with_corrected_text():
    """「🔍 正在搜尋」要在 _resolve_yt_query 開始前就顯示（不是 resolve 完才補發），
    且文字是修正後的搜尋詞，不是原始語音誤字——這是 _resolve_and_prepare 加
    on_query_resolved callback 要保住的既有 UX（outside voice round2 #6）。"""
    cog = _make_cog()
    vc = cog._vc_mock
    order: list[str] = []

    async def _fake_resolve(search):
        order.append(f"resolve:{search}")
        return {"title": "天天", "url": "u", "webpage_url": "w", "duration": 100}
    cog._resolve_yt_query = AsyncMock(side_effect=_fake_resolve)

    real_send = vc.active_text_channel.send

    async def _tracking_send(*a, **kw):
        if a and isinstance(a[0], str) and "正在搜尋" in a[0]:
            order.append(f"status:{a[0]}")
        return await real_send(*a, **kw)
    vc.active_text_channel.send = AsyncMock(side_effect=_tracking_send)

    await cog._handle_voice_music_command("jack", "放陶喆天天", "play")

    assert order[0].startswith("status:")
    assert "陶喆天天" in order[0]
    assert order[1] == "resolve:陶喆天天"


@pytest.mark.asyncio
async def test_play_shows_correction_note_when_stt_corrected():
    cog = _make_cog()
    vc = cog._vc_mock
    cog._resolve_yt_query = AsyncMock(
        return_value={"title": "天天", "url": "u", "webpage_url": "w", "duration": 100})
    mm = MagicMock()
    mm.apply_stt_correction = MagicMock(return_value=("陶喆天天", "陶太天天"))
    cog.bot.music_memory = mm

    await cog._handle_voice_music_command("jack", "放陶太天天", "play")

    texts = [c.args[0] for c in vc.active_text_channel.send.call_args_list if c.args]
    assert any("語音修正" in t and "陶太天天" in t and "陶喆天天" in t for t in texts)


@pytest.mark.asyncio
async def test_play_not_found_shows_error_and_fail_ack():
    cog = _make_cog()
    vc = cog._vc_mock
    cog._resolve_yt_query = AsyncMock(return_value=None)

    await cog._handle_voice_music_command("jack", "放陶喆天天", "play")

    placeholder = vc.active_text_channel.send.return_value
    placeholder.edit.assert_awaited_once()
    assert "找不到" in placeholder.edit.await_args.kwargs.get("content", "")
    vc._play_ack.assert_called_with("music_fail", speaker="jack")


@pytest.mark.asyncio
async def test_play_empty_query_asks_what_song():
    cog = _make_cog()
    vc = cog._vc_mock
    vc._extract_music_search_query = MagicMock(return_value="")
    cog._resolve_yt_query = AsyncMock()

    await cog._handle_voice_music_command("jack", "放", "play")

    texts = [c.args[0] for c in vc.active_text_channel.send.call_args_list if c.args]
    assert any("要放什麼歌" in t for t in texts)
    cog._resolve_yt_query.assert_not_awaited()


@pytest.mark.asyncio
async def test_play_success_still_queues_and_ensures_loop():
    cog = _make_cog()
    cog._resolve_yt_query = AsyncMock(
        return_value={"title": "天天", "url": "u", "webpage_url": "w", "duration": 100})

    await cog._handle_voice_music_command("jack", "放陶喆天天", "play")

    assert [s["title"] for s in cog.stream_queue] == ["天天"]
    assert cog.stream_mode is True  # _ensure_stream_loop 真的起了 loop


# ── cmd=="play_next" ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_play_next_inserts_front_true():
    cog = _make_cog()
    cog.stream_mode = True
    cog.stream_queue = [{"title": "既有歌", "requested_by": "Marvin推薦", "url": "m",
                          "webpage_url": "wm"}]
    cog._resolve_yt_query = AsyncMock(
        return_value={"title": "插播歌", "url": "u2", "webpage_url": "w2", "duration": 100})

    await cog._handle_voice_music_command("showay", "插播周杰倫稻香", "play_next")

    titles = [s["title"] for s in cog.stream_queue]
    assert titles == ["既有歌", "插播歌"], f"應插在 slot 1（stream_mode 安全位），實際 {titles}"
    assert cog.stream_queue[1].get("_play_next") is True


@pytest.mark.asyncio
async def test_play_next_not_found_acks_failure():
    cog = _make_cog()
    vc = cog._vc_mock
    cog._resolve_yt_query = AsyncMock(return_value=None)

    await cog._handle_voice_music_command("showay", "插播不存在的歌", "play_next")

    texts = [c.args[0] for c in vc.active_text_channel.send.call_args_list if c.args]
    assert any("找不到" in t for t in texts)
    vc._play_ack.assert_called_with("music_fail", speaker="showay")


@pytest.mark.asyncio
async def test_play_next_empty_query_asks_what_song():
    cog = _make_cog()
    vc = cog._vc_mock
    vc._extract_music_search_query = MagicMock(return_value="")
    cog._resolve_yt_query = AsyncMock()

    await cog._handle_voice_music_command("showay", "插播", "play_next")

    texts = [c.args[0] for c in vc.active_text_channel.send.call_args_list if c.args]
    assert any("要插播什麼歌" in t for t in texts)
    cog._resolve_yt_query.assert_not_awaited()


# ── cmd=="clear_queue" ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_clear_queue_while_streaming_sets_pending_stop_and_acks():
    cog = _make_cog()
    vc = cog._vc_mock
    cog.stream_mode = True
    cog.stream_queue = [{"title": "a", "requested_by": "Marvin推薦", "url": "u", "webpage_url": "w"}]
    cog._personal_shuffle = {"user": "jack", "remaining": []}

    await cog._handle_voice_music_command("jack", "", "clear_queue")

    assert cog.stream_queue == []
    assert cog._pending_stop_after_song is True
    assert cog._stream_user_stopped is True
    assert cog._personal_shuffle is None
    texts = [c.args[0] for c in vc.active_text_channel.send.call_args_list if c.args]
    assert any("放完就停" in t for t in texts)


@pytest.mark.asyncio
async def test_clear_queue_when_idle_and_empty_says_already_empty():
    cog = _make_cog()
    vc = cog._vc_mock
    cog.stream_mode = False
    cog.stream_queue = []

    await cog._handle_voice_music_command("jack", "", "clear_queue")

    texts = [c.args[0] for c in vc.active_text_channel.send.call_args_list if c.args]
    assert any("本來就是空的" in t for t in texts)
    assert cog._pending_stop_after_song is False  # 沒在播，不該誤設
