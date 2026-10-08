"""DJ 口白行為護欄（註冊表第 2 刀）——冷卻表/副作用契約。

用真的 `dj_narration_orchestrator.select_narration_mode` + 真的
`dj_topic_selector.TopicCooldownStore`，鎖死「記憶對歌命中不燒 life/interest
冷卻」「focus=song 完全不寫狀態」「扭蛋抽中才 mark_used + 設 last_fallback」
「autopilot 理由覆蓋 quick/atmosphere 但 last_fallback 仍記錄抽獎當下的原始
mode」這幾條現行契約——第 3 刀搬進註冊表時要維持這些行為不變。
"""
from __future__ import annotations

import dj_topic_selector
from dj_narration_orchestrator import select_narration_mode
from dj_topic_selector import TopicCooldownStore


def _store(tmp_path, **kw):
    return TopicCooldownStore(str(tmp_path / "cd.json"), **kw)


def test_memory_match_hit_leaves_life_interest_untouched(tmp_path):
    store = _store(tmp_path)
    topic, mode = select_narration_mode(
        life=["Alice 學日文"],
        interests=["登山"],
        topic_store=store,
        memory_evidence="Alice 說過最愛這首",
    )
    assert (topic, mode) == ("Alice 說過最愛這首", "memory_match")
    assert store.is_cool("Alice 學日文") is True
    assert store.is_cool("登山") is True
    assert store.get_last_fallback() is None
    assert store.is_cool("Alice 說過最愛這首") is False  # 命中的證據本身要進冷卻


def test_focus_song_writes_nothing(tmp_path):
    store = _store(tmp_path)
    topic, mode = select_narration_mode(
        life=["Alice 學日文"],
        interests=["登山"],
        topic_store=store,
        focus="song",
    )
    assert (topic, mode) == (None, "song")
    assert store._data == {}


def test_gacha_life_marks_topic_and_last_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(dj_topic_selector, "MODE_WEIGHTS", {"life": 1.0})
    store = _store(tmp_path)
    topic, mode = select_narration_mode(
        life=["Alice 學日文"],
        interests=[],
        topic_store=store,
    )
    assert (topic, mode) == ("Alice 學日文", "life")
    assert store.is_cool("Alice 學日文") is False
    assert store.get_last_fallback() == "life"


def test_gacha_news_uses_news_cooldown(tmp_path, monkeypatch):
    monkeypatch.setattr(dj_topic_selector, "MODE_WEIGHTS", {"news": 1.0})
    t = [1_000_000.0]
    store = _store(tmp_path, now=lambda: t[0])
    topic, mode = select_narration_mode(
        life=[],
        interests=[],
        topic_store=store,
        news_items=["颱風轉向"],
    )
    assert (topic, mode) == ("颱風轉向", "news")
    assert store.is_cool("颱風轉向", cooldown_s=dj_topic_selector.NEWS_COOLDOWN_S) is False
    t[0] += dj_topic_selector.NEWS_COOLDOWN_S + 1
    assert store.is_cool("颱風轉向", cooldown_s=dj_topic_selector.NEWS_COOLDOWN_S) is True


def test_reason_override_keeps_last_fallback_quick(tmp_path, monkeypatch):
    monkeypatch.setattr(dj_topic_selector, "MODE_WEIGHTS", {"quick": 1.0})
    store = _store(tmp_path)
    topic, mode = select_narration_mode(
        life=[],
        interests=[],
        topic_store=store,
        autopilot_reason="理由",
    )
    assert (topic, mode) == (None, "reason")
    assert store.get_last_fallback() == "quick"
    assert set(store._data.keys()) == {"_last_fallback_mode"}


def test_reason_does_not_override_topic_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(dj_topic_selector, "MODE_WEIGHTS", {"life": 1.0})
    store = _store(tmp_path)
    topic, mode = select_narration_mode(
        life=["X"],
        interests=[],
        topic_store=store,
        autopilot_reason="理由",
    )
    assert mode == "life"


def test_excluded_memory_match_falls_to_gacha(tmp_path, monkeypatch):
    monkeypatch.setattr(dj_topic_selector, "MODE_WEIGHTS", {"life": 1.0})
    store = _store(tmp_path)
    topic, mode = select_narration_mode(
        life=["X"],
        interests=[],
        topic_store=store,
        memory_evidence="E",
        exclude_modes=("memory_match",),
    )
    assert mode == "life"
    assert store.is_cool("E") is True


def test_news_reenters_pool_after_news_cooldown_but_before_topic_cooldown(tmp_path, monkeypatch):
    """新聞用 2h 冷卻（不是話題的 8h）：抽過的新聞 2h 後就能再進池。"""
    monkeypatch.setattr(dj_topic_selector, "MODE_WEIGHTS", {"news": 1.0, "quick": 1.0})
    t = [1_000_000.0]
    store = TopicCooldownStore(path=str(tmp_path / "cd.json"), now=lambda: t[0])
    store.mark_used("颱風轉向")
    t[0] += dj_topic_selector.NEWS_COOLDOWN_S + 1  # 過了 2h、還不到 8h
    assert dj_topic_selector.NEWS_COOLDOWN_S + 1 < dj_topic_selector.COOLDOWN_S
    _topic, mode = select_narration_mode(
        life=[], interests=[], topic_store=store, news_items=["颱風轉向"])
    assert mode == "news"
