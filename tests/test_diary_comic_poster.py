"""diary_comic_poster：render_story 接 production 流程的規劃閘 + marvin.db 取精華。

只測純規劃與資料閘（不碰網路/出圖）。
"""
import sqlite3

import diary_comic_poster as poster


_TOPICS = ["台中包棟民宿烤肉", "GTA 系列遊戲回憶", "鋼琴與二胡的保養",
           "AI 創業營運構想", "弔唁送別行程安排", "國中重考的惡夢"]


def _log(n):
    """造 n 筆 6 月格式日誌（3 分鐘一筆、話題各異 → 同場次不被跳針合併）。"""
    blocks = []
    for i in range(n):
        topic = _TOPICS[i % len(_TOPICS)]
        blocks.append(
            f"[2026-06-20 22:{i*3:02d}:00] --- 10分鐘對話總結 ---\n"
            f"核心：{topic}\n"
            f"摘要：狗與露與 showay 討論{topic}。\n")
    return "\n".join(blocks)


def _rows_with_laugh(start_ts_str, end_ts_str):
    return [("showay", "我國中還在重考找不到制服", 1718000000.0),
            ("showay", "哈哈哈哈哈哈哈", 1718000010.0)]


def _rows_no_laugh(start_ts_str, end_ts_str):
    return [("showay", "今天天氣不錯", 1718000000.0),
            ("狗與露", "對啊蠻舒服的", 1718000010.0)]


def test_plan_latest_session_with_highlight_returns_plan():
    out = poster.plan_latest_session(_log(6), _rows_with_laugh)
    assert out is not None
    session, plan, end = out
    assert plan is not None
    assert plan.format in ("meme", "slant")
    assert end == "2026-06-20 22:15:00"  # 第 6 筆（index 5 → 15 分）


def test_plan_latest_session_no_laugh_returns_none():
    # 內容夠多但沒爆笑 → fuse 回 None → 不出漫畫
    assert poster.plan_latest_session(_log(6), _rows_no_laugh) is None


def test_plan_latest_session_too_short_returns_none():
    # <6 筆 → should_generate False → 不出
    assert poster.plan_latest_session(_log(3), _rows_with_laugh) is None


def test_plan_latest_session_empty_log_returns_none():
    assert poster.plan_latest_session("", _rows_with_laugh) is None


def test_db_rows_filters_to_session_window(tmp_path):
    db = tmp_path / "t.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE transcripts (speaker TEXT, text TEXT, timestamp REAL)")
    base = 1718000000.0
    con.executemany("INSERT INTO transcripts VALUES (?,?,?)", [
        ("a", "太早不算", base - 99999),
        ("b", "場次內1", base + 60),
        ("c", "場次內2", base + 120),
        ("d", "太晚不算", base + 99999),
    ])
    con.commit()
    con.close()
    import datetime
    start = datetime.datetime.fromtimestamp(base).isoformat(sep=" ")
    end = datetime.datetime.fromtimestamp(base + 300).isoformat(sep=" ")
    rows = poster._db_rows(start, end, db_path=str(db))
    texts = [t for _s, t, _ts in rows]
    assert texts == ["場次內1", "場次內2"]


def test_db_rows_bad_timestamp_returns_empty():
    assert poster._db_rows("not-a-date", "also-bad", db_path=":memory:") == []
