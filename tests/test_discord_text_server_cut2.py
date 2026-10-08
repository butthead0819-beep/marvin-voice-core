"""單一 mixer 第2刀：:8790 文字 app 搬進 Discord 進程 + car puck 上線自動進語音頻道。

驗：
(1) decide_car_arrive / decide_car_depart 純函式三種組合
(2) pick_rejoin_channel：car_fallback 的優先序
(3) pick_car_channel：channel_id 命中/找不到；guild_id 挑 position 最小；guild 找不到
(4) handle_auto_dismiss：car puck 在線不撤離，其餘照舊 handle_dismiss
(5) auto_rejoin_on_boot(car_join=True, resume_music=False)：無真人頻道時補進車載頻道，
    且不接續 autopilot；resume_music=True + 有真人時照舊接續
(6) car_http_app.build_text_app 的 /audio_stream：SilenceFillQueue 有接上
(7) main_discord._start_discord_text_server：失敗降級 / 沒 _mixer 降級 / 正常接線
(8) Discord 版 _play_open / _stop_playback 透過 start_text_http_server(discord_voice=...)
    接線：present() 不卡住、背景 task 呼叫 auto_rejoin_on_boot；absent() 按人數決定
    撤離與否
(9) Discord 進程車載模式不依賴 MARVIN_CAR_MODE env（傳 discord_voice 即接車載模式）
"""
from __future__ import annotations

import asyncio
import os
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

import car_http_app
import main_discord
from cogs.voice_controller_connection import (
    ConnectionMixin,
    pick_car_channel,
    pick_rejoin_channel,
)
from car_http_app import decide_car_arrive, decide_car_depart
from marvin_voice_core.stream_speaker_output import StreamSpeakerOutput


# ---------- (1) decide_car_arrive / decide_car_depart ----------

def test_decide_car_arrive_not_connected_join_and_open():
    assert decide_car_arrive(connected=False, music_active=False) == "join_and_open"
    assert decide_car_arrive(connected=False, music_active=True) == "join_and_open"


def test_decide_car_arrive_connected_and_playing_skips_open():
    assert decide_car_arrive(connected=True, music_active=True) == "skip_open"


def test_decide_car_arrive_connected_and_idle_opens():
    assert decide_car_arrive(connected=True, music_active=False) == "open"


def test_decide_car_depart_connected_no_humans_dismisses():
    assert decide_car_depart(connected=True, human_count=0) == "dismiss"


def test_decide_car_depart_connected_with_humans_keeps():
    assert decide_car_depart(connected=True, human_count=1) == "keep"


def test_decide_car_depart_not_connected_keeps():
    assert decide_car_depart(connected=False, human_count=0) == "keep"


# ---------- (2) pick_rejoin_channel ----------

def _ch(id_, position, members):
    return SimpleNamespace(id=id_, position=position, members=members, name=f"ch{id_}")


def _human():
    return SimpleNamespace(bot=False)


def _bot_member():
    return SimpleNamespace(bot=True)


def test_pick_rejoin_prefers_human_channel_even_with_car_fallback():
    human_ch = _ch(1, 0, [_human()])
    car_ch = _ch(2, 1, [])
    guild = SimpleNamespace(voice_channels=[human_ch, car_ch])
    assert pick_rejoin_channel([guild], False, car_fallback=car_ch) is human_ch


def test_pick_rejoin_falls_back_to_car_channel_when_no_humans():
    car_ch = _ch(2, 1, [])
    guild = SimpleNamespace(voice_channels=[car_ch])
    assert pick_rejoin_channel([guild], False, car_fallback=car_ch) is car_ch


def test_pick_rejoin_no_humans_no_fallback_returns_none():
    guild = SimpleNamespace(voice_channels=[_ch(1, 0, [])])
    assert pick_rejoin_channel([guild], False, car_fallback=None) is None


def test_pick_rejoin_already_connected_returns_none_even_with_fallback():
    car_ch = _ch(2, 1, [])
    guild = SimpleNamespace(voice_channels=[car_ch])
    assert pick_rejoin_channel([guild], True, car_fallback=car_ch) is None


# ---------- (3) pick_car_channel ----------

def test_pick_car_channel_by_id_hit():
    ch1, ch2 = _ch(1, 0, []), _ch(2, 1, [])
    guild = SimpleNamespace(id=99, voice_channels=[ch1, ch2])
    assert pick_car_channel([guild], guild_id=99, channel_id=2) is ch2


def test_pick_car_channel_by_id_miss_returns_none():
    guild = SimpleNamespace(id=99, voice_channels=[_ch(1, 0, [])])
    assert pick_car_channel([guild], guild_id=99, channel_id=404) is None


