"""_t2_discovery_candidates 多 seed 混合 wiring（2026-06-04）。

驗證 T2 從點播史聚合多 seed → 各跑 radio → blend。用 unbound method + stub self，
monkeypatch ytmusic_radio.ytmusic_radio 控制每 seed 輸出（blend 用真的）。
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest


def _import_vc():
    base = Path(__file__).parent.parent
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))
    from cogs.voice_controller import VoiceController
    return VoiceController


class _FakeMM:
    def __init__(self, played, liked):
        self._played = played
        self._liked = liked

    def get_played_seed_ids(self, members, limit=20):
        return self._played[:limit]

    def get_liked_video_ids(self, members):
        return self._liked


class _StubSelf:
    def __init__(self, mm, last=None):
        self.bot = types.SimpleNamespace(music_memory=mm)
        self._last_user_song_seed = last
        self._round_size = 3


@pytest.mark.asyncio
async def test_t2_blends_multiple_played_seeds(monkeypatch):
    VC = _import_vc()
    import ytmusic_radio

    calls = []

    def fake_radio(seed, exclude_titles=None, limit=None, **kw):
        calls.append(seed)
        # 每 seed 回不同歌
        return [{"title": f"{seed}-song", "artist": "x",
                 "url": f"http://y/{seed}"}]

    monkeypatch.setattr(ytmusic_radio, "ytmusic_radio", fake_radio)

    mm = _FakeMM(played=["aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc", "ddddddddddd"],
                 liked=[])
    stub = _StubSelf(mm)
    out = await VC._t2_discovery_candidates(stub, ["狗與露"], exclude_titles=[])

    # 取前 3 seed（_N_SEEDS），各 radio 一次
    assert len(calls) == 3
    # blend 後 3 首都在（交錯混合）
    titles = {c.anchor_title for c in out}
    assert titles == {"aaaaaaaaaaa-song", "bbbbbbbbbbb-song", "ccccccccccc-song"}


@pytest.mark.asyncio
async def test_t2_last_user_song_leads_seed_pool(monkeypatch):
    VC = _import_vc()
    import ytmusic_radio
    calls = []

    def fake_radio(seed, exclude_titles=None, limit=None, **kw):
        calls.append(seed)
        return [{"title": f"{seed}-s", "artist": "x", "url": f"http://y/{seed}"}]

    monkeypatch.setattr(ytmusic_radio, "ytmusic_radio", fake_radio)
    mm = _FakeMM(played=["bbbbbbbbbbb", "ccccccccccc"], liked=[])
    stub = _StubSelf(mm, last="zzzzzzzzzzz")          # 手動點的歌
    await VC._t2_discovery_candidates(stub, ["狗與露"], exclude_titles=[])
    assert calls[0] == "zzzzzzzzzzz"                  # 最近手動點排第一


@pytest.mark.asyncio
async def test_t2_single_seed_failure_skipped_not_fatal(monkeypatch):
    VC = _import_vc()
    import ytmusic_radio

    def fake_radio(seed, exclude_titles=None, limit=None, **kw):
        if seed == "bbbbbbbbbbb":
            raise RuntimeError("radio boom")
        return [{"title": f"{seed}-s", "artist": "x", "url": f"http://y/{seed}"}]

    monkeypatch.setattr(ytmusic_radio, "ytmusic_radio", fake_radio)
    mm = _FakeMM(played=["aaaaaaaaaaa", "bbbbbbbbbbb"], liked=[])
    out = await VC._t2_discovery_candidates(_StubSelf(mm), ["狗與露"], exclude_titles=[])
    titles = {c.anchor_title for c in out}
    assert titles == {"aaaaaaaaaaa-s"}               # 壞的跳過、好的留


@pytest.mark.asyncio
async def test_t2_empty_seeds_returns_empty(monkeypatch):
    VC = _import_vc()
    mm = _FakeMM(played=[], liked=[])
    out = await VC._t2_discovery_candidates(_StubSelf(mm), ["狗與露"], exclude_titles=[])
    assert out == []
