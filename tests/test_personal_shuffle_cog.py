"""TDD — 個人歌單連續隨機播（MusicCog 側）。

需求：一個指令讓使用者連續播他點過的全部歌，順序隨機、不重複、播完為止；
**一次只墊一首待播**，不塞爆佇列，別人現場點歌照樣進得來。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_cog(songs):
    """songs: list of (title, requesters_dict)。"""
    bot = MagicMock()
    bot.guilds = []
    _vc = MagicMock()
    _vc.is_connected.return_value = True   # 預設有連線語音（個人歌單要在語音內才跑）
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
    cog.stream_mode = True  # 已在串流 → start 不會另起真 loop task

    async def _resolve(q):
        title = q.rsplit("/", 1)[-1]
        return {"title": title, "url": f"http://stream/{title}", "webpage_url": q}
    cog._resolve_yt_query = AsyncMock(side_effect=_resolve)
    return cog


def _personal_items(cog):
    return [it for it in cog.stream_queue if it.get("_lane") == "personal"]


# ── start：建池只收他點過的歌 ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_start_pool_only_contains_users_own_songs():
    cog = _make_cog([("A歌", {"阿明": 2}), ("B歌", {"小華": 1}), ("C歌", {"阿明": 1, "小華": 1})])
    ok, _ = await cog.start_personal_shuffle("阿明")
    assert ok is True
    assert cog._personal_shuffle is not None
    # 阿明 的池 = A歌 + C歌（一首已墊進佇列、其餘在 remaining）
    queued = {it["title"] for it in _personal_items(cog)}
    remaining = {s["title"] for s in cog._personal_shuffle["remaining"]}
    assert queued | remaining == {"A歌", "C歌"}
    assert "B歌" not in (queued | remaining)


@pytest.mark.asyncio
async def test_start_empty_pool_returns_false_no_session():
    cog = _make_cog([("X", {"別人": 1})])
    ok, _ = await cog.start_personal_shuffle("阿明")
    assert ok is False
    assert cog._personal_shuffle is None
    assert cog.stream_queue == []


@pytest.mark.asyncio
async def test_start_enqueues_one_personal_song_at_tail():
    cog = _make_cog([("A歌", {"阿明": 1}), ("B歌", {"阿明": 1}), ("C歌", {"阿明": 1})])
    cog.stream_queue.append({"title": "別人點的", "url": "http://x/other", "requested_by": "小華"})
    await cog.start_personal_shuffle("阿明")
    items = _personal_items(cog)
    assert len(items) == 1
    assert items[0]["requested_by"] == "阿明"
    assert items[0]["_lane"] == "personal"
    # 墊在佇列尾（別人那首仍在前）
    assert cog.stream_queue[-1]["_lane"] == "personal"
    assert cog.stream_queue[0]["requested_by"] == "小華"


# ── 一次只墊一首（不塞爆佇列）─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_concurrent_topups_never_double_queue():
    """併發 bug 重現：stream loop 的 <2 分支會 fire-and-forget 噴多個 topup task，
    pending 檢查與 append 之間隔著慢 resolve → 兩個 topup 同時通過檢查各塞一首，
    佇列就有兩首個人歌（搶播根因）。單飛守衛要保證任何時刻只塞一首。"""
    cog = _make_cog([(t, {"阿明": 1}) for t in ["A", "B", "C", "D"]])
    cog._personal_shuffle = {"user": "阿明", "remaining": list(cog.bot.music_memory.all_songs().values())}

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_resolve(q):
        started.set()
        await release.wait()  # 撐開「檢查→append」之間的併發窗口
        t = q.rsplit("/", 1)[-1]
        return {"title": t, "url": f"http://stream/{t}", "webpage_url": q}
    cog._resolve_yt_query = slow_resolve

    t1 = asyncio.create_task(cog._personal_shuffle_topup())
    t2 = asyncio.create_task(cog._personal_shuffle_topup())
    await started.wait()
    release.set()
    await asyncio.gather(t1, t2)

    assert len(_personal_items(cog)) == 1, "併發 topup 不可雙塞，否則兩首搶播"


@pytest.mark.asyncio
async def test_topup_is_noop_when_personal_song_already_pending():
    cog = _make_cog([("A歌", {"阿明": 1}), ("B歌", {"阿明": 1}), ("C歌", {"阿明": 1})])
    await cog.start_personal_shuffle("阿明")  # 已墊 1 首
    await cog._personal_shuffle_topup()       # 再呼叫
    await cog._personal_shuffle_topup()       # 再呼叫
    assert len(_personal_items(cog)) == 1, "佇列裡永遠最多一首個人待播歌"


# ── 不重複、播完為止 ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_serves_each_song_exactly_once_then_ends():
    cog = _make_cog([("A歌", {"阿明": 1}), ("B歌", {"阿明": 1}), ("C歌", {"阿明": 1})])
    await cog.start_personal_shuffle("阿明")
    seen = []
    for _ in range(20):  # 上界保護
        if cog._personal_shuffle is None and not _personal_items(cog):
            break
        items = _personal_items(cog)
        if items:
            seen.append(items[0]["title"])
            cog.stream_queue.remove(items[0])  # 模擬播掉
        await cog._personal_shuffle_topup()
    assert sorted(seen) == ["A歌", "B歌", "C歌"], "每首恰好一次、不重複"
    assert cog._personal_shuffle is None, "播完後 session 收掉、回退一般推薦"


@pytest.mark.asyncio
async def test_topup_returns_false_and_clears_when_pool_exhausted():
    cog = _make_cog([("只有一首", {"阿明": 1})])
    await cog.start_personal_shuffle("阿明")  # 唯一一首已墊進佇列
    cog.stream_queue.clear()                   # 播掉它
    added = await cog._personal_shuffle_topup()
    assert added is False
    assert cog._personal_shuffle is None


# ── 停止：清掉 session ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stop_personal_shuffle_clears_session():
    cog = _make_cog([("A歌", {"阿明": 1})])
    await cog.start_personal_shuffle("阿明")
    assert cog.stop_personal_shuffle() is True
    assert cog._personal_shuffle is None
    assert cog.stop_personal_shuffle() is False  # 第二次：本來就沒在跑


@pytest.mark.asyncio
async def test_stop_personal_shuffle_purges_pending_personal_songs_from_queue():
    """結束個人歌單時，把佇列裡還沒播的個人墊位清掉 → 下一首立刻回一般推薦/主題。"""
    cog = _make_cog([("A歌", {"阿明": 1}), ("B歌", {"阿明": 1})])
    cog.stream_queue.insert(0, {"title": "別人點的", "url": "http://x/o", "requested_by": "小華"})
    await cog.start_personal_shuffle("阿明")  # 佇列尾多一首 personal
    assert any(it.get("_lane") == "personal" for it in cog.stream_queue)
    cog.stop_personal_shuffle()
    assert not any(it.get("_lane") == "personal" for it in cog.stream_queue), "個人墊位應被清掉"
    assert any(it.get("requested_by") == "小華" for it in cog.stream_queue), "別人點的歌保留"


@pytest.mark.asyncio
async def test_topup_clears_session_when_no_connected_voice():
    """bot 被 dismiss/撤離後沒有連線語音 → topup 要清掉 session、別讓 stream loop 一直
    churn 解析+跳過（2026-06-29 死鎖事故的相鄰根因：離開語音後 session 沒清）。"""
    cog = _make_cog([("A", {"阿明": 1}), ("B", {"阿明": 1})])
    await cog.start_personal_shuffle("阿明")
    assert cog._personal_shuffle is not None
    # 模擬離開語音：沒有連線中的 voice client
    cog.bot.voice_clients[0].is_connected.return_value = False
    added = await cog._personal_shuffle_topup()
    assert added is False
    assert cog._personal_shuffle is None, "無連線語音時要結束個人歌單 session"


@pytest.mark.asyncio
async def test_stop_stream_also_clears_personal_session():
    cog = _make_cog([("A歌", {"阿明": 1}), ("B歌", {"阿明": 1})])
    await cog.start_personal_shuffle("阿明")
    await cog.stop_stream("測試停止")
    assert cog._personal_shuffle is None
