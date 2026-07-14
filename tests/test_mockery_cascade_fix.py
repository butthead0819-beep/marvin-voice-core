"""TDD：嘲諷觸發後必須立刻 reset last_marvin_speech_time，避免 cascade。

2026-05-20 prod 觀察（15:42–15:50）：嘲諷連發 10 次，silence_duration 從 137s
單調成長到 612s。Root cause：stream_mode + silent_during_stream=True → play_tts
line 4658 直接 return，line 4870 的 self.last_marvin_speech_time = time.time()
沒執行 → 下次 silence check 仍超 threshold → 嘲諷再觸發 → 連發。

修法：嘲諷邏輯（_check_silent_mockery 或對等位置）觸發時，立刻設
self.last_marvin_speech_time = time.time()，與 TTS 是否真播解耦。
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest


def _make_cog():
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.tts_engine = MagicMock()
    bot.router = MagicMock()
    bot.engine = MagicMock()
    bot.engine.conv_buffer = MagicMock()
    bot.engine.conv_buffer.get_conversation_temperature = MagicMock(return_value=0.5)
    bot.engine.conv_buffer.get_history = MagicMock(return_value=[])
    bot.engine.get_active_sink = MagicMock(return_value=None)
    bot.engine.post_summon_callback = None
    bot.loop = MagicMock()
    bot.loop.create_task = MagicMock()

    with patch("cogs.voice_controller.DepartureStats", MagicMock), \
         patch("cogs.voice_controller.ConsentManager", MagicMock):
        from cogs.voice_controller import VoiceController
        cog = VoiceController(bot)
    cog.stt_logger = MagicMock()
    return cog


# ── 1. 嘲諷觸發後 last_marvin_speech_time 必須 reset ────────────────────────

def test_mockery_resets_last_marvin_speech_time():
    """觸發嘲諷後，下次 silence_duration 應該歸零（從 reset 點重新計算）。"""
    cog = _make_cog()
    cog.stream_mode = False  # 非 stream mode 也要 reset（rule 普適）

    # 模擬「Marvin 上次說話在 200 秒前」
    now = time.time()
    cog.last_marvin_speech_time = now - 200.0

    # 直接呼 _trigger_silent_mockery（或既有方法）
    cog._trigger_silent_mockery("狗與露", silence_duration=200.0)

    # 嘲諷觸發後 last_marvin_speech_time 應該 ≥ now（已被 reset 到當下）
    assert cog.last_marvin_speech_time >= now - 0.5, \
        f"嘲諷後 last_marvin_speech_time 必須 reset 到當下，實際: {cog.last_marvin_speech_time} vs now={now}"


def test_mockery_in_stream_mode_still_resets_timer():
    """stream_mode + silent_during_stream → play_tts 被 skip，但 reset 仍要發生。
    這正是 2026-05-20 真實 prod cascade 的觸發場景。
    """
    cog = _make_cog()
    cog.stream_mode = True  # 模擬正在串流播放
    cog.active_text_channel = MagicMock()

    now = time.time()
    cog.last_marvin_speech_time = now - 300.0

    cog._trigger_silent_mockery("狗與露", silence_duration=300.0)

    assert cog.last_marvin_speech_time >= now - 0.5, \
        "stream_mode 嘲諷也必須 reset timer，否則 cascade"


# ── 1b. satellite/local 模式關閉嘲諷 ──────────────────────────────────────

def test_mockery_suppressed_in_local_satellite_mode():
    """satellite/local（device）模式使用者不主動講話 → 「等你回話」嘲諷不該觸發。

    2026-07-14：satellite 本來就是 PTT/喚醒驅動，使用者不會主動開口，「反應太慢」
    的延遲嘲諷在這語境下純屬騷擾。_local_mode（satellite + 本機）一律不嘲諷。
    """
    cog = _make_cog()
    cog.stream_mode = False
    cog._local_mode = True  # satellite / 本機 device 模式

    now = time.time()
    cog.last_marvin_speech_time = now - 300.0

    cog._trigger_silent_mockery("狗與露", silence_duration=300.0)

    # 未觸發：不打 log、不排 TTS、也不加入 pending_mock_users
    cog.stt_logger.info.assert_not_called()
    cog.bot.loop.create_task.assert_not_called()
    assert "狗與露" not in cog.pending_mock_users


# ── 2. Cooldown 防 cascade（驗證雙重保護）─────────────────────────────────

def test_mockery_per_speaker_cooldown_45s():
    """同 speaker 45s 內第二次不該再嘲諷（既有保護，不該 regress）。"""
    cog = _make_cog()
    cog.stream_mode = False

    # 第一次觸發
    cog._trigger_silent_mockery("狗與露", silence_duration=200.0)
    first_log_count = cog.stt_logger.info.call_count

    # 第二次馬上觸發（同個 speaker）
    cog._trigger_silent_mockery("狗與露", silence_duration=200.0)
    second_log_count = cog.stt_logger.info.call_count

    assert second_log_count == first_log_count, \
        f"45s cooldown 內第二次該被擋，但 stt_logger 又被叫一次（{first_log_count} → {second_log_count}）"