def test_pick_car_channel_by_guild_picks_lowest_position():
    ch_hi, ch_lo = _ch(1, 5, []), _ch(2, 0, [])
    guild = SimpleNamespace(id=99, voice_channels=[ch_hi, ch_lo])
    assert pick_car_channel([guild], guild_id=99, channel_id=None) is ch_lo


def test_pick_car_channel_guild_not_found_returns_none():
    guild = SimpleNamespace(id=99, voice_channels=[_ch(1, 0, [])])
    assert pick_car_channel([guild], guild_id=12345, channel_id=None) is None


# ---------- (4) handle_auto_dismiss ----------

@pytest.mark.asyncio
async def test_auto_dismiss_skips_when_car_present():
    fake_self = SimpleNamespace(
        bot=SimpleNamespace(car_presence=SimpleNamespace(is_present=True)),
        handle_dismiss=AsyncMock(),
    )
    await ConnectionMixin.handle_auto_dismiss(fake_self)
    fake_self.handle_dismiss.assert_not_called()


@pytest.mark.asyncio
async def test_auto_dismiss_proceeds_when_car_absent():
    fake_self = SimpleNamespace(
        bot=SimpleNamespace(car_presence=SimpleNamespace(is_present=False)),
        handle_dismiss=AsyncMock(),
    )
    await ConnectionMixin.handle_auto_dismiss(fake_self)
    fake_self.handle_dismiss.assert_awaited_once()


@pytest.mark.asyncio
async def test_auto_dismiss_proceeds_when_no_car_presence_at_all():
    fake_self = SimpleNamespace(
        bot=SimpleNamespace(car_presence=None),
        handle_dismiss=AsyncMock(),
    )
    await ConnectionMixin.handle_auto_dismiss(fake_self)
    fake_self.handle_dismiss.assert_awaited_once()


# ---------- (5) auto_rejoin_on_boot(car_join=True, resume_music=False) ----------

def _make_cog():
    from cogs.voice_controller import VoiceController

    cog = VoiceController.__new__(VoiceController)
    cog.bot = MagicMock()
    cog.bot.voice_clients = []
    cog.bot.guilds = []
    cog.bot.car_presence = None
    cog.active_text_channel = None
    cog.stream_mode = False
    cog.radio_mode = False
    cog.is_playing_audio = False
    cog.connection_time = 0.0
    cog.sink_failure_count = 0
    cog._voice_cooldown_until = 0.0
    cog._auto_rejoin_running = False
    cog._on_key_desync_storm = MagicMock()
    cog.report_sink_error = MagicMock()
    return cog


def _wire_channel_connect(channel):
    new_vc = MagicMock()
    new_vc.is_connected.return_value = True
    new_vc.listen = MagicMock()
    channel.connect = AsyncMock(return_value=new_vc)
    return new_vc


def _fake_engine_module():
    mod = types.ModuleType("discord_voice_engine")
    mod.RealtimeVADSink = MagicMock()
    mod.patch_voice_recv_key_sync = MagicMock()
    return mod


@pytest.mark.asyncio
async def test_auto_rejoin_car_join_connects_car_channel_and_skips_resume():
    cog = _make_cog()
    car_ch = _ch(7, 0, [])  # 無真人
    _wire_channel_connect(car_ch)
    guild = SimpleNamespace(id=42, voice_channels=[car_ch])
    cog.bot.guilds = [guild]
    mc = MagicMock()
    # 讓「接續 autopilot」條件除了 resume_music 以外全部成立，才測得到 resume_music 守門
    mc.stream_mode = False
    mc.radio_mode = False
    mc._autopilot_online_members.return_value = ["狗與露"]
    cog.bot.cogs.get.return_value = mc
    cog.get_online_members = MagicMock(return_value=[])

    with patch.dict(os.environ, {"MARVIN_AUTO_REJOIN": "1", "GUILD_ID": "42",
                                  "MARVIN_CAR_VOICE_CHANNEL_ID": ""}), \
         patch.dict(sys.modules, {"discord_voice_engine": _fake_engine_module()}), \
         patch("asyncio.sleep", new=AsyncMock()):
        await cog.auto_rejoin_on_boot(car_join=True, resume_music=False)

    car_ch.connect.assert_awaited_once()
    mc._ensure_stream_loop.assert_not_called()


