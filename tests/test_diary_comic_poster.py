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


def _rows_crosstalk(start_ts_str, end_ts_str):
    # 3 人在 2 秒內各講長 → 搶話峰值
    return [("狗與露", "我覺得這個設計真的有問題啦", 1718000000.0),
            ("showay", "不是啦你聽我說成本根本壓不下來", 1718000001.0),
            ("陳進文", "對啊而且客戶那邊也不會買單啦", 1718000001.8)]


def _rows_calm(start_ts_str, end_ts_str):
    # 單人、無重疊 → 無搶話
    return [("showay", "今天天氣真的蠻舒服的啊", 1718000000.0)]


def test_plan_latest_session_crosstalk_returns_slant():
    out = poster.plan_latest_session(_log(6), _rows_crosstalk)
    assert out is not None
    session, plan, end = out
    assert plan.format == "slant"  # 有搶話高潮 → 整頁
    assert end == "2026-06-20 22:15:00"  # 第 6 段（index 5 → 15 分）


def test_plan_latest_session_calm_returns_topic_meme():
    # 沒搶話但有料 → 退最強話題、單格 meme（不再是 None）
    out = poster.plan_latest_session(_log(6), _rows_calm)
    assert out is not None
    _session, plan, _end = out
    assert plan.format == "meme"


def test_plan_latest_session_too_short_returns_none():
    # <6 段 → should_generate False → 不出
    assert poster.plan_latest_session(_log(3), _rows_crosstalk) is None


def test_plan_latest_session_empty_log_returns_none():
    assert poster.plan_latest_session("", _rows_crosstalk) is None


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


# ---- 延後發布：pending 狀態 + 貼+置頂 ----
import pytest
from unittest.mock import AsyncMock, MagicMock


def test_pending_state_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(poster, "PENDING_PATH", str(tmp_path / "p.json"))
    assert poster._pending() == {}
    poster._set_pending("2026-06-22 23:00:00", "records/x.png", "slant")
    p = poster._pending()
    assert p["end"] == "2026-06-22 23:00:00" and p["format"] == "slant"
    poster._clear_pending()
    assert poster._pending() == {}


@pytest.mark.asyncio
async def test_maybe_post_diary_posts_pins_clears(tmp_path, monkeypatch):
    img = tmp_path / "page.png"
    img.write_bytes(b"x")
    monkeypatch.setattr(poster, "PENDING_PATH", str(tmp_path / "p.json"))
    monkeypatch.setattr(poster, "STATE_PATH", str(tmp_path / "s.json"))
    poster._set_pending("2026-06-22 23:00:00", str(img), "slant")
    msg = MagicMock()
    msg.pin = AsyncMock()
    channel = MagicMock()
    channel.send = AsyncMock(return_value=msg)
    monkeypatch.setattr(poster, "_find_diary_channel", lambda bot: channel)
    res = await poster.maybe_post_diary(MagicMock())
    assert res is not None and res[0] is channel
    channel.send.assert_awaited()
    msg.pin.assert_awaited()                       # 置頂
    assert poster._pending() == {}                 # 清掉 pending
    assert poster._last_posted() == "2026-06-22 23:00:00"  # 標已貼


@pytest.mark.asyncio
async def test_maybe_post_diary_no_pending_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(poster, "PENDING_PATH", str(tmp_path / "p.json"))
    assert await poster.maybe_post_diary(MagicMock()) is None
