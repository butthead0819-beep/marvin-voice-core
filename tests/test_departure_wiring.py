"""送客改「人還在時先送」+ 回台簡單招呼——cog 接線測試。

慣例比照 tests/test_bridge_wiring.py：MagicMock(spec=VoiceController) + 直接呼叫
unbound 的 VoiceController.on_voice_state_update / ProactiveSocialMixin 方法。
"""
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cogs.voice_controller import VoiceController
from tts_speak_policy import SpeakKind


def _make_cog():
    cog = MagicMock(spec=VoiceController)
    cog.bot = MagicMock()
    cog.bot.user = MagicMock()
    cog.bot.user.id = 99999
    cog.consent = MagicMock()
    cog.consent.has_seen_notice.return_value = True
    cog._nudges = MagicMock()
    cog.active_text_channel = None
    cog.greeting_cooldown = {}
    cog.stream_mode = False
    cog.stt_logger = MagicMock()
    cog.departure_stats = MagicMock()
    cog.departure_stats.record_departure = AsyncMock()
    cog._last_leave_ts = {}
    cog._departure_predictor = MagicMock()
    cog.bot.router = MagicMock()
    cog.bot.router.generate_player_greeting = AsyncMock(return_value="hi")
    cog.bot.router.generate_player_farewell = AsyncMock(return_value="bye")
    cog.play_tts = AsyncMock()
    cog.speak = AsyncMock()
    cog._send_mood_sticker = AsyncMock()
    cog.handle_dismiss = AsyncMock()
    cog._delayed_player_greeting = AsyncMock()
    return cog


def _member(id_=12345, name="Jack"):
    member = MagicMock()
    member.id = id_
    member.display_name = name
    member.guild = MagicMock()
    return member


async def _run_listener(cog, member, before_channel, after_channel):
    listener_fn = VoiceController.on_voice_state_update
    before = MagicMock(); before.channel = before_channel
    after = MagicMock(); after.channel = after_channel
    await listener_fn(cog, member, before, after)


# ── 9. leave（房內還有別人）：不送客、不 speak，記 _last_leave_ts + on_leave ──

@pytest.mark.asyncio
async def test_leave_with_others_present_does_not_farewell(monkeypatch):
    cog = _make_cog()

    channel_a = MagicMock()
    other = MagicMock(bot=False)
    channel_a.members = [other]
    voice_client = MagicMock()
    voice_client.channel = channel_a
    cog.bot.voice_clients = [voice_client]

    import discord
    monkeypatch.setattr(discord.utils, "get", lambda iterable, **kw: voice_client)

    member = _member()
    await _run_listener(cog, member, channel_a, None)

    cog.bot.router.generate_player_farewell.assert_not_called()
    cog.speak.assert_not_called()
    assert member.id in cog._last_leave_ts
    cog._departure_predictor.on_leave.assert_called_once()
    assert cog._departure_predictor.on_leave.call_args[0][0] == "Jack"


# ── 10. rejoin action 決定要不要排招呼 ──────────────────────────────────────

@pytest.mark.asyncio
async def test_rejoin_within_flap_window_skips_greeting(monkeypatch):
    cog = _make_cog()
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now)

    member = _member()
    cog._last_leave_ts[member.id] = now - 60  # 60s < FLAP_S(120)

    channel_a = MagicMock()
    channel_a.members = [MagicMock(bot=False)]
    voice_client = MagicMock()
    voice_client.channel = channel_a
    cog.bot.voice_clients = [voice_client]

    import discord
    monkeypatch.setattr(discord.utils, "get", lambda iterable, **kw: voice_client)

    await _run_listener(cog, member, None, channel_a)

    cog._delayed_player_greeting.assert_not_called()


@pytest.mark.asyncio
async def test_rejoin_after_flap_but_within_welcome_back_window(monkeypatch):
    cog = _make_cog()
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now)

    member = _member()
    cog._last_leave_ts[member.id] = now - 300  # 120s <= gap < 3600s

    channel_a = MagicMock()
    channel_a.members = [MagicMock(bot=False)]
    voice_client = MagicMock()
    voice_client.channel = channel_a
    cog.bot.voice_clients = [voice_client]

    import discord
    monkeypatch.setattr(discord.utils, "get", lambda iterable, **kw: voice_client)

    await _run_listener(cog, member, None, channel_a)

    cog._delayed_player_greeting.assert_called_once()
    _, kwargs = cog._delayed_player_greeting.call_args
    assert kwargs.get("welcome_back") is True


