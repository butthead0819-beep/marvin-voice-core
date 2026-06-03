"""
CompanionBridge 接線測試 — Phase 3a。

驗證 bridge 在 bot startup 被正確啟動、四個 emitter hook 被正確呼叫、
COMPANION_BRIDGE_ENABLED=false 時優雅跳過、periodic snapshot task 周期觸發。

慣例：使用 MagicMock + AsyncMock；不啟 Discord 連線；bot 由 MagicMock 模擬。
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── 共用 fixture ────────────────────────────────────────────────────────────

@pytest.fixture
def fake_bot():
    """模擬 MarvinBot：帶 router.atmosphere_tracker、music_memory，loop=當前 loop。"""
    bot = MagicMock()
    bot.router = MagicMock()
    bot.router.atmosphere_tracker = MagicMock()
    bot.router.atmosphere_tracker.get_snapshot.return_value = MagicMock(
        dominant_topic="casual",
        topic_confidence=0.8,
        room_mood="ok",
        speaker_states={},
        recent_topics=[],
        ts=0.0,
    )
    bot.router.memory = MagicMock()  # suki_memory
    bot.music_memory = MagicMock()
    bot.loop = asyncio.get_event_loop()
    return bot


# ── Task 1: bridge startup ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bridge_starts_alongside_marmo(monkeypatch, fake_bot):
    """start_companion_bridge() 在 enabled 時呼叫 bridge.start()，並掛到 bot。"""
    monkeypatch.setenv("COMPANION_BRIDGE_ENABLED", "true")
    monkeypatch.delenv("MARMO_TOKEN", raising=False)

    from main_discord import start_companion_bridge

    fake_vc = MagicMock()
    fake_vc.play_tts = AsyncMock()

    with patch("main_discord.CompanionBridge") as MockBridge:
        mock_inst = MagicMock()
        mock_inst.start = AsyncMock()
        mock_inst.emit_atmosphere_snapshot = AsyncMock()
        MockBridge.return_value = mock_inst

        await start_companion_bridge(fake_bot, voice_controller=fake_vc)

        assert MockBridge.called
        mock_inst.start.assert_awaited_once()
        assert getattr(fake_bot, "companion_bridge", None) is mock_inst


@pytest.mark.asyncio
async def test_bridge_disabled_via_env(monkeypatch, fake_bot):
    """COMPANION_BRIDGE_ENABLED=false → 不實例化、不 start、bot 沒 companion_bridge。"""
    monkeypatch.setenv("COMPANION_BRIDGE_ENABLED", "false")

    from main_discord import start_companion_bridge

    with patch("main_discord.CompanionBridge") as MockBridge:
        await start_companion_bridge(fake_bot, voice_controller=MagicMock())
        assert not MockBridge.called
        assert getattr(fake_bot, "companion_bridge", None) is None


# ── Task 2.3: periodic atmosphere emit loop ─────────────────────────────────

@pytest.mark.asyncio
async def test_atmosphere_emit_loop_runs():
    """周期 task：interval=0.05s，跑 0.2s 後至少呼叫一次 emit_atmosphere_snapshot。"""
    from main_discord import _atmosphere_emit_loop

    bridge = MagicMock()
    bridge.emit_atmosphere_snapshot = AsyncMock()

    task = asyncio.create_task(_atmosphere_emit_loop(bridge, interval=0.05))
    await asyncio.sleep(0.2)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert bridge.emit_atmosphere_snapshot.await_count >= 1


# ── Task 2.1: STT hook ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stt_hook_emits_on_transcribe():
    """emit_stt_to_bridge(bot, speaker, text, engine) → bridge.emit_stt_chunk 被呼叫。"""
    from bridge_emitters import emit_stt_to_bridge

    bridge = MagicMock()
    bridge.is_running = True
    bridge.emit_stt_chunk = AsyncMock()

    bot = MagicMock()
    bot.companion_bridge = bridge
    bot.loop = asyncio.get_running_loop()

    emit_stt_to_bridge(bot, "Jack", "hello world", "Swift")

    # emit_stt_to_bridge 排 task，需要 yield 一次讓它跑
    await asyncio.sleep(0.05)
    bridge.emit_stt_chunk.assert_awaited_once_with("Jack", "hello world", "Swift")


@pytest.mark.asyncio
async def test_stt_hook_skips_when_no_bridge():
    """bot.companion_bridge 不存在 → 不爆。"""
    from bridge_emitters import emit_stt_to_bridge

    bot = MagicMock(spec=[])  # 沒有 companion_bridge 屬性
    # 不應 raise
    emit_stt_to_bridge(bot, "Jack", "hi", "Swift")
    await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_stt_hook_skips_when_bridge_not_running():
    """bridge.is_running=False → 不發送。"""
    from bridge_emitters import emit_stt_to_bridge

    bridge = MagicMock()
    bridge.is_running = False
    bridge.emit_stt_chunk = AsyncMock()
    bot = MagicMock()
    bot.companion_bridge = bridge

    emit_stt_to_bridge(bot, "Jack", "hello", "Swift")
    await asyncio.sleep(0.05)
    bridge.emit_stt_chunk.assert_not_awaited()


# ── Task 2.2: TTS hooks ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tts_hook_emits_started_and_done():
    """模擬 play_tts 的 wrapper：呼叫 emit_started → 邏輯 → emit_done。"""
    from bridge_emitters import emit_tts_started_to_bridge, emit_tts_done_to_bridge

    bridge = MagicMock()
    bridge.is_running = True
    bridge.emit_tts_started = AsyncMock()
    bridge.emit_tts_done = AsyncMock()

    bot = MagicMock()
    bot.companion_bridge = bridge

    call_order = []
    bridge.emit_tts_started.side_effect = lambda *a, **kw: call_order.append("started")
    bridge.emit_tts_done.side_effect = lambda *a, **kw: call_order.append("done")

    await emit_tts_started_to_bridge(bot, "嗨", "zh-TW-HsiaoChenNeural", None)
    await emit_tts_done_to_bridge(bot)

    assert call_order == ["started", "done"]
    bridge.emit_tts_started.assert_awaited_once_with("嗨", "zh-TW-HsiaoChenNeural", None)
    bridge.emit_tts_done.assert_awaited_once()


@pytest.mark.asyncio
async def test_tts_hook_emits_done_on_exception():
    """模擬 play_tts 流程中發生例外：emit_done 仍要在 finally 被呼叫。"""
    from bridge_emitters import emit_tts_started_to_bridge, emit_tts_done_to_bridge

    bridge = MagicMock()
    bridge.is_running = True
    bridge.emit_tts_started = AsyncMock()
    bridge.emit_tts_done = AsyncMock()
    bot = MagicMock()
    bot.companion_bridge = bridge

    # 模擬 try/finally：started 後業務拋例外，finally 仍要 emit_done
    try:
        await emit_tts_started_to_bridge(bot, "test", "voice", None)
        raise RuntimeError("playback failure")
    except RuntimeError:
        pass
    finally:
        await emit_tts_done_to_bridge(bot)

    bridge.emit_tts_started.assert_awaited_once()
    bridge.emit_tts_done.assert_awaited_once()


@pytest.mark.asyncio
async def test_tts_hooks_skip_when_no_bridge():
    """bot 沒 companion_bridge → emit helper 不爆、不 raise。"""
    from bridge_emitters import emit_tts_started_to_bridge, emit_tts_done_to_bridge

    bot = MagicMock(spec=[])
    await emit_tts_started_to_bridge(bot, "x", "v", None)
    await emit_tts_done_to_bridge(bot)


# ── Lane E: music emit helpers ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_music_hook_emits_on_play():
    """emit_music_started_to_bridge(bot, song_info, requested_by) → bridge.emit_music_started 被呼叫。"""
    from bridge_emitters import emit_music_started_to_bridge

    bridge = MagicMock()
    bridge.is_running = True
    bridge.emit_music_started = AsyncMock()
    bot = MagicMock()
    bot.companion_bridge = bridge

    song_info = {"title": "X", "style": "lo-fi", "target": "Bob", "source": "library"}
    await emit_music_started_to_bridge(bot, song_info, "Bob")
    bridge.emit_music_started.assert_awaited_once_with(song_info, "Bob")


@pytest.mark.asyncio
async def test_music_hook_emits_on_end():
    """emit_music_ended_to_bridge → bridge.emit_music_ended 被呼叫。"""
    from bridge_emitters import emit_music_ended_to_bridge

    bridge = MagicMock()
    bridge.is_running = True
    bridge.emit_music_ended = AsyncMock()
    bot = MagicMock()
    bot.companion_bridge = bridge

    await emit_music_ended_to_bridge(bot, {"title": "X"}, "natural")
    bridge.emit_music_ended.assert_awaited_once_with({"title": "X"}, "natural")


@pytest.mark.asyncio
async def test_music_hooks_skip_when_no_bridge():
    """bot 無 companion_bridge → emit helper 不爆。"""
    from bridge_emitters import emit_music_started_to_bridge, emit_music_ended_to_bridge

    bot = MagicMock(spec=[])
    await emit_music_started_to_bridge(bot, {"title": "X"}, "Bob")
    await emit_music_ended_to_bridge(bot, {"title": "X"}, "natural")


# ── Lane B2: member presence helpers ────────────────────────────────────────

@pytest.mark.asyncio
async def test_member_joined_hook_emits_to_bridge():
    """emit_member_joined_to_bridge → bridge.emit_member_joined 被呼叫。"""
    from bridge_emitters import emit_member_joined_to_bridge

    bridge = MagicMock()
    bridge.is_running = True
    bridge.emit_member_joined = AsyncMock()
    bot = MagicMock()
    bot.companion_bridge = bridge

    await emit_member_joined_to_bridge(bot, "Jack", {"name": "狗與露"})
    bridge.emit_member_joined.assert_awaited_once_with("Jack", {"name": "狗與露"})


@pytest.mark.asyncio
async def test_member_left_hook_emits_to_bridge():
    """emit_member_left_to_bridge → bridge.emit_member_left 被呼叫。"""
    from bridge_emitters import emit_member_left_to_bridge

    bridge = MagicMock()
    bridge.is_running = True
    bridge.emit_member_left = AsyncMock()
    bot = MagicMock()
    bot.companion_bridge = bridge

    await emit_member_left_to_bridge(bot, "Jack")
    bridge.emit_member_left.assert_awaited_once_with("Jack")


@pytest.mark.asyncio
async def test_member_hooks_skip_when_no_bridge():
    """bot 無 companion_bridge → 不爆。"""
    from bridge_emitters import emit_member_joined_to_bridge, emit_member_left_to_bridge

    bot = MagicMock(spec=[])
    await emit_member_joined_to_bridge(bot, "Jack", {})
    await emit_member_left_to_bridge(bot, "Jack")


@pytest.mark.asyncio
async def test_member_hooks_skip_when_bridge_not_running():
    """bridge.is_running=False → 不發送。"""
    from bridge_emitters import emit_member_joined_to_bridge, emit_member_left_to_bridge

    bridge = MagicMock()
    bridge.is_running = False
    bridge.emit_member_joined = AsyncMock()
    bridge.emit_member_left = AsyncMock()
    bot = MagicMock()
    bot.companion_bridge = bridge

    await emit_member_joined_to_bridge(bot, "Jack", {})
    await emit_member_left_to_bridge(bot, "Jack")
    bridge.emit_member_joined.assert_not_awaited()
    bridge.emit_member_left.assert_not_awaited()


# ── Lane B2: on_voice_state_update wiring ───────────────────────────────────
#
# VoiceController.on_voice_state_update 已存在；測試在該 listener 內，當玩家
# 加入 / 離開 Marvin 所在的語音頻道時，會呼叫 emit_member_joined_to_bridge /
# emit_member_left_to_bridge。

@pytest.mark.asyncio
async def test_voice_state_join_emits_member_joined(monkeypatch):
    """玩家加入 Marvin 所在頻道 → emit_member_joined_to_bridge 被呼叫。"""
    # Hook：在 VoiceController.on_voice_state_update 觸發 join 時，main_discord
    # 的 emit_member_joined_to_bridge 應該被叫到（攜帶 speaker = display_name）。
    called = []

    async def fake_emit_joined(bot, speaker, extras):
        called.append(("joined", speaker, extras))

    async def fake_emit_left(bot, speaker):
        called.append(("left", speaker))

    import main_discord
    import bridge_emitters as bridge_emitters_mod
    monkeypatch.setattr(bridge_emitters_mod, "emit_member_joined_to_bridge", fake_emit_joined)
    monkeypatch.setattr(bridge_emitters_mod, "emit_member_left_to_bridge", fake_emit_left)

    # 用 cog 的 listener function（裸 coroutine 形式）模擬呼叫
    from cogs.voice_controller import VoiceController

    # 建構最小 cog stub
    cog = MagicMock(spec=VoiceController)
    cog.bot = MagicMock()
    cog.bot.user = MagicMock()
    cog.bot.user.id = 99999
    cog.consent = MagicMock()
    cog.consent.has_seen_notice.return_value = True
    cog.active_text_channel = None
    cog.greeting_cooldown = {}
    cog.stream_mode = False  # greeting 路徑讀 self.stream_mode（spec mock 不含 instance 屬性）
    cog.stt_logger = MagicMock()
    cog.recent_verbal_farewells = {}
    cog.departure_stats = MagicMock()
    cog.departure_stats.record_departure = AsyncMock()
    cog.bot.router = MagicMock()
    cog.bot.router.generate_player_greeting = AsyncMock(return_value="hi")
    cog.bot.router.generate_player_farewell = AsyncMock(return_value="bye")
    cog.play_tts = AsyncMock()
    cog._send_mood_sticker = AsyncMock()
    cog.handle_dismiss = AsyncMock()

    # 模擬 Marvin 在 channel A
    channel_a = MagicMock()
    channel_a.members = [MagicMock(bot=False)]
    voice_client = MagicMock()
    voice_client.channel = channel_a
    cog.bot.voice_clients = [voice_client]

    member = MagicMock()
    member.id = 12345
    member.display_name = "Jack"
    member.guild = MagicMock()

    before = MagicMock()
    before.channel = None
    after = MagicMock()
    after.channel = channel_a

    # discord.utils.get patching
    import discord
    monkeypatch.setattr(discord.utils, "get", lambda iterable, **kw: voice_client)

    # 直接呼叫 listener 方法（unbound）
    # listener decorator 包了 coroutine；直接拿 function 物件呼叫
    listener_fn = VoiceController.on_voice_state_update
    await listener_fn(cog, member, before, after)

    # 期望：emit_member_joined_to_bridge 被叫到一次，speaker="Jack"
    joined = [c for c in called if c[0] == "joined"]
    assert len(joined) == 1
    assert joined[0][1] == "Jack"


@pytest.mark.asyncio
async def test_voice_state_leave_emits_member_left(monkeypatch):
    """玩家離開 Marvin 所在頻道 → emit_member_left_to_bridge 被呼叫。"""
    called = []

    async def fake_emit_joined(bot, speaker, extras):
        called.append(("joined", speaker))

    async def fake_emit_left(bot, speaker):
        called.append(("left", speaker))

    import main_discord
    import bridge_emitters as bridge_emitters_mod
    monkeypatch.setattr(bridge_emitters_mod, "emit_member_joined_to_bridge", fake_emit_joined)
    monkeypatch.setattr(bridge_emitters_mod, "emit_member_left_to_bridge", fake_emit_left)

    from cogs.voice_controller import VoiceController

    cog = MagicMock(spec=VoiceController)
    cog.bot = MagicMock()
    cog.bot.user = MagicMock()
    cog.bot.user.id = 99999
    cog.consent = MagicMock()
    cog.consent.has_seen_notice.return_value = True
    cog.active_text_channel = None
    cog.greeting_cooldown = {}
    cog.stream_mode = False  # greeting 路徑讀 self.stream_mode（spec mock 不含 instance 屬性）
    cog.stt_logger = MagicMock()
    cog.recent_verbal_farewells = {}
    cog.departure_stats = MagicMock()
    cog.departure_stats.record_departure = AsyncMock()
    cog.bot.router = MagicMock()
    cog.bot.router.generate_player_greeting = AsyncMock(return_value="hi")
    cog.bot.router.generate_player_farewell = AsyncMock(return_value="bye")
    cog.play_tts = AsyncMock()
    cog._send_mood_sticker = AsyncMock()
    cog.handle_dismiss = AsyncMock()

    channel_a = MagicMock()
    # 還剩一個人類在房間，不會觸發 auto dismiss
    other = MagicMock(bot=False)
    channel_a.members = [other]
    voice_client = MagicMock()
    voice_client.channel = channel_a
    cog.bot.voice_clients = [voice_client]

    member = MagicMock()
    member.id = 12345
    member.display_name = "Jack"
    member.guild = MagicMock()

    before = MagicMock()
    before.channel = channel_a
    after = MagicMock()
    after.channel = None

    import discord
    monkeypatch.setattr(discord.utils, "get", lambda iterable, **kw: voice_client)

    # listener decorator 包了 coroutine；直接拿 function 物件呼叫
    listener_fn = VoiceController.on_voice_state_update
    await listener_fn(cog, member, before, after)

    left = [c for c in called if c[0] == "left"]
    assert len(left) == 1
    assert left[0][1] == "Jack"
