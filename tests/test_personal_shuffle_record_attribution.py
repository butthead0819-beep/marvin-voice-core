"""個人歌單墊歌不再記成本人點播——requesters 自我放大根因修正。

根因：個人歌單池＝requesters 含該使用者的歌，墊歌時 info['requested_by'] = user，
stream loop 用 requested_by 呼叫 record_play → 點一次的歌每被個人歌單抽到一次就
+1 點播（自我放大，逢甲串燒 1 次真點播膨脹成 4）。修法：墊歌時額外標
info['_record_as']，record_play 記這個值而非 requested_by；requested_by 仍保持
user（控制卡頭像、DJ 都照舊要用）。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_cog(songs):
    """複用 test_personal_shuffle_cog.py 的 fake 方式。songs: list of (title, requesters_dict)。"""
    bot = MagicMock()
    bot.guilds = []
    _vc = MagicMock()
    _vc.is_connected.return_value = True
    bot.voice_clients = [_vc]
    bot.cogs.get.return_value = None
    bot.tts_engine = MagicMock()
    mm = MagicMock()
    data = {}
    for i, (title, requesters) in enumerate(songs):
        data[f"k{i}"] = {
            "title": title, "uploader": "u", "url": f"http://x/{title}",
            "webpage_url": f"http://yt/{title}", "requesters": dict(requesters),
        }
    mm.all_songs.return_value = data
    bot.music_memory = mm
    from cogs.music_cog import MusicCog
    cog = MusicCog(bot)
    cog.stream_mode = True

    async def _resolve(q):
        title = q.rsplit("/", 1)[-1]
        return {"title": title, "url": f"http://stream/{title}", "webpage_url": q}
    cog._resolve_yt_query = AsyncMock(side_effect=_resolve)
    return cog


def _personal_items(cog):
    return [it for it in cog.stream_queue if it.get("_lane") == "personal"]


@pytest.mark.asyncio
async def test_topup_marks_record_as_distinct_from_requested_by():
    cog = _make_cog([("A歌", {"阿明": 1}), ("B歌", {"阿明": 1})])
    await cog.start_personal_shuffle("阿明")
    items = _personal_items(cog)
    assert len(items) == 1
    info = items[0]
    assert info["requested_by"] == "阿明"
    assert info["_record_as"] == "Marvin推薦（為阿明）"
