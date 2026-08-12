"""TDD: DJ 串場 prev_title 用 round 內排序提示，別靠 prefetch 當下的 stream_history

背景：round 內同批 enqueue 的歌曲會在 enqueue 當下就並行觸發 DJ 文案 prefetch，
但 stream_history 只在歌曲「真正開始播放」才 append。round 中第 2、3 首歌 prefetch
當下，round 第 1 首根本還沒播完，stream_history 抓到的是上一輪的舊歷史，導致 DJ
唸出的「上一首」跟實際播放順序對不上（甚至同一輪多首歌重複報同一個 prev_title）。

修法：round enqueue 迴圈把同輪前一位置的標題存進 info['_prev_title_hint']，
_fetch_dj_interjection_raw 優先採用這個 hint，而不是去查可能過期的 stream_history。
"""
from __future__ import annotations
import pytest
from unittest.mock import MagicMock, patch


def _make_cog(stream_history):
    from cogs.music_cog import MusicCog
    cog = MusicCog.__new__(MusicCog)

    bot = MagicMock()
    bot.music_memory = None
    bot.tts_engine = MagicMock()
    bot.tts_engine.get_estimated_duration = MagicMock(return_value=3.0)

    async def _fake_generate(t):
        return None
    bot.tts_engine.generate_audio = _fake_generate
    cog.bot = bot
    cog.stream_history = stream_history

    cog._parse_song_title_artist = MagicMock(return_value=("B", ""))
    return cog


@pytest.mark.asyncio
async def test_round_hint_overrides_stale_stream_history():
    """round 第 2 首歌帶 _prev_title_hint 時，就算 stream_history 還停在上一輪的歌，
    prev_title 仍應採用 hint（本輪第 1 首），而不是 stream_history 的舊資料。"""
    # stream_history 還停在「上一輪最後一首」，round 第 1、2 首都還沒真正播放。
    cog = _make_cog(stream_history=[{'title': '上一輪最後一首'}])

    info = {
        'title': 'B',
        'requested_by': 'Marvin推薦（為Alice）',
        '_spotlight': 'Alice',
        '_lane': 'spotlight',
        '_round_first': False,
        '_round_position': 1,
        'url': 'http://fake/1',
        '_prev_title_hint': 'A',  # round 內第 1 首歌的標題
    }

    captured_ctx = {}

    async def _fake_generate_dynamic_system_msg(kind, context):
        captured_ctx['context'] = context
        return 'DJ 文案'

    cog.bot.router = MagicMock()
    cog.bot.router.generate_dynamic_system_msg = _fake_generate_dynamic_system_msg
    cog.bot.engine = MagicMock()
    cog.bot.engine.conv_buffer = MagicMock()
    cog.bot.engine.conv_buffer.get_last_n_utterances = MagicMock(return_value=[])

    async def _fake_sleep(s):
        return None

    with patch("tts_length_policy.truncate_for_tts", return_value=("DJ 文案", False)), \
         patch("asyncio.sleep", side_effect=_fake_sleep):
        result = await cog._fetch_dj_interjection_raw(info)

    assert result is not None
    ctx = captured_ctx.get('context', '')
    assert '《A》' in ctx, f"prev_title 應採用 round hint『A』，實際 context: {ctx}"
    assert '上一輪最後一首' not in ctx, f"不該用過期的 stream_history，實際 context: {ctx}"


@pytest.mark.asyncio
async def test_round_first_falls_back_to_stream_history():
    """round 首曲沒有 hint → 沿用原本 stream_history 反查邏輯（行為不變）。"""
    cog = _make_cog(stream_history=[{'title': '真正的上一首'}])

    info = {
        'title': 'A',
        'requested_by': 'Marvin推薦（為Alice）',
        '_spotlight': 'Alice',
        '_lane': 'spotlight',
        '_round_first': True,
        '_round_position': 0,
        'url': 'http://fake/0',
    }

    captured_ctx = {}

    async def _fake_generate_dynamic_system_msg(kind, context):
        captured_ctx['context'] = context
        return 'DJ 文案'

    cog.bot.router = MagicMock()
    cog.bot.router.generate_dynamic_system_msg = _fake_generate_dynamic_system_msg
    cog.bot.engine = MagicMock()
    cog.bot.engine.conv_buffer = MagicMock()
    cog.bot.engine.conv_buffer.get_last_n_utterances = MagicMock(return_value=[])

    async def _fake_sleep(s):
        return None

    with patch("tts_length_policy.truncate_for_tts", return_value=("DJ 文案", False)), \
         patch("asyncio.sleep", side_effect=_fake_sleep):
        result = await cog._fetch_dj_interjection_raw(info)

    assert result is not None
    ctx = captured_ctx.get('context', '')
    assert '《真正的上一首》' in ctx, f"沒有 hint 時應 fallback 用 stream_history，實際 context: {ctx}"
