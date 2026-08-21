"""TDD — DJ 串場話題選擇器：拆開話題來源＋8 小時話題冷卻。"""
from __future__ import annotations

from dj_topic_selector import COOLDOWN_S, TopicCooldownStore, select_topic


def _clock():
    t = [1000.0]
    return t, (lambda: t[0])


def test_select_topic_prefers_life_over_interest(tmp_path):
    store = TopicCooldownStore(str(tmp_path / "c.json"))
    topic, kind = select_topic(["昨天去爬山"], ["喜歡周杰倫"], store)
    assert (topic, kind) == ("昨天去爬山", "life")


def test_select_topic_falls_back_to_interest_when_no_life(tmp_path):
    store = TopicCooldownStore(str(tmp_path / "c.json"))
    topic, kind = select_topic([], ["喜歡周杰倫"], store)
    assert (topic, kind) == ("喜歡周杰倫", "interest")


def test_select_topic_none_when_nothing_available(tmp_path):
    store = TopicCooldownStore(str(tmp_path / "c.json"))
    topic, kind = select_topic([], [], store)
    assert (topic, kind) == (None, "none")


def test_selected_topic_goes_on_cooldown_and_falls_through(tmp_path):
    t, now = _clock()
    store = TopicCooldownStore(str(tmp_path / "c.json"), now=now)
    topic, kind = select_topic(["昨天去爬山"], ["喜歡周杰倫"], store)
    assert kind == "life"
    # 同一話題還在冷卻中 → 該生活話題不能再被選，退到興趣
    topic2, kind2 = select_topic(["昨天去爬山"], ["喜歡周杰倫"], store)
    assert (topic2, kind2) == ("喜歡周杰倫", "interest")


def test_topic_becomes_available_again_after_cooldown_expires(tmp_path):
    t, now = _clock()
    store = TopicCooldownStore(str(tmp_path / "c.json"), now=now)
    select_topic(["昨天去爬山"], [], store)
    t[0] += COOLDOWN_S - 1
    assert select_topic(["昨天去爬山"], [], store) == (None, "none")  # 還沒滿 8 小時
    t[0] += 2
    assert select_topic(["昨天去爬山"], [], store) == ("昨天去爬山", "life")  # 滿了可再用


def test_cooldown_persists_across_store_instances(tmp_path):
    t, now = _clock()
    path = str(tmp_path / "c.json")
    store1 = TopicCooldownStore(path, now=now)
    select_topic(["昨天去爬山"], [], store1)
    store2 = TopicCooldownStore(path, now=now)  # 模擬重啟後重新載入
    assert select_topic(["昨天去爬山"], [], store2) == (None, "none")


def test_multiple_life_cores_second_used_when_first_on_cooldown(tmp_path):
    store = TopicCooldownStore(str(tmp_path / "c.json"))
    select_topic(["事件A"], [], store)
    topic, kind = select_topic(["事件A", "事件B"], [], store)
    assert (topic, kind) == ("事件B", "life")


def test_corrupt_cache_file_fails_open(tmp_path):
    path = tmp_path / "c.json"
    path.write_text("not json", encoding="utf-8")
    store = TopicCooldownStore(str(path))
    assert select_topic(["昨天去爬山"], [], store) == ("昨天去爬山", "life")


# ── emotional_highlight：第三優先，life/interest 之後 ────────────────────────

def test_select_topic_falls_back_to_emotional_highlight_when_no_life_or_interest(tmp_path):
    store = TopicCooldownStore(str(tmp_path / "c.json"))
    topic, kind = select_topic([], [], store, ["上次你說覺得被理解那句話"])
    assert (topic, kind) == ("上次你說覺得被理解那句話", "emotional_highlight")


def test_select_topic_prefers_interest_over_emotional_highlight(tmp_path):
    store = TopicCooldownStore(str(tmp_path / "c.json"))
    topic, kind = select_topic([], ["喜歡周杰倫"], store, ["上次的感動瞬間"])
    assert (topic, kind) == ("喜歡周杰倫", "interest")


def test_emotional_highlight_goes_on_cooldown(tmp_path):
    store = TopicCooldownStore(str(tmp_path / "c.json"))
    select_topic([], [], store, ["同一個瞬間"])
    topic, kind = select_topic([], [], store, ["同一個瞬間"])
    assert (topic, kind) == (None, "none")


def test_no_emotional_highlights_falls_through_to_none(tmp_path):
    store = TopicCooldownStore(str(tmp_path / "c.json"))
    assert select_topic([], [], store) == (None, "none")
    assert select_topic([], [], store, None) == (None, "none")
    assert select_topic([], [], store, []) == (None, "none")
