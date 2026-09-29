"""TDD：audiophile_fetcher.fetch_song_card 單次聚合卡片 Ingestion 測試。

驗證：
1. 快取命中零 API 呼叫。
2. 舊版快取向下相容（已有 audiophile_guide 時不重打 API）。
3. 快取未命中時，以單次 Grounding 呼叫獲取聽覺幕後、社群熱評、歌詞刺點並快取。
4. 查無資料或格式不符時回傳 None 且不污染快取。
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from song_knowledge_store import SongKnowledgeStore

KEY = "audiophile::周杰倫 - 晴天"


def _resp(text: str, *, chunks: int = 1):
    chunk_objs = [
        SimpleNamespace(web=SimpleNamespace(uri="https://example.com/a"))
        for _ in range(chunks)
    ]
    gm = SimpleNamespace(grounding_chunks=chunk_objs)
    cand = SimpleNamespace(grounding_metadata=gm, finish_reason="STOP")
    return SimpleNamespace(
        text=text,
        candidates=[cand],
        usage_metadata=SimpleNamespace(prompt_token_count=120, candidates_token_count=80),
    )


def _client(resp=None, exc=None):
    cli = MagicMock()
    if exc is not None:
        cli.aio.models.generate_content = AsyncMock(side_effect=exc)
    else:
        cli.aio.models.generate_content = AsyncMock(return_value=resp)
    return cli


def _guard(allow=True):
    g = MagicMock()
    g.allow.return_value = allow
    g.record = MagicMock()
    return g


@pytest.fixture
def store(tmp_path):
    return SongKnowledgeStore(path=str(tmp_path / "song_knowledge.json"))


@pytest.mark.asyncio
async def test_fetch_song_card_cache_hit(store):
    from audiophile_fetcher import fetch_song_card

    store.set(KEY, {
        "audiophile_guide": "前奏木吉他刷弦一出來，就是整個千禧年代的校園回憶。",
        "lyric_hook": {"quote": "從前從前有個人愛妳很久", "subtext": "青春無疾而終的遺憾"},
        "ts": time.time(),
    })
    free = _client(_resp("不應呼叫"))
    paid = _client(_resp("不應呼叫"))

    card = await fetch_song_card(
        "晴天", "周杰倫",
        free_client=free, paid_client=paid, guard=_guard(), store=store,
    )

    assert card is not None
    assert "前奏木吉他刷弦" in card["audiophile_guide"]
    assert card["lyric_hook"]["quote"] == "從前從前有個人愛妳很久"
    free.aio.models.generate_content.assert_not_awaited()
    paid.aio.models.generate_content.assert_not_awaited()


@pytest.mark.asyncio
async def test_fetch_song_card_legacy_cache_compatibility(store):
    from audiophile_fetcher import fetch_song_card

    # 模擬以前只存了 audiophile_guide 的舊快取
    store.set(KEY, {
        "audiophile_guide": "舊版導聆台詞",
        "ts": time.time(),
    })
    free = _client(_resp("不應呼叫"))

    card = await fetch_song_card(
        "晴天", "周杰倫",
        free_client=free, paid_client=None, guard=_guard(), store=store,
    )

    assert card is not None
    assert card["audiophile_guide"] == "舊版導聆台詞"
    assert card["lyric_hook"] is None
    free.aio.models.generate_content.assert_not_awaited()


@pytest.mark.asyncio
async def test_fetch_song_card_miss_and_persist(store):
    from audiophile_fetcher import fetch_song_card

    raw_response = (
        "【聽覺與幕後】：周杰倫這首《晴天》，開頭故意保留下雨採樣與木吉他交織，注意聲場中大提琴的哀傷拉奏。戴上耳機感受這段青春。\n"
        "【社群熱評標籤】：標籤：千禧年校園回憶神曲 | 情境：下課鈴聲響起窗外的陣雨\n"
        "【歌詞刺點】：句：從前從前有個人愛妳很久 | 時：02:14 | 析：青澀暗戀裡最痛但最溫柔的一聲嘆息"
    )
    free = _client(_resp(raw_response))

    card = await fetch_song_card(
        "晴天", "周杰倫",
        lyrics="[02:14.00] 從前從前有個人愛妳很久",
        free_client=free, paid_client=None, guard=_guard(), store=store,
    )

    assert card is not None
    assert "下雨採樣" in card["audiophile_guide"]
    assert card["lyric_hook"]["quote"] == "從前從前有個人愛妳很久"

    # 確認寫入 store 快取
    saved = store.get(KEY)
    assert saved is not None
    assert saved["audiophile_guide"] == card["audiophile_guide"]
    assert saved["lyric_hook"] == card["lyric_hook"]
    assert "sources" in saved


@pytest.mark.asyncio
async def test_fetch_song_card_invalid_response_does_not_cache(store):
    from audiophile_fetcher import fetch_song_card

    # 回傳「無」或無效內容
    free = _client(_resp("無"))

    card = await fetch_song_card(
        "晴天", "周杰倫",
        free_client=free, paid_client=None, guard=_guard(), store=store,
    )

    assert card is None
    assert store.get(KEY) is None
