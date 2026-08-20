"""測試 MusicCog 的響度正規化在 preload 與 prefetch 階段提前觸發，且解耦 _current_stream_info。"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cogs.music_cog import MusicCog


@pytest.mark.asyncio
async def test_measure_norm_gain_bg_uses_passed_args_not_current_stream():
    cog = MusicCog(bot=MagicMock())
    cog._current_stream_info = {"duration": 999.0, "title": "Old Song"}

    with patch("asyncio.create_subprocess_exec") as mock_exec, \
         patch("loudness_norm.sample_positions") as mock_sample_pos, \
         patch("loudness_norm.parse_ebur128_integrated", return_value=-14.0):

        mock_sample_pos.return_value = [50.0]
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"", b"Summary:\n I: -14.0 LUFS"))
        mock_exec.return_value = proc

        # 呼叫時顯式傳入 duration=200.0 與 highlight_start_s=50.0
        await cog._measure_norm_gain_bg(
            "https://test.url/song2",
            duration=200.0,
            highlight_start_s=50.0,
            info={"webpage_url": "https://test.url/song2", "duration": 200.0},
        )

        # 驗證 sample_positions 收到的是 200.0 與 start_s=50.0，而非舊歌的 999.0
        mock_sample_pos.assert_called_once_with(200.0, start_s=50.0)
        assert cog._stream_norm_gain["https://test.url/song2"] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_start_music_preload_triggers_gain_measurement():
    cog = MusicCog(bot=MagicMock())
    cog._measure_norm_gain_bg = AsyncMock()

    info = {
        "url": "https://test.url/next_song",
        "duration": 180.0,
        "highlight_start_s": 45.0,
    }

    with patch("local_mixing_source.preload_f32_source", return_value=MagicMock()):
        cog._start_music_preload(info)
        # 等待一小段讓 background task 跑起
        await asyncio.sleep(0.01)

    cog._measure_norm_gain_bg.assert_called_once_with(
        "https://test.url/next_song",
        duration=180.0,
        highlight_start_s=45.0,
        info=info,
    )
