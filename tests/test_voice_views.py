"""Tests for cogs/voice_views.py — PlayControlView + ConsentView 抽離後行為等價。

涵蓋（Phase 0 — UI views characterization）：
  - PlayControlView 建構 + _build_embed render
  - 各 button callback 狀態機行為（pause / vol / prev / next / jump / delete / on_select）
  - on_timeout 停用所有 item + 自 controller._active_views 移除（T2 ref release）
  - ConsentView accept / decline 寫 consent + 非目標成員被擋
"""
from __future__ import annotations

import weakref

import discord
import pytest
from unittest.mock import AsyncMock, MagicMock

from cogs.voice_views import ConsentView, PlayControlView


def _fake_controller(**overrides):
    c = MagicMock()
    c._active_views = weakref.WeakSet()
    c.stream_queue = []
    c.stream_history = []
    c.stream_paused = False
    c.stream_mode = False
    c.stream_volume = 0.50
    c._current_stream_info = None
    c._current_stream_comment = None
    c._current_lyrics = None
    c._plan12 = False   # 預設測舊路徑（避免 MagicMock 把 getattr(_plan12) 當 truthy）
    c._mixer = None
    for k, v in overrides.items():
        setattr(c, k, v)
    return c


def _fake_interaction(playing=True, values=None):
    interaction = MagicMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    vc = MagicMock()
    vc.is_playing.return_value = playing
    interaction.guild.voice_client = vc
    interaction.data = {"values": values or ["0"]}
    return interaction


def _song(title="A"):
    return {"title": title, "uploader": "u", "duration": 100, "requested_by": ""}


# ── PlayControlView render ───────────────────────────────────────────────────

def test_play_control_view_builds_embed_when_idle():
    view = PlayControlView(_fake_controller())
    embed = view._build_embed()
    assert isinstance(embed, discord.Embed)
    assert embed.title == "🎛️ 串流控制台"


# ── PlayControlView button state machine ─────────────────────────────────────

@pytest.mark.asyncio
async def test_pause_resume_button_pauses_when_playing():
    c = _fake_controller(stream_paused=False)
    view = PlayControlView(c)
    interaction = _fake_interaction()
    await view.pause_resume_button.callback(interaction)
    assert interaction.guild.voice_client.pause.called
    assert c.stream_paused is True


@pytest.mark.asyncio
async def test_pause_resume_button_resumes_when_paused():
    c = _fake_controller(stream_paused=True)
    view = PlayControlView(c)
    interaction = _fake_interaction()
    await view.pause_resume_button.callback(interaction)
    assert interaction.guild.voice_client.resume.called
    assert c.stream_paused is False


@pytest.mark.asyncio
async def test_vol_down_button_decreases_volume():
    c = _fake_controller(stream_volume=0.50)
    view = PlayControlView(c)
    await view.vol_down_button.callback(_fake_interaction())
    assert c.stream_volume == 0.45   # 按鈕步進 5%（2026-06-04）


@pytest.mark.asyncio
async def test_vol_up_button_increases_volume():
    c = _fake_controller(stream_volume=0.50)
    view = PlayControlView(c)
    await view.vol_up_button.callback(_fake_interaction())
    assert c.stream_volume == 0.55   # 按鈕步進 5%（2026-06-04）


@pytest.mark.asyncio
async def test_prev_button_without_history_sends_message():
    c = _fake_controller(stream_history=[])
    view = PlayControlView(c)
    interaction = _fake_interaction()
    await view.prev_button.callback(interaction)
    assert interaction.response.send_message.called


@pytest.mark.asyncio
async def test_next_button_without_stream_sends_message():
    c = _fake_controller(stream_mode=False)
    view = PlayControlView(c)
    interaction = _fake_interaction()
    await view.next_button.callback(interaction)
    assert interaction.response.send_message.called


@pytest.mark.asyncio
async def test_jump_button_without_selection_sends_message():
    view = PlayControlView(_fake_controller())
    interaction = _fake_interaction()
    await view.jump_button.callback(interaction)
    assert interaction.response.send_message.called


@pytest.mark.asyncio
async def test_delete_button_removes_selected_song_from_queue():
    queue = [_song("A"), _song("B"), _song("C")]
    c = _fake_controller(stream_queue=queue)
    view = PlayControlView(c)
    view._selected_index = 1
    await view.delete_button.callback(_fake_interaction())
    assert [s["title"] for s in c.stream_queue] == ["A", "C"]
    assert view._selected_index is None


# ── 🙈 誤點抹除按鈕 ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_misclick_button_erases_current_from_memory_and_skips():
    cur = {"title": "手滑點到的歌", "uploader": "某藝人",
           "webpage_url": "https://youtu.be/dQw4w9WgXcQ"}
    c = _fake_controller(stream_mode=True, _current_stream_info=cur)
    view = PlayControlView(c)
    interaction = _fake_interaction(playing=True)
    await view.misclick_button.callback(interaction)
    # 反向抵銷 record_play + 加永久黑名單
    c.bot.music_memory.undo_play.assert_called_once_with(cur)
    c.bot.music_memory.record_skipped_video_id.assert_called_once_with(
        "https://youtu.be/dQw4w9WgXcQ")
    # 跳到下一首（舊路徑：vc.stop_playing）
    assert interaction.guild.voice_client.stop_playing.called


@pytest.mark.asyncio
async def test_misclick_button_noop_when_nothing_playing():
    c = _fake_controller(stream_mode=False, _current_stream_info=None)
    view = PlayControlView(c)
    interaction = _fake_interaction()
    await view.misclick_button.callback(interaction)
    assert interaction.response.send_message.called
    assert not c.bot.music_memory.undo_play.called


@pytest.mark.asyncio
async def test_on_select_sets_selected_index():
    view = PlayControlView(_fake_controller())
    interaction = _fake_interaction(values=["2"])
    await view._on_select(interaction)
    assert view._selected_index == 2
    assert interaction.response.defer.called


# ── on_timeout (T2 ref release) ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_on_timeout_disables_items_and_releases_ref():
    c = _fake_controller()
    view = PlayControlView(c)
    assert view in c._active_views
    await view.on_timeout()
    assert all(item.disabled for item in view.children)
    assert view not in c._active_views


# ── ConsentView ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_consent_accept_records_consent_true():
    cm = MagicMock()
    view = ConsentView(cm, "Alice")
    interaction = _fake_interaction()
    interaction.user.display_name = "Alice"
    await view.accept.callback(interaction)
    cm.set_consent.assert_called_once_with("Alice", True)
    assert interaction.response.edit_message.called


@pytest.mark.asyncio
async def test_consent_decline_records_consent_false():
    cm = MagicMock()
    view = ConsentView(cm, "Alice")
    interaction = _fake_interaction()
    interaction.user.display_name = "Alice"
    await view.decline.callback(interaction)
    cm.set_consent.assert_called_once_with("Alice", False)
    assert interaction.response.edit_message.called


@pytest.mark.asyncio
async def test_consent_non_owner_click_blocked():
    cm = MagicMock()
    view = ConsentView(cm, "Alice")
    interaction = _fake_interaction()
    interaction.user.display_name = "Bob"
    await view.accept.callback(interaction)
    assert not cm.set_consent.called
    assert interaction.response.send_message.called
