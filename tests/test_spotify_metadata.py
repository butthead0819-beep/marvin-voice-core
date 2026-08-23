"""Spotify Search metadata 解析器測試（TDD）。

resolve_metadata 用可注入的 fetch，測試不真連網、不需要 Spotify token。驗證：
field-scoped 優先、相似度守門擋假陽性、自由文字備援、fail-safe（失敗/關 flag/
無標題一律回 None，不炸）。
"""
import pytest

import spotify_metadata


def _search_payload(track_name, artist_name, album_name="專輯", uri="spotify:track:abc"):
    return {"tracks": {"items": [{
        "name": track_name,
        "artists": [{"name": artist_name}],
        "album": {"name": album_name},
        "uri": uri,
    }]}}


def _capturing_fetch(payload):
    calls = []

    async def _fetch(query, **kw):
        calls.append(query)
        return payload

    _fetch.calls = calls
    return _fetch


def _routed_fetch(routes, default=None):
    """query 含某 substring 時回對應 payload；都不中回 default。"""
    calls = []

    async def _fetch(query, **kw):
        calls.append(query)
        for needle, payload in routes:
            if needle in query:
                return payload
        return default

    _fetch.calls = calls
    return _fetch


@pytest.mark.asyncio
async def test_field_scoped_confident_match():
    fetch = _capturing_fetch(_search_payload("龍捲風", "Jay Chou", "同名專輯", "spotify:track:xyz"))
    meta = await spotify_metadata.resolve_metadata(
        "周杰倫 Jay Chou【龍捲風 Tornado】-Official Music Video", "周杰倫 Jay Chou", fetch=fetch
    )
    assert meta == {"title": "龍捲風", "artist": "Jay Chou", "album": "同名專輯", "uri": "spotify:track:xyz"}
    assert any(q.startswith("track:") for q in fetch.calls)


@pytest.mark.asyncio
async def test_field_scoped_false_positive_is_rejected_then_free_text_used():
    # field query 語法對但語意不對（模擬撞到完全不相關曲目的假陽性）→ 相似度守門擋掉
    # → 落回自由文字，且自由文字這次回一筆真正相關的結果 → 採用
    fetch = _routed_fetch([
        ("track:", _search_payload("Orchestral Suite No. 2", "Bach")),
    ], default=_search_payload("不是花火呀", "某歌手", uri="spotify:track:real"))
    meta = await spotify_metadata.resolve_metadata("不是花火呀 - TA", "某頻道", fetch=fetch)
    assert meta is not None
    assert meta["title"] == "不是花火呀"


@pytest.mark.asyncio
async def test_low_confidence_free_text_returns_none():
    fetch = _capturing_fetch(_search_payload("Bohemian Rhapsody", "Queen"))
    meta = await spotify_metadata.resolve_metadata("完全不相干的抽象標題文字", "神秘頻道", fetch=fetch)
    assert meta is None


@pytest.mark.asyncio
async def test_empty_results_returns_none():
    fetch = _capturing_fetch({"tracks": {"items": []}})
    meta = await spotify_metadata.resolve_metadata("七里香", "周杰倫", fetch=fetch)
    assert meta is None


@pytest.mark.asyncio
async def test_fetch_failure_returns_none():
    async def _boom(query, **kw):
        return None

    meta = await spotify_metadata.resolve_metadata("七里香", "周杰倫", fetch=_boom)
    assert meta is None


@pytest.mark.asyncio
async def test_no_title_returns_none_without_fetch():
    async def _must_not_call(query, **kw):
        raise AssertionError("沒標題不該打 API")

    meta = await spotify_metadata.resolve_metadata("", "周杰倫", fetch=_must_not_call)
    assert meta is None


@pytest.mark.asyncio
async def test_disabled_flag_skips_and_returns_none(monkeypatch):
    monkeypatch.setenv("MARVIN_SPOTIFY_METADATA", "0")

    async def _must_not_call(query, **kw):
        raise AssertionError("關 flag 時不該打 API")

    meta = await spotify_metadata.resolve_metadata("七里香", "周杰倫", fetch=_must_not_call)
    assert meta is None
