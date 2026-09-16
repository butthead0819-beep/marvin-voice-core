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


def test_two_instances_mark_used_do_not_clobber_each_other(tmp_path):
    """Phase B 遷移到 StateStore.update() 後的 race 回歸測試。

    舊實作（手刻 _load/_save，__init__ 時 load 一次進 self._data，mark_used
    改完整份 self._data 就整份覆寫）在這個情境下會丟資料：store2 在 store1
    mark_used 之前就已經 __init__（讀到空檔案），store2.mark_used() 時
    self._data 仍是它自己那份「空」的 stale copy，整份存回去會把 store1
    剛寫入的 key 蓋掉。

    改用 StateStore.update() 之後，mark_used() 內部一定在拿到鎖之後才重新
    從 disk load「當下最新」資料，不吃呼叫方自己的 stale in-memory cache，
    所以兩邊的 mutation 都會保留。
    """
    path = str(tmp_path / "c.json")
    store1 = TopicCooldownStore(path)
    store2 = TopicCooldownStore(path)  # 跟 store1 同時 __init__，各自讀到空檔案

    store1.mark_used("話題A")
    store2.mark_used("話題B")  # 舊實作會用 store2 的 stale self._data 蓋掉話題A

    # 用第三個全新 instance 重新從 disk load，驗證兩次 mark_used 都留下來了。
    store3 = TopicCooldownStore(path)
    assert store3.is_cool("話題A") is False
    assert store3.is_cool("話題B") is False


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


# ── news：新聞模式與 2 小時冷卻 ──────────────────────────────────────

def test_select_topic_picks_news_when_available(tmp_path):
    store = TopicCooldownStore(str(tmp_path / "c.json"))
    topic, kind = select_topic([], [], store, emotional_highlights=[], news_items=["台積電新廠完工"])
    assert (topic, kind) == ("台積電新廠完工", "news")


def test_news_cooldown_is_2_hours(tmp_path):
    from dj_topic_selector import NEWS_COOLDOWN_S
    assert NEWS_COOLDOWN_S == 2 * 3600

    t, now = _clock()
    store = TopicCooldownStore(str(tmp_path / "c.json"), now=now)
    select_topic([], [], store, news_items=["台積電新廠完工"])

    # 1 小時後：仍在冷卻中
    t[0] += 3600
    assert select_topic([], [], store, news_items=["台積電新廠完工"]) == (None, "none")

    # 滿 2 小時（+3601s）：冷卻結束，可再次使用
    t[0] += 3601
    assert select_topic([], [], store, news_items=["台積電新廠完工"]) == ("台積電新廠完工", "news")