@pytest.mark.asyncio
async def test_auto_rejoin_resume_music_true_continues_autopilot_when_humans_present():
    cog = _make_cog()
    human_ch = _ch(8, 0, [_human()])
    _wire_channel_connect(human_ch)
    guild = SimpleNamespace(id=42, voice_channels=[human_ch])
    cog.bot.guilds = [guild]
    mc = MagicMock()
    mc.stream_mode = False
    mc.radio_mode = False
    mc._autopilot_online_members.return_value = ["狗與露"]
    cog.bot.cogs.get.return_value = mc
    cog.get_online_members = MagicMock(return_value=["狗與露"])

    with patch.dict(os.environ, {"MARVIN_AUTO_REJOIN": "1"}), \
         patch.dict(sys.modules, {"discord_voice_engine": _fake_engine_module()}), \
         patch("asyncio.sleep", new=AsyncMock()):
        await cog.auto_rejoin_on_boot(car_join=False, resume_music=True)

    mc._ensure_stream_loop.assert_called_once()


# ---------- (6) main_satellite.build_text_app /audio_stream SilenceFillQueue ----------

@pytest.mark.asyncio
async def test_satellite_audio_stream_fills_silence_when_idle():
    loop = asyncio.get_running_loop()
    out = StreamSpeakerOutput(loop)  # 沒有任何 write()＝idle，沒有真幀
    vc = MagicMock()
    app = car_http_app.build_text_app(vc, token=None, stream_source=out)
    async with TestClient(TestServer(app)) as client:
        async with client.get("/audio_stream") as resp:
            try:
                chunk = await asyncio.wait_for(resp.content.readany(), timeout=0.5)
            finally:
                out.close()
            await asyncio.wait_for(resp.read(), timeout=5)
    assert len(chunk) > 0
    assert chunk[0] == 0xFF and (chunk[1] & 0xE0) == 0xE0


# ---------- (7) main_discord._start_discord_text_server ----------

@pytest.mark.asyncio
async def test_start_discord_text_server_returns_none_without_mixer():
    vc = MagicMock()
    vc._mixer = None
    result = await main_discord._start_discord_text_server(asyncio.get_running_loop(), vc)
    assert result is None


@pytest.mark.asyncio
async def test_start_discord_text_server_degrades_on_exception():
    vc = MagicMock()
    vc._mixer = MagicMock()

    async def _boom(*a, **k):
        raise OSError("port in use")

    with patch("car_http_app.start_text_http_server", _boom):
        result = await main_discord._start_discord_text_server(asyncio.get_running_loop(), vc)
    assert result is None


@pytest.mark.asyncio
async def test_start_discord_text_server_wires_tap_and_discord_voice(monkeypatch):
    vc = MagicMock()
    vc._mixer = MagicMock()
    fake_runner = MagicMock()
    recorded = {}

    async def _fake_start(*args, **kwargs):
        recorded["args"] = args
        recorded["kwargs"] = kwargs
        return fake_runner

    with patch("car_http_app.start_text_http_server", _fake_start):
        result = await main_discord._start_discord_text_server(asyncio.get_running_loop(), vc)

    assert result is fake_runner
    vc._mixer.set_tap.assert_called_once()
    assert recorded["kwargs"].get("discord_voice") is vc


@pytest.mark.asyncio
async def test_start_discord_text_server_car_gain_follows_mixer_volume_target():
    """車機輸出補增益讀 mixer 的音量目標值（不是 ramp 中的 _volume）：0.10→×10、0.5→×2。"""
    vc = MagicMock()
    vc._mixer = MagicMock()
    vc._mixer._volume_target = 0.10
    vc._mixer._volume = 1.0   # ramp 中的值不該被讀到

    async def _fake_start(*args, **kwargs):
        return MagicMock()

    with patch("car_http_app.start_text_http_server", _fake_start):
        await main_discord._start_discord_text_server(asyncio.get_running_loop(), vc)

    stream_out = vc._mixer.set_tap.call_args.args[0]
    assert stream_out._gain_fn() == pytest.approx(10.0)
    vc._mixer._volume_target = 0.5
    assert stream_out._gain_fn() == pytest.approx(2.0)


# ---------- (8) Discord 版 _play_open / _stop_playback 接線 ----------

class _FakeVoiceControllerForCarMode:
    """start_text_http_server(discord_voice=...) 接線測試用的最小假物件：只提供
    Discord 版 _play_open/_stop_playback 會碰到的屬性/方法，不是真 VoiceController。"""

    def __init__(self):
        self.bot = SimpleNamespace(
            voice_clients=[],
            cogs=SimpleNamespace(get=lambda name: None),
            guilds=[],
        )
        self.auto_rejoin_on_boot = AsyncMock()
        self.handle_dismiss = AsyncMock()
        self._online_members: list[str] = []

    def get_online_members(self):
        return self._online_members


