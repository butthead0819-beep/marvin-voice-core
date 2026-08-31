"""scripts/generate_song_jokes._gen_batch：LLM JSON → draft rows。"""
from __future__ import annotations

import pytest

pytest.importorskip("yaml")

from scripts.generate_song_jokes import _gen_batch

SONGS = [
    {"video_id": "aaa", "label": "周杰倫 告白氣球"},
    {"video_id": "bbb", "label": "五月天 溫柔"},
]


@pytest.mark.asyncio
async def test_parses_and_filters_unknown_ids():
    async def fake_call(user, *, system, caller, max_tokens=None):
        return ('垃圾前綴 {"jokes":['
                '{"video_id":"aaa","style":"puns","joke":"告白氣球笑話……唉"},'
                '{"video_id":"bbb","style":"skip","joke":""},'
                '{"video_id":"ZZZ","style":"puns","joke":"不在清單裡，要被丟掉"}'
                ']} 垃圾後綴')

    rows = await _gen_batch(SONGS, fake_call)

    assert [r["key"] for r in rows] == ["aaa", "bbb"]
    assert rows[0]["title"] == "周杰倫 告白氣球"
    assert rows[1]["style"] == "skip"


@pytest.mark.asyncio
async def test_bad_json_returns_empty():
    async def fake_call(user, *, system, caller, max_tokens=None):
        return "模型今天壞掉了，沒有 JSON"

    assert await _gen_batch(SONGS, fake_call) == []


@pytest.mark.asyncio
async def test_empty_response_returns_empty():
    async def fake_call(user, *, system, caller, max_tokens=None):
        return ""

    assert await _gen_batch(SONGS, fake_call) == []
