"""TDD: _record_speak_outcome_after 判斷勝出後有沒有真的推 TTS 出去。

只量前 15 秒（tts_push_count 有沒有增加），其餘等到 followup_window_s 才寫 log
（had_followup_stt 仍照舊）。tts_push_before=None（量不到 mixer）→ tts_pushed 也是
None，不瞎猜。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cogs.voice_controller_social import ProactiveSocialMixin


def _fake_self(mixer_push_count: int = 0, last_room_stt_time: float = 0.0):
    return SimpleNamespace(
        _mixer=SimpleNamespace(tts_push_count=mixer_push_count),
        _last_room_stt_time=last_room_stt_time,
    )


async def _run(self_obj, **kwargs):
    await ProactiveSocialMixin._record_speak_outcome_after(self_obj, **kwargs)


@pytest.mark.asyncio
async def test_tts_pushed_true_when_count_increases():
    self_obj = _fake_self(mixer_push_count=0)
    sleep_calls = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)
        if len(sleep_calls) == 1:
            self_obj._mixer.tts_push_count += 1  # 第一段 sleep（15s）後才推入

    with patch("cogs.voice_controller_social.asyncio.sleep", new=fake_sleep), \
         patch("cogs.voice_controller_social.append_speak_outcome") as mock_append:
        await _run(
            self_obj, ts=1000.0, trigger="idle_tick", winner="ProactiveTopicAgent",
            confidence=0.6, reason="r", bid_count=1, silence_seconds=300.0,
            present_speakers=("a", "b"), tts_push_before=0, followup_window_s=60.0,
        )

    assert sleep_calls == [15.0, 45.0]
    rec = mock_append.call_args[0][0]
    assert rec.tts_pushed is True


@pytest.mark.asyncio
async def test_tts_pushed_false_when_count_unchanged():
    self_obj = _fake_self(mixer_push_count=3)

    async def fake_sleep(seconds):
        return None

    with patch("cogs.voice_controller_social.asyncio.sleep", new=fake_sleep), \
         patch("cogs.voice_controller_social.append_speak_outcome") as mock_append:
        await _run(
            self_obj, ts=1000.0, trigger="idle_tick", winner="ProactiveTopicAgent",
            confidence=0.6, reason="r", bid_count=1, silence_seconds=300.0,
            present_speakers=("a", "b"), tts_push_before=3, followup_window_s=60.0,
        )

    rec = mock_append.call_args[0][0]
    assert rec.tts_pushed is False


@pytest.mark.asyncio
async def test_tts_pushed_none_when_push_before_unknown():
    self_obj = _fake_self(mixer_push_count=5)

    async def fake_sleep(seconds):
        return None

    with patch("cogs.voice_controller_social.asyncio.sleep", new=fake_sleep), \
         patch("cogs.voice_controller_social.append_speak_outcome") as mock_append:
        await _run(
            self_obj, ts=1000.0, trigger="idle_tick", winner="ProactiveTopicAgent",
            confidence=0.6, reason="r", bid_count=1, silence_seconds=300.0,
            present_speakers=("a", "b"), tts_push_before=None, followup_window_s=60.0,
        )

    rec = mock_append.call_args[0][0]
    assert rec.tts_pushed is None
