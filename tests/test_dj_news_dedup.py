"""重現 10/7 事故：新聞冷卻太短（舊版 2h）導致同一則新聞（Berlingo 露營套件）一天
講 14 次。驗證：抓多則候選 + 30 天冷卻後，短時間內（3 小時）不會重複選到同一則新聞。
"""
from __future__ import annotations

import dj_topic_selector
from dj_topic_selector import TopicCooldownStore, select_mode


def test_news_does_not_repeat_within_hours_after_new_cooldown(tmp_path, monkeypatch):
    monkeypatch.setattr(dj_topic_selector, "MODE_WEIGHTS", {"news": 1.0})
    t = [1_000_000.0]
    store = TopicCooldownStore(str(tmp_path / "cd.json"), now=lambda: t[0])
    news_items = ["Berlingo露營套件", "第二則"]

    topic1, mode1 = select_mode(
        life_cores=[], interests=[], store=store, news_items=news_items,
    )
    assert (topic1, mode1) == ("Berlingo露營套件", "news")

    # 舊版 2h 冷卻在這個時間點會放行，新版 30 天冷卻應該還在冷卻中
    t[0] += 3 * 3600
    topic2, mode2 = select_mode(
        life_cores=[], interests=[], store=store, news_items=news_items,
    )
    assert (topic2, mode2) == ("第二則", "news")
