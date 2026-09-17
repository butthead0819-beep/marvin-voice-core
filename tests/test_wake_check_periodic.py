"""🔁 [WakeCheck] 句首三連拍之後的週期補拍 —— 抓「講話途中才插入的指令」。

2026-09-17 18:24 事故：使用者連續發言到第 ~11 秒才說「馬文，下一首」，但
_WAKE_CHECK_TIMES 只在句首做三次快照 (0.6/1.2/1.8s)，之後整段語音不再檢查喚醒詞，
途中插入的指令只能等 VAD 切斷才被處理——那次剛好撞上 12s 硬切被劈成兩段，
兩段都不含喚醒詞，指令整個掉了。

測的是 discord_voice_engine.wake_check_due_at()（sink 熱路徑呼叫的同一個純函式），
不是在測試檔裡複製一份公式再驗算自己。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from discord_voice_engine import (
    _WAKE_CHECK_INTERVAL,
    _WAKE_CHECK_MAX_COUNT,
    _WAKE_CHECK_TIMES,
    wake_check_due_at,
)


# ── 時間軸 ────────────────────────────────────────────────────────────────────

def test_first_three_checks_unchanged():
    """回歸：句首三連拍時間點完全不變（0.6 / 1.2 / 1.8s）。"""
    assert wake_check_due_at(0) == pytest.approx(0.6)
    assert wake_check_due_at(1) == pytest.approx(1.2)
    assert wake_check_due_at(2) == pytest.approx(1.8)


def test_periodic_checks_after_first_three():
    """三連拍之後每 3 秒補一拍：4.8 / 7.8 / 10.8s。"""
    assert wake_check_due_at(3) == pytest.approx(4.8)
    assert wake_check_due_at(4) == pytest.approx(7.8)
    assert wake_check_due_at(5) == pytest.approx(10.8)


def test_due_times_strictly_increasing_and_spaced_by_interval():
    """時間軸單調遞增，且三連拍之後的間隔恰好是 _WAKE_CHECK_INTERVAL。"""
    dues = [wake_check_due_at(i) for i in range(_WAKE_CHECK_MAX_COUNT)]
    assert dues == sorted(dues)
    assert len(set(dues)) == len(dues)
    for i in range(len(_WAKE_CHECK_TIMES), _WAKE_CHECK_MAX_COUNT):
        assert dues[i] - dues[i - 1] == pytest.approx(_WAKE_CHECK_INTERVAL)


def test_incident_11s_speech_is_covered():
    """事故場景：講到第 11 秒才插指令，第 5 拍（10.8s）涵蓋得到。"""
    assert wake_check_due_at(5) <= 11.0
    assert 5 < _WAKE_CHECK_MAX_COUNT


def test_max_count_bounds_the_timeline():
    """次數上限存在且涵蓋到 12s 硬切之後（配合 B3 的 +2s 寬限窗）。"""
    assert _WAKE_CHECK_MAX_COUNT == 8
    assert wake_check_due_at(_WAKE_CHECK_MAX_COUNT - 1) >= 14.0


# ── sink 觸發接線 ─────────────────────────────────────────────────────────────

def _bare_sink(counts=None):
    """繞過 __init__（要 opus/DAVE），只裝 wake check 需要的狀態。"""
    from discord_voice_engine import RealtimeVADSink

    sink = RealtimeVADSink.__new__(RealtimeVADSink)
    sink.user_wake_check_count = dict(counts or {})
    return sink


def test_sink_fires_periodic_check_after_three_taps():
    """接線：三連拍用完後，sink 在 4.8s 仍會補拍（退回舊條件這條會紅）。"""
    sink = _bare_sink({7: 3})
    assert sink._should_wake_check(7, 4.8) is True
    assert sink._should_wake_check(7, 4.7) is False


def test_sink_fires_at_incident_timestamp():
    """事故場景接線：已拍 5 次、講到第 11 秒 → 仍要補拍。"""
    sink = _bare_sink({7: 5})
    assert sink._should_wake_check(7, 11.0) is True


def test_sink_respects_first_three_taps():
    """回歸：句首三連拍的觸發時機不變。"""
    sink = _bare_sink({7: 0})
    assert sink._should_wake_check(7, 0.59) is False
    assert sink._should_wake_check(7, 0.6) is True


def test_sink_stops_at_max_count():
    """次數上限：達上限後不論講多久都不再補拍。"""
    sink = _bare_sink({7: _WAKE_CHECK_MAX_COUNT})
    assert sink._should_wake_check(7, 999.0) is False


# ── 命中後停拍（去重）─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_wake_hit_stops_further_snapshots_this_utterance():
    """Track A 在快照命中後，該 user 的快照次數被推到上限，停止後續補拍。

    不擋的話，buffer 要等 VAD 切斷才清空，同一個「馬文」會在 4.8/7.8/10.8s
    被反覆抓到、反覆觸發喚醒。
    """
    from discord_voice_engine import DiscordVoiceEngine

    bot = MagicMock()
    bot.guilds = []
    with patch("discord_voice_engine.faster_whisper", None, create=True):
        engine = DiscordVoiceEngine(bot)

    sink = MagicMock()
    sink.user_wake_check_count = {42: 3}
    engine.get_active_sink = MagicMock(return_value=sink)
    engine.stt_callback = AsyncMock()
    engine._run_swift_stt = AsyncMock(return_value=("馬文下一首", {}))

    await engine._process_stt_hybrid(
        speaker_name="測試者",
        wav_path="/tmp/does_not_exist.wav",
        wav_bytes=b"",
        timestamp=0.0,
        is_wake_check=True,
        user_id=42,
    )

    engine.stt_callback.assert_awaited_once()
    assert engine.stt_callback.await_args.kwargs["track"] == "A"
    assert sink.user_wake_check_count[42] == _WAKE_CHECK_MAX_COUNT


@pytest.mark.asyncio
async def test_wake_miss_does_not_stop_snapshots():
    """快照沒命中喚醒詞 → 不動快照次數，後續補拍照常進行。"""
    from discord_voice_engine import DiscordVoiceEngine

    bot = MagicMock()
    bot.guilds = []
    with patch("discord_voice_engine.faster_whisper", None, create=True):
        engine = DiscordVoiceEngine(bot)

    sink = MagicMock()
    sink.user_wake_check_count = {42: 3}
    engine.get_active_sink = MagicMock(return_value=sink)
    engine.stt_callback = AsyncMock()
    engine._run_swift_stt = AsyncMock(return_value=("今天天氣真好", {}))

    await engine._process_stt_hybrid(
        speaker_name="測試者",
        wav_path="/tmp/does_not_exist.wav",
        wav_bytes=b"",
        timestamp=0.0,
        is_wake_check=True,
        user_id=42,
    )

    assert sink.user_wake_check_count[42] == 3


@pytest.mark.asyncio
async def test_wake_hit_survives_missing_sink():
    """拿不到 sink 時不能讓喚醒流程掛掉（優雅降級）。"""
    from discord_voice_engine import DiscordVoiceEngine

    bot = MagicMock()
    bot.guilds = []
    with patch("discord_voice_engine.faster_whisper", None, create=True):
        engine = DiscordVoiceEngine(bot)

    engine.get_active_sink = MagicMock(side_effect=RuntimeError("no sink"))
    engine.stt_callback = AsyncMock()
    engine._run_swift_stt = AsyncMock(return_value=("馬文下一首", {}))

    await engine._process_stt_hybrid(
        speaker_name="測試者",
        wav_path="/tmp/does_not_exist.wav",
        wav_bytes=b"",
        timestamp=0.0,
        is_wake_check=True,
        user_id=42,
    )

    engine.stt_callback.assert_awaited_once()