@pytest.mark.asyncio
async def test_join_without_leave_record_is_full_greeting(monkeypatch):
    cog = _make_cog()

    member = _member()  # 沒有預填 _last_leave_ts

    channel_a = MagicMock()
    channel_a.members = [MagicMock(bot=False)]
    voice_client = MagicMock()
    voice_client.channel = channel_a
    cog.bot.voice_clients = [voice_client]

    import discord
    monkeypatch.setattr(discord.utils, "get", lambda iterable, **kw: voice_client)

    await _run_listener(cog, member, None, channel_a)

    cog._delayed_player_greeting.assert_called_once()
    _, kwargs = cog._delayed_player_greeting.call_args
    assert kwargs.get("welcome_back") is False


# ── 11. _delayed_player_greeting(welcome_back=True) → 簡單招呼句 ────────────

@pytest.mark.asyncio
async def test_delayed_player_greeting_welcome_back_says_simple_line():
    cog = _make_cog()
    cog._delayed_player_greeting = VoiceController._delayed_player_greeting.__get__(cog)

    member = _member(name="Jack")
    channel = MagicMock()
    channel.members = [member]
    voice_client = MagicMock()
    voice_client.guild = member.guild
    voice_client.channel = channel
    cog.bot.voice_clients = [voice_client]
    cog._mixer = MagicMock()
    cog._mixer._tts_gain = 0.1
    cog._mixer._tts_load_samples.return_value = 0

    import discord
    with patch.object(discord.utils, "get", lambda iterable, **kw: voice_client):
        await cog._delayed_player_greeting(member, channel, delay_sec=0, welcome_back=True)

    cog.speak.assert_awaited_once_with("Jack，你回來啦", proactive=True, kind=SpeakKind.JOIN_GREETING)
    cog.bot.router.generate_player_greeting.assert_not_called()


# ── 12. _watch_departure ─────────────────────────────────────────────────────

def test_watch_departure_observe_true_schedules_farewell(monkeypatch):
    from cogs.voice_controller_social import ProactiveSocialMixin

    cog = MagicMock()
    cog._departure_predictor = MagicMock()
    cog._departure_predictor.observe.return_value = True

    created = []
    monkeypatch.setattr(asyncio, "create_task", lambda coro: created.append(coro) or coro)

    ProactiveSocialMixin._watch_departure(cog, "Jack", "我先下線了", 123.0)

    cog._departure_predictor.observe.assert_called_once_with("Jack", "我先下線了", 123.0)
    cog._departure_predictor.mark_farewelled.assert_called_once_with("Jack", 123.0)
    assert len(created) == 1
    created[0].close()


def test_watch_departure_observe_false_does_nothing(monkeypatch):
    from cogs.voice_controller_social import ProactiveSocialMixin

    cog = MagicMock()
    cog._departure_predictor = MagicMock()
    cog._departure_predictor.observe.return_value = False

    created = []
    monkeypatch.setattr(asyncio, "create_task", lambda coro: created.append(coro) or coro)

    ProactiveSocialMixin._watch_departure(cog, "Jack", "今天天氣不錯", 123.0)

    cog._departure_predictor.mark_farewelled.assert_not_called()
    assert created == []


# ── 13. _speak_departure_farewell 用 DEPARTURE_FAREWELL kind ─────────────────

@pytest.mark.asyncio
async def test_speak_departure_farewell_uses_departure_farewell_kind():
    from cogs.voice_controller_social import ProactiveSocialMixin

    cog = MagicMock()
    cog._departure_predictor = MagicMock()
    cog._departure_predictor.precision.return_value = (10, 0.8)
    cog.bot.router.generate_player_farewell = AsyncMock(return_value="bye")
    cog.stream_mode = False
    cog.active_text_channel = None
    cog.stt_logger = MagicMock()
    cog.speak = AsyncMock()

    await ProactiveSocialMixin._speak_departure_farewell(cog, "Jack", "我先下線了")

    cog.speak.assert_awaited_once_with("bye", proactive=True, kind=SpeakKind.DEPARTURE_FAREWELL)


# ── 14. _handle_wake_farewell recently_farewelled 去重 ───────────────────────

@pytest.mark.asyncio
async def test_handle_wake_farewell_skips_when_recently_farewelled():
    from cogs.voice_controller_social import ProactiveSocialMixin

    cog = MagicMock()
    cog._departure_predictor = MagicMock()
    cog._departure_predictor.recently_farewelled.return_value = True
    cog.bot.router.generate_player_farewell = AsyncMock(return_value="bye")

    await ProactiveSocialMixin._handle_wake_farewell(cog, "Jack")

    cog.bot.router.generate_player_farewell.assert_not_called()
    cog._departure_predictor.mark_farewelled.assert_not_called()
