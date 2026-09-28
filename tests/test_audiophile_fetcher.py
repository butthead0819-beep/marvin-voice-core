"""TDD：audiophile_fetcher.fetch_audiophile_guide（docs/PLAN_audiophile_music_tour.md Phase 2）。

設計：不自開 client、不寫死 model——重用 intent_agents.grounded_qa_agent.grounded_answer
（free→付費鏈 + PaidUsageGuard 記帳 + L1 拒答 / L2 無 grounding_chunks 幻覺 guard），
只換 system_prompt（build_audiophile_guide_prompt）與記帳 caller。

快取：SongKnowledgeStore 同一個 records/song_knowledge.json，但用獨立 key
「audiophile::<artist> - <title>」——不跟 get_or_extract_insight 的「<artist> - <title>」
記錄共用 dict（那邊 set 是整份覆寫，共用會互相洗掉欄位）。
失敗 → 保底台詞（不含任何事實），且**不寫快取**（下次還有機會查到真的）。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from song_knowledge_store import SongKnowledgeStore

GUIDE = "別被雙截棍的快嘴騙了，" + "仔細聽左聲道那把二胡，" * 6 + "戴上耳機。"
KEY = "audiophile::周杰倫 - 雙截棍"


def _resp(text, *, chunks=1, finish="STOP"):
    chunk_objs = [SimpleNamespace(web=SimpleNamespace(uri="https://example.com/a"))
                  for _ in range(chunks)]
    gm = SimpleNamespace(grounding_chunks=chunk_objs)
    cand = SimpleNamespace(grounding_metadata=gm, finish_reason=finish)
    return SimpleNamespace(
        text=text, candidates=[cand],
        usage_metadata=SimpleNamespace(prompt_token_count=100, candidates_token_count=50),
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
async def test_cache_hit_does_not_call_api(store):
    from audiophile_fetcher import fetch_audiophile_guide

    store.set(KEY, {"audiophile_guide": "快取裡的導聆台詞"})
    free = _client(_resp("不該被呼叫"))
    paid = _client(_resp("不該被呼叫"))

    out = await fetch_audiophile_guide("雙截棍", "周杰倫", free_client=free,
                                       paid_client=paid, guard=_guard(), store=store)

    assert out == "快取裡的導聆台詞"
    free.aio.models.generate_content.assert_not_awaited()
    paid.aio.models.generate_content.assert_not_awaited()


@pytest.mark.asyncio
async def test_cache_miss_calls_grounded_with_google_search_and_persists(store, tmp_path):
    from audiophile_fetcher import fetch_audiophile_guide
    from dj_prompt_builder import build_audiophile_guide_prompt

    # 同一首歌既有的賞析記錄（get_or_extract_insight 寫的）不可被洗掉
    store.set("周杰倫 - 雙截棍", {"lyricist": "方文山"})
    free = _client(_resp(GUIDE))

    out = await fetch_audiophile_guide("雙截棍", "周杰倫", free_client=free,
                                       paid_client=None, guard=_guard(), store=store)

    assert out == GUIDE
    free.aio.models.generate_content.assert_awaited_once()
    config = free.aio.models.generate_content.await_args.kwargs["config"]
    assert config.tools and config.tools[0].google_search is not None
    assert config.system_instruction == build_audiophile_guide_prompt("周杰倫 - 雙截棍")

    # 永久快取：重新從檔案讀也在
    reloaded = SongKnowledgeStore(path=str(tmp_path / "song_knowledge.json"))
    assert reloaded.get(KEY)["audiophile_guide"] == GUIDE
    assert reloaded.get("周杰倫 - 雙截棍") == {"lyricist": "方文山"}


@pytest.mark.asyncio
async def test_second_call_is_zero_api_cost(store):
    from audiophile_fetcher import fetch_audiophile_guide

    free = _client(_resp(GUIDE))
    for _ in range(2):
        out = await fetch_audiophile_guide("雙截棍", "周杰倫", free_client=free,
                                           paid_client=None, guard=_guard(), store=store)
        assert out == GUIDE
    free.aio.models.generate_content.assert_awaited_once()


@pytest.mark.asyncio
async def test_paid_call_recorded_with_audiophile_caller(store):
    from audiophile_fetcher import fetch_audiophile_guide

    free = _client(exc=RuntimeError("RESOURCE_EXHAUSTED"))
    paid = _client(_resp(GUIDE))
    guard = _guard(allow=True)

    out = await fetch_audiophile_guide("雙截棍", "周杰倫", free_client=free,
                                       paid_client=paid, guard=guard, store=store)

    assert out == GUIDE
    guard.record.assert_called()
    assert guard.record.call_args.kwargs["caller"] == "audiophile_guide"


@pytest.mark.asyncio
async def test_api_failure_returns_fallback_and_does_not_cache(store):
    from audiophile_fetcher import FALLBACK_GUIDE_TEMPLATE, fetch_audiophile_guide

    free = _client(exc=RuntimeError("boom"))

    out = await fetch_audiophile_guide("雙截棍", "周杰倫", free_client=free,
                                       paid_client=None, guard=_guard(), store=store)

    assert out == FALLBACK_GUIDE_TEMPLATE.format(title="雙截棍")
    assert "雙截棍" in out
    assert store.get(KEY) is None


@pytest.mark.asyncio
async def test_no_grounding_sources_returns_fallback(store):
    """L2 guard：沒真的搜到網頁（grounding_chunks 空）＝疑似幻覺 → 保底，不快取。"""
    from audiophile_fetcher import FALLBACK_GUIDE_TEMPLATE, fetch_audiophile_guide

    free = _client(_resp(GUIDE, chunks=0))

    out = await fetch_audiophile_guide("雙截棍", "周杰倫", free_client=free,
                                       paid_client=None, guard=_guard(), store=store)

    assert out == FALLBACK_GUIDE_TEMPLATE.format(title="雙截棍")
    assert store.get(KEY) is None


@pytest.mark.asyncio
async def test_llm_refusal_returns_fallback(store):
    """L1 guard：prompt 要求查不到就回「無」→ grounded_answer 擋下 → 保底。"""
    from audiophile_fetcher import FALLBACK_GUIDE_TEMPLATE, fetch_audiophile_guide

    free = _client(_resp("無"))

    out = await fetch_audiophile_guide("雙截棍", "周杰倫", free_client=free,
                                       paid_client=None, guard=_guard(), store=store)

    assert out == FALLBACK_GUIDE_TEMPLATE.format(title="雙截棍")
    assert store.get(KEY) is None


@pytest.mark.asyncio
async def test_no_artist_uses_title_only_key(store):
    from audiophile_fetcher import fetch_audiophile_guide

    free = _client(_resp(GUIDE))
    out = await fetch_audiophile_guide("雙截棍", "", free_client=free,
                                       paid_client=None, guard=_guard(), store=store)
    assert out == GUIDE
    assert store.get("audiophile::雙截棍")["audiophile_guide"] == GUIDE


def test_fallback_template_has_no_factual_claims():
    """保底台詞只准是「不多嘴、直接聽」這類無事實內容（說錯不如沒說）。"""
    from audiophile_fetcher import FALLBACK_GUIDE_TEMPLATE

    assert "{title}" in FALLBACK_GUIDE_TEMPLATE
    text = FALLBACK_GUIDE_TEMPLATE.format(title="X")
    assert "耳機" in text
    assert len(text) <= 40
