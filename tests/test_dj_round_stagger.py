"""TDD: DJ intro TTS 生成依 round 位置錯開，避免 rate limit burst

行為規格：
- _round_position=0（round 首曲）→ 不等待，立即生成
- _round_position=1 → asyncio.sleep(3.0) 再生成
- _round_position=2 → asyncio.sleep(6.0) 再生成
- 非 Marvin 推薦（user 點的）→ 不錯開（原有行為不變）
"""
from __future__ import annotations
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call


def _make_cog():
    from cogs.voice_controller import VoiceController
    cog = VoiceController.__new__(VoiceController)

    bot = MagicMock()
    bot.music_memory = None
    bot.tts_engine = MagicMock()
    bot.tts_engine.get_estimated_duration = MagicMock(return_value=3.0)

    async def _fake_generate(t):
        return None
    bot.tts_engine.generate_audio = _fake_generate
    cog.bot = bot

    cog._parse_song_title_artist = MagicMock(return_value=("天天", "陶喆"))
    return cog


def _marvin_info(position: int, spotlight: str = "Alice") -> dict:
    return {
        'title': '天天',
        'requested_by': f'Marvin推薦（為{spotlight}）',
        '_spotlight': spotlight,
        '_lane': 'spotlight',
        '_round_first': (position == 0),
        '_round_position': position,
        'url': f'http://fake/{position}',
    }


@pytest.mark.asyncio
async def test_round_first_no_stagger():
    """_round_position=0 → asyncio.sleep 不應以 3+ 秒被呼叫。"""
    cog = _make_cog()
    slept = []

    async def _fake_sleep(s):
        slept.append(s)

    with patch("tts_length_policy.truncate_for_tts", return_value=("ok", False)), \
         patch("asyncio.sleep", side_effect=_fake_sleep):
        result = await cog._fetch_dj_interjection_raw(_marvin_info(0))

    assert result is not None
    long_sleeps = [s for s in slept if s >= 3.0]
    assert not long_sleeps, f"round_first 不應有長等待，得到: {long_sleeps}"


@pytest.mark.asyncio
async def test_round_second_sleeps_3s():
    """_round_position=1 → asyncio.sleep(3.0) 被呼叫。"""
    cog = _make_cog()
    slept = []

    async def _fake_sleep(s):
        slept.append(s)

    with patch("tts_length_policy.truncate_for_tts", return_value=("ok", False)), \
         patch("asyncio.sleep", side_effect=_fake_sleep):
        result = await cog._fetch_dj_interjection_raw(_marvin_info(1))

    assert result is not None
    assert 3.0 in slept, f"position=1 應 sleep(3.0)，實際 slept={slept}"


@pytest.mark.asyncio
async def test_round_third_sleeps_6s():
    """_round_position=2 → asyncio.sleep(6.0) 被呼叫。"""
    cog = _make_cog()
    slept = []

    async def _fake_sleep(s):
        slept.append(s)

    with patch("tts_length_policy.truncate_for_tts", return_value=("ok", False)), \
         patch("asyncio.sleep", side_effect=_fake_sleep):
        result = await cog._fetch_dj_interjection_raw(_marvin_info(2))

    assert result is not None
    assert 6.0 in slept, f"position=2 應 sleep(6.0)，實際 slept={slept}"


@pytest.mark.asyncio
async def test_user_requested_no_stagger():
    """user 點的歌（非 Marvin）→ 不錯開，不插長等待。"""
    cog = _make_cog()
    cog.bot.router = MagicMock()
    cog.bot.router.generate_dynamic_system_msg = AsyncMock(return_value="DJ quip text")
    cog.bot.engine = MagicMock()
    cog.bot.engine.conv_buffer = MagicMock()
    cog.bot.engine.conv_buffer.get_last_n_utterances = MagicMock(return_value=[])
    slept = []

    async def _fake_sleep(s):
        slept.append(s)

    with patch("tts_length_policy.truncate_for_tts", return_value=("ok", False)), \
         patch("asyncio.sleep", side_effect=_fake_sleep):
        info = {'title': '天天', 'requested_by': 'Alice', '_round_position': 2}
        result = await cog._fetch_dj_interjection_raw(info)

    long_sleeps = [s for s in slept if s >= 3.0]
    assert not long_sleeps, f"user 點的歌不應長等待，得到: {long_sleeps}"
