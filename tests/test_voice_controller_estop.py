"""TDD：喊「馬文閉嘴」緊急停止接線（cogs/voice_controller_estop.py）。

喊停語要穿透所有喚醒防護（Echo Guard／Response Lock／對談窗…），所以攔截點必須在
handle_stt_result 最前面、consent 之後就處理，靠 _estop_intercept 這個同步入口。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from cogs.voice_controller_estop import EmergencyStopMixin
from emergency_stop import EmergencyStop


def _make_fake(tmp_path):
    class _Fake(EmergencyStopMixin):
        pass

    fake = _Fake()
    fake._estop = EmergencyStop(str(tmp_path / "e.json"))
    fake._mixer = MagicMock()
    music_cog = MagicMock()
    music_cog.stop_stream = AsyncMock()
    music_cog.stop_radio = AsyncMock()
    fake.bot = MagicMock()
    fake.bot.cogs.get.return_value = music_cog
    fake.active_text_channel = MagicMock()
    fake.active_text_channel.send = AsyncMock()
    return fake, music_cog


@pytest.mark.asyncio
async def test_estop_phrase_intercepted_mutes_and_stops_music(tmp_path):
    fake, music_cog = _make_fake(tmp_path)
    assert fake._estop_intercept("A", "馬文閉嘴") is True
    fake._mixer.set_estop.assert_called_once_with(True)
    assert fake._estop_active() is True
    await asyncio.sleep(0)
    music_cog.stop_stream.assert_awaited_once()
    music_cog.stop_radio.assert_awaited_once()
    fake.active_text_channel.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_estop_phrase_again_while_already_active_is_noop(tmp_path):
    fake, music_cog = _make_fake(tmp_path)
    fake._estop_intercept("A", "馬文閉嘴")
    await asyncio.sleep(0)
    fake._mixer.set_estop.reset_mock()
    music_cog.stop_stream.reset_mock()
    fake.active_text_channel.send.reset_mock()

    assert fake._estop_intercept("B", "馬文安靜") is True
    await asyncio.sleep(0)
    fake._mixer.set_estop.assert_not_called()
    music_cog.stop_stream.assert_not_called()
    fake.active_text_channel.send.assert_not_called()


@pytest.mark.asyncio
async def test_non_estop_phrase_returns_false_and_no_side_effects(tmp_path):
    fake, _music_cog = _make_fake(tmp_path)
    assert fake._estop_intercept("A", "馬文下一首") is False
    fake._mixer.set_estop.assert_not_called()

    fake._estop_state().engage("A", 1.0)
    fake._mixer.reset_mock()
    assert fake._estop_intercept("A", "馬文下一首") is False
    fake._mixer.set_estop.assert_not_called()


@pytest.mark.asyncio
async def test_estop_command_release_while_active(tmp_path):
    fake, _music_cog = _make_fake(tmp_path)
    fake._estop_state().engage("A", 1.0)
    fake._mixer.reset_mock()

    reply = AsyncMock()
    action = MagicMock()
    action.value = "release"
    await fake._estop_command("me", "release", reply)

    fake._mixer.set_estop.assert_called_once_with(False)
    assert fake._estop_active() is False
    reply.assert_awaited_once()


@pytest.mark.asyncio
async def test_estop_command_release_when_not_active_is_ephemeral(tmp_path):
    fake, _music_cog = _make_fake(tmp_path)
    reply = AsyncMock()
    await fake._estop_command("me", "release", reply)
    fake._mixer.set_estop.assert_not_called()
    reply.assert_awaited_once_with("目前沒有在緊急停止。", ephemeral=True)


@pytest.mark.asyncio
async def test_estop_command_stop_engages_and_stops_music(tmp_path):
    fake, music_cog = _make_fake(tmp_path)
    reply = AsyncMock()
    await fake._estop_command("me", "stop", reply)
    fake._mixer.set_estop.assert_called_once_with(True)
    music_cog.stop_stream.assert_awaited_once()
    music_cog.stop_radio.assert_awaited_once()

    reply2 = AsyncMock()
    await fake._estop_command("me", "stop", reply2)
    reply2.assert_awaited_once_with("已經在緊急停止中。", ephemeral=True)


def test_estop_state_persists_across_instances(tmp_path):
    class _Fake(EmergencyStopMixin):
        pass

    path = str(tmp_path / "e.json")
    f1 = _Fake()
    f1._estop = EmergencyStop(path)
    f1._estop_state().engage("A", 1.0)

    f2 = _Fake()
    f2._estop = EmergencyStop(path)
    assert f2._estop_active() is True


@pytest.mark.asyncio
async def test_estop_intercept_does_not_raise_when_mixer_none(tmp_path):
    fake, _music_cog = _make_fake(tmp_path)
    fake._mixer = None
    assert fake._estop_intercept("A", "馬文閉嘴") is True
    await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("intercepted", [True, False])
async def test_handle_stt_result_intercepts_before_anything_else(intercepted):
    """接線：喊停語在 consent 之後、其他任何處理（含 mixer duck、喚醒防護）之前就吞掉。"""
    from cogs.voice_controller import VoiceController
    fake = MagicMock()
    fake.consent.is_consented.return_value = True
    fake._estop_intercept.return_value = intercepted
    fake._memory_check_intercept.return_value = False  # 同一個 if 的記憶確認攔截，與本測試無關
    try:
        await VoiceController.handle_stt_result(fake, "A", "馬文閉嘴", 0.0, b"")
    except Exception:
        pass   # 沒攔截時會往下跑進 MagicMock 撐不住的地方，這裡只看有沒有走到 duck
    fake._estop_intercept.assert_called_once_with("A", "馬文閉嘴")
    assert fake._mixer.note_player_speech.called is (not intercepted)


@pytest.mark.parametrize("active", [True, False])
def test_initial_mixer_applies_persisted_estop(active):
    """重啟後 mixer 一建好就套用存檔的緊急停止狀態。"""
    from cogs.voice_controller_playback import PlaybackMixin
    fake = MagicMock()
    fake._estop_active.return_value = active
    fake._stream_volume_local = 0.5
    bot = MagicMock()
    bot.cogs.get.return_value = None
    mixer = PlaybackMixin._make_initial_mixer(fake, bot)
    assert mixer._estop is active