@pytest.mark.asyncio
async def test_discord_car_present_does_not_block_and_triggers_auto_rejoin(monkeypatch):
    fake = _FakeVoiceControllerForCarMode()
    # 模擬 connect 卡很久（真實最長 60s）：若 _play_open inline await，present() 會跟著卡住
    _never = asyncio.Event()

    async def _slow_rejoin(**kw):
        await _never.wait()

    fake.auto_rejoin_on_boot = AsyncMock(side_effect=_slow_rejoin)
    monkeypatch.setenv("MARVIN_CAR_MODE", "1")
    monkeypatch.setenv("MARVIN_CLAUDE_STATUS_SCAN", "0")
    monkeypatch.setattr("car_presence_state.save_car_presence_state", lambda **kw: None)

    class _FakeSite:
        def __init__(self, *a, **k):
            pass

        async def start(self):
            pass

    monkeypatch.setattr("aiohttp.web.TCPSite", _FakeSite)

    before = asyncio.all_tasks()
    runner = await car_http_app.start_text_http_server(fake, discord_voice=fake)
    try:
        car_presence = fake.bot.car_presence
        assert car_presence is not None

        import time as _time
        start = _time.monotonic()
        await asyncio.wait_for(car_presence.present("狗與露"), timeout=1.0)  # 連線卡住時 present() 仍須立刻返回
        elapsed = _time.monotonic() - start
        assert elapsed < 0.2, "present() 不該卡在等背景開場流程完成"

        # 讓背景 task 有機會跑
        for _ in range(5):
            await asyncio.sleep(0)

        fake.auto_rejoin_on_boot.assert_awaited_once_with(car_join=True, resume_music=False)
    finally:
        await runner.cleanup()
        for t in asyncio.all_tasks() - before:
            t.cancel()


@pytest.mark.asyncio
async def test_discord_car_absent_dismisses_only_when_no_humans_left(monkeypatch):
    fake = _FakeVoiceControllerForCarMode()
    fake.bot.voice_clients = [MagicMock()]  # 已連線
    monkeypatch.setenv("MARVIN_CAR_MODE", "1")
    monkeypatch.setenv("MARVIN_CLAUDE_STATUS_SCAN", "0")
    monkeypatch.setattr("car_presence_state.save_car_presence_state", lambda **kw: None)

    class _FakeSite:
        def __init__(self, *a, **k):
            pass

        async def start(self):
            pass

    monkeypatch.setattr("aiohttp.web.TCPSite", _FakeSite)

    before = asyncio.all_tasks()
    runner = await car_http_app.start_text_http_server(fake, discord_voice=fake)
    try:
        car_presence = fake.bot.car_presence
        # 先上車（song/pool 皆空，開場靜音；不影響這裡要測的下車邏輯）
        await car_presence.present("狗與露")
        for _ in range(5):
            await asyncio.sleep(0)

        # 頻道還有其他真人 → absent 不該撤離
        fake._online_members = ["其他真人"]
        await car_presence.absent("狗與露")
        fake.handle_dismiss.assert_not_called()

        # 重新上車，這次頻道沒有其他真人 → absent 該撤離
        await car_presence.present("狗與露")
        fake._online_members = []
        await car_presence.absent("狗與露")
        fake.handle_dismiss.assert_awaited_once()
    finally:
        await runner.cleanup()
        for t in asyncio.all_tasks() - before:
            t.cancel()


# ---------- (9) Discord 進程車載模式不依賴 MARVIN_CAR_MODE env ----------

@pytest.mark.asyncio
async def test_discord_process_car_mode_on_even_when_env_blank(monkeypatch):
    """run_bot.py 會把 MARVIN_CAR_MODE 強制清空（防音量污染），但 Discord 進程
    傳了 discord_voice，車載模式仍該接上。"""
    fake = _FakeVoiceControllerForCarMode()
    monkeypatch.setenv("MARVIN_CAR_MODE", "")
    monkeypatch.setenv("MARVIN_CLAUDE_STATUS_SCAN", "0")
    monkeypatch.setattr("car_presence_state.save_car_presence_state", lambda **kw: None)

    class _FakeSite:
        def __init__(self, *a, **k):
            pass

        async def start(self):
            pass

    monkeypatch.setattr("aiohttp.web.TCPSite", _FakeSite)

    before = asyncio.all_tasks()
    runner = await car_http_app.start_text_http_server(fake, discord_voice=fake)
    try:
        car_presence = fake.bot.car_presence
        assert car_presence is not None

        await asyncio.wait_for(car_presence.present("狗與露"), timeout=1.0)
        for _ in range(5):
            await asyncio.sleep(0)

        fake.auto_rejoin_on_boot.assert_awaited_once_with(car_join=True, resume_music=False)
    finally:
        await runner.cleanup()
        for t in asyncio.all_tasks() - before:
            t.cancel()
