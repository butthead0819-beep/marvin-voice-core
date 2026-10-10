"""新聞口白「講給誰聽」：news_context 把標題對應回搜它的在場興趣原句，
render 時注入給 LLM；沒有在場興趣就不抓新聞。"""
from __future__ import annotations

import pytest

import dj_topic_selector
from cogs.music_cog_dj_lyrics import MusicDJLyricsMixin
from dj_narration_orchestrator import DJMaterials, _render_news, plan_narration
from dj_topic_selector import TopicCooldownStore


def test_render_news_with_context_names_who_and_keeps_title():
    materials = DJMaterials(news_context={"Berlingo出露營套件": "showay喜歡露營車"})
    lines = _render_news("Berlingo出露營套件", materials)
    assert "showay喜歡露營車" in lines[0]
    assert "Berlingo出露營套件" in lines[0]
    assert "點名" in lines[1]
    assert "只能講標題裡寫的事" in lines[1]


def test_render_news_without_context_falls_back_to_legacy_text():
    materials = DJMaterials()
    lines = _render_news("某新聞標題", materials)
    assert lines[0].startswith("最新時事消息")


def test_plan_narration_news_mode_injects_who_its_for(tmp_path, monkeypatch):
    monkeypatch.setattr(dj_topic_selector, "MODE_WEIGHTS", {"news": 1.0})
    store = TopicCooldownStore(str(tmp_path / "news_store.json"))
    materials = DJMaterials(
        news_items=["Berlingo出露營套件"],
        news_context={"Berlingo出露營套件": "showay喜歡露營車"},
    )
    plan = plan_narration(materials, store)
    assert plan.mode == "news"
    assert "showay喜歡露營車" in "\n".join(plan.ctx_lines)


class _DummyDJ(MusicDJLyricsMixin):
    pass


@pytest.mark.asyncio
async def test_fetch_news_items_async_uses_present_interest_keyword(monkeypatch):
    calls = []

    async def _fake_fetch_news_headlines(keyword, **kw):
        calls.append(keyword)
        return ["A", "B"]

    import news_fetch
    monkeypatch.setattr(news_fetch, "fetch_news_headlines", _fake_fetch_news_headlines)

    dj = _DummyDJ()
    titles, context = await dj._fetch_news_items_async(["showay喜歡露營車"])
    assert titles == ["A", "B"]
    assert context == {"A": "showay喜歡露營車", "B": "showay喜歡露營車"}
    assert calls == ["露營車"]


@pytest.mark.asyncio
async def test_fetch_news_items_async_no_interests_skips_fetch(monkeypatch):
    async def _must_not_call(keyword, **kw):
        raise AssertionError("沒有在場興趣不該抓新聞")

    import news_fetch
    monkeypatch.setattr(news_fetch, "fetch_news_headlines", _must_not_call)

    dj = _DummyDJ()
    titles, context = await dj._fetch_news_items_async([])
    assert titles == []
    assert context == {}
