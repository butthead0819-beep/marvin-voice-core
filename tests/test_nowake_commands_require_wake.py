"""指令一律要喚醒詞（2026-10-24 使用者定）。

10/23 12:51 狗與露跟 showay 聊「他找到那一句歌詞啊嗯」，沒叫馬文，debounce 免喚醒
路徑的 _FIND_SONG_GATE（找.*歌詞）命中 → find_song 0.90 → 直接放了一首歌。
process_debounced_speech 的免喚醒直派只留「問歌名」短問句（回答不是指令）；播/跳/停/暫停/繼續/找歌一律不派。
"""
from __future__ import annotations

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

from tests.test_followup_listening import _make_cog_for_stt


def _cog_with_bus():
    cog, _ = _make_cog_for_stt()
    cog._append_jsonl_log = AsyncMock()
    cog._intent_bus = MagicMock()
    cog._intent_bus.dispatch = AsyncMock(return_value=None)
    cog.stream_mode = True
    cog._current_stream_info = {"title": "x"}
    return cog


async def _run(cog, text):
    cog.speech_buffers["狗與露"] = {"texts": [text], "first_timestamp": 0.0, "wav_bytes": bytearray()}
    await cog.process_debounced_speech("狗與露")
    await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "他找到那一句歌詞啊嗯",   # 10/23 實際誤觸發句
    "下一首",
    "暫停",
    "繼續播",
    "我想聽周杰倫的晴天",
    "不過現在臺灣街頭藝人要抽稅",   # 10/19 舊 regex 碰「藝人」誤接
])
async def test_wakeless_command_not_dispatched(text):
    cog = _cog_with_bus()
    await _run(cog, text)
    cog._intent_bus.dispatch.assert_not_called()




@pytest.mark.asyncio
async def test_wakeless_short_song_question_still_dispatched():
    cog = _cog_with_bus()
    await _run(cog, "這首是什麼歌啊")
    cog._intent_bus.dispatch.assert_called_once()
    assert cog._intent_bus.dispatch.call_args.args[0].dispatch_source == "nowake"


@pytest.mark.asyncio
async def test_wakeless_song_question_ignored_when_not_streaming():
    cog = _cog_with_bus()
    cog.stream_mode = False
    await _run(cog, "這首是什麼歌啊")
    cog._intent_bus.dispatch.assert_not_called()
