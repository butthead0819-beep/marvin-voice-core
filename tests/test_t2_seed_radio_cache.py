"""T2 SeedCache（2026-08-10）：ytmusicapi 1.12.0 KeyError('endpoint') 事故排查時發現，
單次 get_watch_playlist 已回全量(~50首)，但每輪只取 round_size*2 就丟棄剩下的——而 seed
輪替（seed_rotation.order_rotating_seeds）常見同一顆種子連續多輪被選中。加 TTL 快取：
同 seed 在 TTL 內重用原始結果，只在本地重套當下的 exclude_titles，省下重複 API 呼叫。
"""
from __future__ import annotations

import types

import pytest

from cogs.music_cog import MusicCog

pytestmark = pytest.mark.asyncio


class _StubSelf:
    def __init__(self):
        self._round_size = 3
        self._t2_seed_cache: dict = {}
        self._T2_SEED_CACHE_TTL_S = 3600


async def test_second_call_same_seed_hits_cache_no_api_call(monkeypatch):
    import ytmusic_radio

    calls = []

    def fake_radio(seed, exclude_titles=None, limit=None, **kw):
        calls.append(seed)
        return [{"title": f"{seed}-song{i}", "artist": "x", "url": f"http://y/{seed}/{i}"}
                for i in range(50)]

    monkeypatch.setattr(ytmusic_radio, "ytmusic_radio", fake_radio)
    stub = _StubSelf()

    out1 = await MusicCog._t2_radio_for_seed(stub, "seedaaaaaaa", [])
    out2 = await MusicCog._t2_radio_for_seed(stub, "seedaaaaaaa", [])

    assert len(calls) == 1                 # 第二次命中快取，沒有再打 API
    assert len(out1) == 6                  # 全量50首 → 隨機抽 round_size*2
    assert len(out2) == 6
    all_titles = {f"seedaaaaaaa-song{i}" for i in range(50)}
    assert {c["title"] for c in out1} <= all_titles
    assert {c["title"] for c in out2} <= all_titles


async def test_random_sampling_varies_across_rounds(monkeypatch):
    """快取住同一批後改隨機抽樣：連續多輪不該每次都抽到一模一樣的 6 首。"""
    import ytmusic_radio

    def fake_radio(seed, exclude_titles=None, limit=None, **kw):
        return [{"title": f"song{i}", "artist": "x", "url": f"http://y/{i}"} for i in range(50)]

    monkeypatch.setattr(ytmusic_radio, "ytmusic_radio", fake_radio)
    stub = _StubSelf()

    rounds = [tuple(sorted(c["title"] for c in await MusicCog._t2_radio_for_seed(stub, "seedaaaaaaa", [])))
              for _ in range(8)]
    assert len(set(rounds)) > 1            # 至少有兩輪抽到不同組合


async def test_different_seed_still_calls_api(monkeypatch):
    import ytmusic_radio

    calls = []

    def fake_radio(seed, exclude_titles=None, limit=None, **kw):
        calls.append(seed)
        return [{"title": f"{seed}-s", "artist": "x", "url": f"http://y/{seed}"}]

    monkeypatch.setattr(ytmusic_radio, "ytmusic_radio", fake_radio)
    stub = _StubSelf()

    await MusicCog._t2_radio_for_seed(stub, "seedaaaaaaa", [])
    await MusicCog._t2_radio_for_seed(stub, "seedbbbbbbb", [])

    assert calls == ["seedaaaaaaa", "seedbbbbbbb"]


async def test_cache_expired_calls_api_again(monkeypatch):
    import ytmusic_radio

    calls = []

    def fake_radio(seed, exclude_titles=None, limit=None, **kw):
        calls.append(seed)
        return [{"title": f"{seed}-s", "artist": "x", "url": f"http://y/{seed}"}]

    monkeypatch.setattr(ytmusic_radio, "ytmusic_radio", fake_radio)
    stub = _StubSelf()

    await MusicCog._t2_radio_for_seed(stub, "seedaaaaaaa", [])
    # 手動把快取時間戳打回過去，模擬 TTL 過期
    ts, raw = stub._t2_seed_cache["seedaaaaaaa"]
    stub._t2_seed_cache["seedaaaaaaa"] = (ts - 7200, raw)
    await MusicCog._t2_radio_for_seed(stub, "seedaaaaaaa", [])

    assert calls == ["seedaaaaaaa", "seedaaaaaaa"]


async def test_exclude_titles_reapplied_from_cache_without_refetch(monkeypatch):
    import ytmusic_radio

    calls = []

    def fake_radio(seed, exclude_titles=None, limit=None, **kw):
        calls.append(seed)
        return [{"title": "已排除的歌", "artist": "x", "url": "http://y/1"},
                {"title": "留下的歌", "artist": "x", "url": "http://y/2"}]

    monkeypatch.setattr(ytmusic_radio, "ytmusic_radio", fake_radio)
    stub = _StubSelf()

    out1 = await MusicCog._t2_radio_for_seed(stub, "seedaaaaaaa", [])
    assert {c["title"] for c in out1} == {"已排除的歌", "留下的歌"}

    # 換一批 exclude_titles，不該再打 API，但結果要反映新的排除
    out2 = await MusicCog._t2_radio_for_seed(stub, "seedaaaaaaa", ["已排除的歌"])
    assert len(calls) == 1
    assert {c["title"] for c in out2} == {"留下的歌"}
