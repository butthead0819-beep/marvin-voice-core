"""
tests/test_context_injector.py
TDD 測試：ContextInjector.enrich() 行為驗證
"""
from __future__ import annotations

import pytest
import pytest_asyncio


# ── Fake 下層元件（不打真實 API / DB）────────────────────────────────────────

class FakeProfileCompressor:
    """回傳固定 profile 的假 compressor"""
    def __init__(self, profile: str | None = "這個人喜歡打遊戲"):
        self._profile = profile

    def get_profile(self, speaker: str, guild_id: int) -> str | None:
        return self._profile


class FakeVectorStore:
    """回傳固定片段的假 vector store"""
    def __init__(self, snippets: list[str] = None):
        self._snippets = snippets if snippets is not None else ["上次說想換工作"]

    def search(self, speaker: str, guild_id: int, query: str, top_k: int = 3) -> list[str]:
        return self._snippets


# ── 測試案例 ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_enrich_returns_empty_when_no_memory():
    """沒有 profile 也沒有向量結果 → 回傳空字串"""
    from context_injector import ContextInjector

    injector = ContextInjector(
        profile_compressor=FakeProfileCompressor(profile=None),
        vector_store=FakeVectorStore(snippets=[]),
    )
    result = await injector.enrich("小明", 123, "今天要幹嘛")
    assert result == ""


@pytest.mark.asyncio
async def test_enrich_includes_profile_when_exists():
    """有 profile → 結果包含 profile 文字"""
    from context_injector import ContextInjector

    injector = ContextInjector(
        profile_compressor=FakeProfileCompressor(profile="這個人喜歡打遊戲"),
        vector_store=FakeVectorStore(snippets=[]),
    )
    result = await injector.enrich("小明", 123, "今天要幹嘛")
    assert "這個人喜歡打遊戲" in result


@pytest.mark.asyncio
async def test_enrich_includes_vector_snippets():
    """向量搜尋有結果 → 結果包含那些片段"""
    from context_injector import ContextInjector

    injector = ContextInjector(
        profile_compressor=FakeProfileCompressor(profile=None),
        vector_store=FakeVectorStore(snippets=["上次說想換工作", "提到最近在學 Python"]),
    )
    result = await injector.enrich("小明", 123, "你有什麼計畫")
    assert "上次說想換工作" in result
    assert "提到最近在學 Python" in result


@pytest.mark.asyncio
async def test_enrich_format_has_header():
    """有記憶時，結果包含 【...的過去上下文】 header"""
    from context_injector import ContextInjector

    injector = ContextInjector(
        profile_compressor=FakeProfileCompressor(profile="某人某事"),
        vector_store=FakeVectorStore(snippets=["片段 A"]),
    )
    result = await injector.enrich("小明", 123, "隨便問一問")
    assert "【小明 的過去上下文】" in result


@pytest.mark.asyncio
async def test_enrich_profile_prefixed_with_label():
    """profile 那行應有『整體印象：』前綴"""
    from context_injector import ContextInjector

    injector = ContextInjector(
        profile_compressor=FakeProfileCompressor(profile="愛打遊戲的傢伙"),
        vector_store=FakeVectorStore(snippets=[]),
    )
    result = await injector.enrich("小華", 456, "你喜歡什麼")
    assert "整體印象：愛打遊戲的傢伙" in result


@pytest.mark.asyncio
async def test_enrich_no_header_when_empty():
    """完全沒有記憶時，不應該有 header"""
    from context_injector import ContextInjector

    injector = ContextInjector(
        profile_compressor=FakeProfileCompressor(profile=None),
        vector_store=FakeVectorStore(snippets=[]),
    )
    result = await injector.enrich("小美", 789, "隨便問")
    assert "【" not in result
