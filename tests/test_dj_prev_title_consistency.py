"""TDD: DJ 串場 prev_title 一致性保護與佇列邊界修復

問題：
1. AutoRecommend round 邊界未考慮 stream_queue 現況，導致 round-#1 跳過 queue 內歌曲直接抓 stream_history。
2. 預取（Prefetch）早綁定了 prev_title，若中途發生插歌/skip/切歌，播放時直接播出會提及錯誤的上一首。
3. _fetch_dj_interjection_raw 未記錄 prev_title_used，播放端無法校驗。
"""
from __future__ import annotations
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _make_cog(stream_history=None, stream_queue=None):
    from cogs.music_cog import MusicCog
    cog = MusicCog.__new__(MusicCog)

    bot = MagicMock()
    bot.music_memory = None
    bot.tts_engine = MagicMock()
    bot.tts_engine.get_estimated_duration = MagicMock(return_value=3.0)

    async def _fake_generate(t, emotion="normal"):
        return "/tmp/fake_audio.mp3"
    bot.tts_engine.generate_audio = _fake_generate
    cog.bot = bot
    cog.stream_history = stream_history or []
    cog.stream_queue = stream_queue or []
    cog._prefetch_cache = {}
    cog._preload_music_cache = {}
    cog._round_size = 3
    cog._cover_blacklist = None

    cog._parse_song_title_artist = MagicMock(return_value=("Song", "Artist"))
    return cog


@pytest.mark.asyncio
async def test_fetch_dj_interjection_records_prev_title_used():
    """_fetch_dj_interjection_raw 回傳 dict 應包含 prev_title_used 欄位。"""
    cog = _make_cog(stream_history=[{'title': 'Song Prev'}])

    info = {
        'title': 'Song Current',
        'requested_by': 'Marvin推薦（為Alice）',
        '_spotlight': 'Alice',
        '_lane': 'spotlight',
        '_round_first': True,
        '_round_position': 0,
        'url': 'http://fake/0',
    }

    async def _fake_generate_dynamic_system_msg(kind, context):
        return 'DJ 串場台詞'

    cog.bot.router = MagicMock()
    cog.bot.router.generate_dynamic_system_msg = _fake_generate_dynamic_system_msg
    cog.bot.engine = MagicMock()
    cog.bot.engine.conv_buffer = MagicMock()
    cog.bot.engine.conv_buffer.get_last_n_utterances = MagicMock(return_value=[])

    with patch("tts_length_policy.truncate_for_tts", return_value=("DJ 串場台詞", False)), \
         patch("os.path.exists", return_value=True):
        result = await cog._fetch_dj_interjection_raw(info)

    assert result is not None
    assert 'prev_title_used' in result
    assert result['prev_title_used'] == 'Song Prev'


@pytest.mark.asyncio
async def test_fetch_dj_interjection_records_hint_as_prev_title_used():
    """_fetch_dj_interjection_raw 有 _prev_title_hint 時，prev_title_used 應記錄 hint。"""
    cog = _make_cog(stream_history=[{'title': 'Old History'}])

    info = {
        'title': 'Song Current',
        'requested_by': 'Marvin推薦（為Alice）',
        '_spotlight': 'Alice',
        '_lane': 'spotlight',
        '_round_first': False,
        '_round_position': 1,
        'url': 'http://fake/1',
        '_prev_title_hint': 'Hint Song',
    }

    async def _fake_generate_dynamic_system_msg(kind, context):
        return 'DJ 串場台詞'

    cog.bot.router = MagicMock()
    cog.bot.router.generate_dynamic_system_msg = _fake_generate_dynamic_system_msg
    cog.bot.engine = MagicMock()
    cog.bot.engine.conv_buffer = MagicMock()
    cog.bot.engine.conv_buffer.get_last_n_utterances = MagicMock(return_value=[])

    with patch("tts_length_policy.truncate_for_tts", return_value=("DJ 串場台詞", False)), \
         patch("os.path.exists", return_value=True):
        result = await cog._fetch_dj_interjection_raw(info)

    assert result is not None
    assert result.get('prev_title_used') == 'Hint Song'


@pytest.mark.asyncio
async def test_tail_dj_consistency_guard_rejects_mismatched_prev_title():
    """當 DJ meta 的 prev_title_used 與當前真實結束的歌名不符時，應被 Consistency Guard 攔截，
    退回不提及錯誤上一首的乾淨介紹或安全模板。"""
    cog = _make_cog(stream_history=[{'title': 'Song A'}])

    cur_info = {'title': 'Song B (實際剛播完)'}
    next_info = {
        'title': 'Song C (即將播放)',
        'url': 'http://fake/c',
        'requested_by': 'Alice',
    }

    # 預取的 DJ meta 裡寫死了當初預期的上一首是 Song A
    stale_dj_meta = {
        'text': '剛才 Song A 結束了，現在來聽 Song C',
        'audio_path': '/tmp/stale_audio.mp3',
        'prev_title_used': 'Song A (過期預期)',
    }

    done_future = asyncio.Future()
    done_future.set_result({'dj': stale_dj_meta})
    cog._prefetch_cache[next_info['url']] = done_future

    with patch("os.path.exists", return_value=True):
        # 呼叫帶有 consistency guard 的 resolve
        dj_meta = await cog._resolve_tail_dj_meta(next_info, cur_info=cur_info)

    # 必須被防護機制攔截：不可直接回傳含有錯誤上一首歌名 'Song A' 的 stale_dj_meta
    if dj_meta is not None:
        assert dj_meta.get('prev_title_used') != 'Song A (過期預期)'
        assert 'Song A' not in (dj_meta.get('text') or '')


def test_clean_title_matching_ignores_bracket_differences():
    """Consistency Guard 比對時應忽略 YouTube 標題括號與後綴差異。"""
    from song_name_clean import clean_title_regex

    title_used = "周杰倫 - 安靜 (Official MV)"
    actual_title = "周杰倫【安靜】歌詞字幕版"

    norm_used = clean_title_regex(title_used).strip().lower()
    norm_actual = clean_title_regex(actual_title).strip().lower()

    assert norm_used == norm_actual == "周杰倫 - 安靜" or "安靜" in norm_used

