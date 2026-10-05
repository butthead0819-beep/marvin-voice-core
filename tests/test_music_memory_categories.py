"""record_play 存 YouTube 分類（懷舊位非音樂閘用）。"""
from __future__ import annotations

from music_memory import MusicMemory


def _info(vid="dQw4w9WgXcQ", title="老歌"):
    return {
        "title": title,
        "uploader": "歌手",
        "webpage_url": f"https://www.youtube.com/watch?v={vid}",
    }


def test_record_play_stores_categories(tmp_path):
    mm = MusicMemory(path=str(tmp_path / "mm.json"))
    info = _info()
    info["categories"] = ["Music"]
    mm.record_play(info, "阿明")
    s = mm._data["songs"][mm._key(info)]
    assert s["categories"] == ["Music"]


def test_record_play_without_categories_keeps_existing_value(tmp_path):
    mm = MusicMemory(path=str(tmp_path / "mm.json"))
    info = _info()
    info["categories"] = ["Music"]
    mm.record_play(info, "阿明")
    mm.record_play(_info(), "阿明")   # 第二次不帶 categories
    s = mm._data["songs"][mm._key(_info())]
    assert s["categories"] == ["Music"]
