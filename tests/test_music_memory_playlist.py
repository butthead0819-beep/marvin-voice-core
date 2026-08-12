"""TDD: MusicMemory 歌單匯出與匯入功能測試。"""
from __future__ import annotations

import pytest
from music_memory import MusicMemory


def _sample_song(vid: str, title: str, uploader: str = "藝人A") -> dict:
    return {
        "title": title,
        "uploader": uploader,
        "webpage_url": f"https://www.youtube.com/watch?v={vid}",
        "url": f"https://www.youtube.com/watch?v={vid}",
    }


def test_export_user_playlist_empty(tmp_path):
    mm = MusicMemory(path=str(tmp_path / "mm.json"))
    assert mm.export_user_playlist("小明") == []


def test_export_user_playlist_returns_only_user_songs_sorted(tmp_path):
    mm = MusicMemory(path=str(tmp_path / "mm.json"))
    s1 = _sample_song("vid1", "小明的愛歌1")
    s2 = _sample_song("vid2", "小明的愛歌2")
    s3 = _sample_song("vid3", "小華的歌")

    mm.record_play(s1, "小明")
    mm.record_play(s1, "小明")  # 小明點 2 次
    mm.record_play(s2, "小明")  # 小明點 1 次
    mm.record_play(s3, "小華")  # 小華點 1 次
    mm.toggle_like(s1, "小明")  # 小明按讚 s1

    exported = mm.export_user_playlist("小明")
    assert len(exported) == 2
    assert exported[0]["title"] == "小明的愛歌1"
    assert exported[0]["user_plays"] == 2
    assert exported[0]["liked"] is True
    assert exported[0]["webpage_url"] == "https://www.youtube.com/watch?v=vid1"

    assert exported[1]["title"] == "小明的愛歌2"
    assert exported[1]["user_plays"] == 1
    assert exported[1]["liked"] is False


def test_import_user_playlist_new_and_existing_songs(tmp_path):
    mm = MusicMemory(path=str(tmp_path / "mm.json"))
    existing_song = _sample_song("vid_exist", "已存在的歌", "歌手X")
    mm.record_play(existing_song, "他人")

    to_import = [
        {"title": "新歌1", "uploader": "歌手1", "webpage_url": "https://www.youtube.com/watch?v=vid_new1"},
        {"title": "已存在的歌", "uploader": "歌手X", "webpage_url": "https://www.youtube.com/watch?v=vid_exist"},
        {"title": "", "uploader": "", "webpage_url": ""},  # 髒資料/無效資料
    ]

    imported_cnt, skipped_cnt = mm.import_user_playlist("小明", to_import)
    assert imported_cnt == 2
    assert skipped_cnt == 1

    # 檢查匯出確認小明擁有這兩首歌
    my_list = mm.export_user_playlist("小明")
    titles = [s["title"] for s in my_list]
    assert "新歌1" in titles
    assert "已存在的歌" in titles

    # 檢查已存在的歌 requesters 正確包含小明與他人
    key = mm._key(existing_song)
    assert mm._data["songs"][key]["requesters"]["小明"] >= 1
    assert mm._data["songs"][key]["requesters"]["他人"] >= 1
