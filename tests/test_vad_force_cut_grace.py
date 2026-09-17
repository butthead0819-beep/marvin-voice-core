"""⏳ [B3 Force-Cut Grace] 12s 硬切改「智慧切點」——寬限窗測試。

2026-09-17 18:24 事故：「馬文，下一首」剛好撞上 12s 強制切斷，「馬文」被切進上一段，
下一段只剩「下一首」，兩段都不含喚醒詞，指令整段掉了。這裡驗證：12s 到點時不立即
硬切，改開 2s 寬限窗把靜音門檻收緊到 0.5s，讓既有的靜音切斷路徑在自然停頓處切；
窗內沒等到停頓就照舊硬切（純 fallback，不可比改動前更差）。

只測 DiscordVoiceEngine._vad_watchdog 的情境 A/B（sink 的 event-driven 靜音偵測是
同一組門檻邏輯的鏡射，改點見 discord_voice_engine.py 的 RealtimeVADSink.write()）。
"""
import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from discord_voice_engine import DiscordVoiceEngine, FORCE_CUT_GRACE_SILENCE, FORCE_CUT_GRACE_WINDOW


def make_engine():
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.get_cog.return_value = None  # 跳過社交補位段落，本測試不關心
    bot.tts_engine = MagicMock()
    with patch("discord_voice_engine.faster_whisper", None, create=True):
        engine = DiscordVoiceEngine(bot)
    engine.process_audio_slice = AsyncMock()
    return engine


def make_sink(user_id, buffer_len=192000, first_audio=0.0, last_spoken=0.0):
    sink = SimpleNamespace()
    sink.user_buffers = {user_id: bytearray(buffer_len)}
    sink.user_last_spoken_time = {user_id: last_spoken}
    sink.user_first_audio_time = {user_id: first_audio}
    sink.user_force_cut_grace = {}
    sink.user_utt_max_gap = {}
    sink.user_wake_check_count = {}
    sink.wake_stream = None
    sink.last_audio_packet_time = 0.0
    sink._stream_release = MagicMock()
    return sink


async def run_watchdog_once(engine, sink, now):
    """跑 _vad_watchdog 迴圈主體恰好一次（第一次 asyncio.sleep 後立刻關閉迴圈旗標）。"""
    engine.is_listening = True
    engine.get_active_sink = MagicMock(return_value=sink)

    async def fake_sleep(_delay):
        engine.is_listening = False

    with patch("discord_voice_engine.asyncio.sleep", fake_sleep), \
         patch("discord_voice_engine.time.time", return_value=now):
        await engine._vad_watchdog()


@pytest.mark.asyncio
async def test_12s_limit_hit_still_talking_opens_grace_no_cut():
    """1. 12s 到點且話還沒停 → 本輪不切，寬限窗被設定為 now + 2.0。"""
    engine = make_engine()
    now = 100000.0
    user_id = 1
    # first_audio 13s 前 → 超過 12s 上限；last_spoken 剛剛（沒有靜默）→ 情境 A 不切
    sink = make_sink(user_id, first_audio=now - 13.0, last_spoken=now - 0.1)

    await run_watchdog_once(engine, sink, now)

    assert len(sink.user_buffers[user_id]) == 192000  # 本輪沒被切斷
    engine.process_audio_slice.assert_not_called()
    assert sink.user_force_cut_grace.get(user_id) == pytest.approx(now + FORCE_CUT_GRACE_WINDOW)


@pytest.mark.asyncio
async def test_natural_pause_inside_grace_cuts_and_clears_grace():
    """2. 寬限窗內出現 >=0.5s 靜音 → 靜音路徑切了（自然切點），且寬限窗被清掉。"""
    engine = make_engine()
    now = 100001.0
    user_id = 2
    sink = make_sink(user_id, first_audio=now - 13.0, last_spoken=now - 0.6)
    sink.user_force_cut_grace[user_id] = now + 1.0  # 寬限窗還沒過期

    await run_watchdog_once(engine, sink, now)

    assert len(sink.user_buffers[user_id]) == 0  # 被情境 A 切斷
    engine.process_audio_slice.assert_called_once()
    assert user_id not in sink.user_force_cut_grace


@pytest.mark.asyncio
async def test_short_pause_inside_grace_does_not_cut():
    """3. 寬限窗內靜音只有 0.3s（< 0.5s）→ 不切，寬限窗仍在。"""
    engine = make_engine()
    now = 100002.0
    user_id = 3
    sink = make_sink(user_id, first_audio=now - 13.0, last_spoken=now - 0.3)
    sink.user_force_cut_grace[user_id] = now + 1.0

    await run_watchdog_once(engine, sink, now)

    assert len(sink.user_buffers[user_id]) == 192000  # 沒被切斷
    engine.process_audio_slice.assert_not_called()
    assert user_id in sink.user_force_cut_grace  # 寬限窗仍保留


@pytest.mark.asyncio
async def test_grace_expired_hard_cuts_like_before():
    """4. 寬限窗過期（now >= deadline）仍沒停 → 硬切，行為與改動前完全一致。"""
    engine = make_engine()
    now = 100003.0
    user_id = 4
    sink = make_sink(user_id, first_audio=now - 13.0, last_spoken=now - 0.1)
    sink.user_force_cut_grace[user_id] = now - 0.01  # 已過期

    await run_watchdog_once(engine, sink, now)

    assert len(sink.user_buffers[user_id]) == 0  # 硬切
    engine.process_audio_slice.assert_called_once()
    args = engine.process_audio_slice.call_args.args
    assert args[0] == user_id
    assert args[2] == now - 13.0  # timestamp 沿用 first_audio，與改動前一致
    assert user_id not in sink.user_force_cut_grace
    assert sink.user_first_audio_time[user_id] == now  # 讓使用者可以繼續說下去


@pytest.mark.asyncio
async def test_regression_under_12s_untouched():
    """5. 回歸：12s 沒到點時，情境 B 完全不觸發，不開寬限窗。"""
    engine = make_engine()
    now = 100004.0
    user_id = 5
    sink = make_sink(user_id, first_audio=now - 5.0, last_spoken=now - 0.1)

    await run_watchdog_once(engine, sink, now)

    assert len(sink.user_buffers[user_id]) == 192000
    engine.process_audio_slice.assert_not_called()
    assert user_id not in sink.user_force_cut_grace


@pytest.mark.asyncio
async def test_grace_only_tightens_never_loosens():
    """6. 寬限窗只收緊不放寬：常態門檻 0.3s（比 0.5s 緊）時，套用 min() 後仍是 0.3s。"""
    engine = make_engine()
    now = 100005.0
    user_id = 6
    engine.conv_buffer.get_conversation_temperature = MagicMock(return_value=0.3)
    # 靜音只有 0.4s：若門檻被錯誤放寬到 0.5s 會被判定「還沒到門檻」而不切；
    # 若正確維持 min(0.3, 0.5)=0.3，0.4s > 0.3s 應該要切。
    sink = make_sink(user_id, first_audio=now - 13.0, last_spoken=now - 0.4)
    sink.user_force_cut_grace[user_id] = now + 1.0

    await run_watchdog_once(engine, sink, now)

    assert len(sink.user_buffers[user_id]) == 0
    engine.process_audio_slice.assert_called_once()
