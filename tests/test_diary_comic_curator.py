"""策展層：一夜 → CurationPlan（輸出與呈現解耦，漫畫/有聲書共用）。忠實、不輪替。"""
from diary_comic.parser import DiaryEntry
from diary_comic.curator import curate, CuratorConditions, CurationPlan


def _entries(n, speakers=("狗與露", "showay", "陳進文", "weakgogo")):
    return [DiaryEntry(ts_str=f"2026-06-22 22:{i*3:02d}:00", core=f"聊主題{i}號的細節",
                       speakers=list(speakers)) for i in range(n)]


def _crosstalk_rows(base=1718000000.0):
    """4 人場、3 人在 2 秒內各講長 → 搶話峰值。"""
    return [
        ("狗與露", "我覺得這個設計真的有問題啦", base + 0.0),
        ("showay", "不是啦你聽我說這個成本根本壓不下來", base + 1.0),
        ("陳進文", "對啊而且客戶那邊也不會買單的啦", base + 1.8),
        ("weakgogo", "嗯", base + 2.5),  # 附和、太短不算
    ]


def _calm_rows(base=1718000000.0):
    """講話間隔很開、輪流講 → 無搶話。"""
    return [
        ("狗與露", "我昨天去看了那個房子覺得還不錯", base + 0.0),
        ("showay", "喔是喔在哪裡啊多少錢", base + 8.0),
        ("陳進文", "那個地段我知道蠻方便的", base + 16.0),
    ]


def test_curate_crosstalk_hero_when_pileon():
    plan = curate(_crosstalk_rows(), _entries(6))
    assert isinstance(plan, CurationPlan)
    assert plan.source == "crosstalk"
    assert plan.hero.kind == "crosstalk"
    assert set(plan.hero.speakers) == {"狗與露", "showay", "陳進文"}  # weakgogo 附和被濾


def test_curate_topic_hero_when_calm():
    plan = curate(_calm_rows(), _entries(6))
    assert plan is not None
    assert plan.source == "topic"
    assert plan.hero.kind == "topic"


def test_curate_too_short_returns_none():
    assert curate(_crosstalk_rows(), _entries(3)) is None  # <6 段


def test_curate_cast_and_context_populated():
    plan = curate(_crosstalk_rows(), _entries(6), CuratorConditions(context_beats=2))
    assert "狗與露" in plan.cast and "showay" in plan.cast
    assert len(plan.context) == 2
    assert all(seg.summary for seg in plan.context)


def test_curate_relative_threshold_two_of_four_is_topic():
    # 4 人場，只有 2 人搶話 → 未過 0.6*4=3 門檻 → 退話題 hero
    rows = [
        ("狗與露", "我覺得這個設計真的有問題啦", 1718000000.0),
        ("showay", "不是啦你聽我說這個成本壓不下來", 1718000001.0),
    ]
    entries = _entries(6)  # cast 來自 entries 的 4 人
    plan = curate(rows, entries)
    assert plan.source == "topic"
