"""日記縮減成「一天一格＝策展」：日記唯一內容 = 當夜 Marvin 策展的「今夜歌單」卡，
零 AI 生圖。無策展歌單 → 不出日記。純渲染閘測試（cover url 留空 → 不碰網路）。
"""
import datetime as dt
import json
from types import SimpleNamespace

import diary_comic_poster as poster


def _session(base):
    return [SimpleNamespace(ts_str=base.strftime("%Y-%m-%d %H:%M:%S")),
            SimpleNamespace(ts_str=(base + dt.timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S"))]


def _write_themed_log(tmp_path, monkeypatch, base, picks):
    rec = {"ts": (base + dt.timedelta(minutes=15)).timestamp(),
           "theme_title": "夜色抒情", "picks": picks}
    p = tmp_path / "themed.jsonl"
    p.write_text(json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")
    import themed_playlist
    monkeypatch.setattr(themed_playlist, "_THEMED_SET_LOG", str(p))


def test_render_themed_card_returns_image_when_set_exists(tmp_path, monkeypatch):
    base = dt.datetime(2026, 7, 8, 22, 0, 0)
    _write_themed_log(tmp_path, monkeypatch, base,
                      [{"title": "周杰倫 - 稻香", "reason": "扣回今晚的鄉愁", "url": ""}])
    card = poster._render_themed_card(_session(base))
    assert card is not None
    assert card.height > 0 and card.width > 0   # 真的渲染出一張卡


def test_render_themed_card_none_when_no_set(tmp_path, monkeypatch):
    base = dt.datetime(2026, 7, 8, 22, 0, 0)
    p = tmp_path / "empty.jsonl"
    p.write_text("", encoding="utf-8")
    import themed_playlist
    monkeypatch.setattr(themed_playlist, "_THEMED_SET_LOG", str(p))
    assert poster._render_themed_card(_session(base)) is None   # 無策展 → 不出日記


def test_render_themed_card_none_when_set_out_of_window(tmp_path, monkeypatch):
    base = dt.datetime(2026, 7, 8, 22, 0, 0)
    # 策展時戳落在場次窗外（前一天）→ 不算這場的日記
    stale = base - dt.timedelta(days=1)
    _write_themed_log(tmp_path, monkeypatch, stale,
                      [{"title": "舊歌", "reason": "昨天的", "url": ""}])
    assert poster._render_themed_card(_session(base)) is None
