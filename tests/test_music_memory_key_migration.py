"""TDD — MusicMemory.songs 用 webpage_url 當 canonical key + 遷移舊資料。

Bug 2026-05-25: yt-dlp 回的 `info["url"]` 是含 expire 的 stream URL，每次解析不同，
導致同一首歌在 songs 裡有多份 entry（張雨生「以為你都知道」7 份、蕭煌奇「慢冷」11 份）。
改用 `info["webpage_url"]`（穩定的 youtube.com/watch?v=... 形式）當 key，並在 _load
時把舊資料按 webpage_url 合併。
"""
from __future__ import annotations

import json

from music_memory import MusicMemory


def _info(title="晴天", uploader="JVR", *, webpage_url=None, url=None):
    out = {"title": title, "uploader": uploader}
    if webpage_url is not None:
        out["webpage_url"] = webpage_url
    if url is not None:
        out["url"] = url
    return out


# ── _key() 優先序 ──────────────────────────────────────────────────────────────

def test_key_prefers_webpage_url(tmp_path):
    mm = MusicMemory(path=str(tmp_path / "mm.json"))
    key = mm._key(_info(
        webpage_url="https://www.youtube.com/watch?v=abc123",
        url="https://rr3---sn-x.googlevideo.com/videoplayback?expire=999",
    ))
    assert key == "https://www.youtube.com/watch?v=abc123"


def test_key_falls_back_to_url_when_no_webpage_url(tmp_path):
    mm = MusicMemory(path=str(tmp_path / "mm.json"))
    key = mm._key(_info(url="https://stream.example/x.m4a"))
    assert key == "https://stream.example/x.m4a"


def test_key_falls_back_to_title_uploader_when_no_urls(tmp_path):
    mm = MusicMemory(path=str(tmp_path / "mm.json"))
    key = mm._key(_info(title="晴天", uploader="JVR"))
    assert key == "晴天|JVR"


# ── 遷移：載入時合併同 webpage_url 的舊 entry ──────────────────────────────────

def _write_dirty(path, songs):
    path.write_text(json.dumps({"songs": songs, "recommendations": {}}, ensure_ascii=False))


def test_migration_merges_entries_with_same_webpage_url(tmp_path):
    """兩份髒 entry（不同 stream URL key、同 webpage_url）→ 合併成一筆。"""
    wp = "https://www.youtube.com/watch?v=zzz"
    p = tmp_path / "mm.json"
    _write_dirty(p, {
        "https://stream.googlevideo.com/x?expire=111": {
            "title": "以為你都知道", "uploader": "滾石", "url": "https://stream/.../?expire=111",
            "webpage_url": wp, "total_plays": 3,
            "plays": [{"by": "大肚", "ts": 1.0}, {"by": "大肚", "ts": 2.0}, {"by": "露", "ts": 3.0}],
            "requesters": {"大肚": 2, "露": 1},
            "reactions": {"大肚": {"feelings": ["懷舊"], "quotes": ["這首神"]}},
            "connections": ["大肚", "露"],
        },
        "https://stream.googlevideo.com/x?expire=222": {
            "title": "以為你都知道", "uploader": "滾石", "url": "https://stream/.../?expire=222",
            "webpage_url": wp, "total_plays": 2,
            "plays": [{"by": "大肚", "ts": 4.0}, {"by": "Q", "ts": 5.0}],
            "requesters": {"大肚": 1, "Q": 1},
            "reactions": {"露": {"feelings": ["流淚"], "quotes": []}},
            "connections": ["Q"],
        },
    })
    mm = MusicMemory(path=str(p))
    songs = mm._data["songs"]
    assert list(songs.keys()) == [wp], f"應只剩一筆 webpage_url key，實際: {list(songs.keys())}"
    s = songs[wp]
    assert s["total_plays"] == 5
    assert len(s["plays"]) == 5
    assert s["requesters"] == {"大肚": 3, "露": 1, "Q": 1}
    assert set(s["connections"]) == {"大肚", "露", "Q"}
    # reactions 並集（不同 user 各自的 entry 都保留）
    assert "大肚" in s["reactions"] and "露" in s["reactions"]
    assert s["reactions"]["大肚"]["feelings"] == ["懷舊"]


def test_migration_preserves_legacy_entries_without_webpage_url(tmp_path):
    """webpage_url 缺值的舊 entry 不動（沿用原 key），避免 silent loss。"""
    p = tmp_path / "mm.json"
    _write_dirty(p, {
        "legacy://no-webpage": {
            "title": "古早歌", "uploader": "X", "url": "legacy://no-webpage",
            "total_plays": 1, "plays": [{"by": "A", "ts": 1.0}],
            "requesters": {"A": 1}, "reactions": {}, "connections": [],
        },
    })
    mm = MusicMemory(path=str(p))
    assert "legacy://no-webpage" in mm._data["songs"]


def test_migration_is_idempotent(tmp_path):
    """已經是 webpage_url key 的乾淨資料 → 重複載入不改變結構。"""
    wp = "https://www.youtube.com/watch?v=clean"
    p = tmp_path / "mm.json"
    _write_dirty(p, {
        wp: {
            "title": "乾淨歌", "uploader": "X", "url": "stream://x", "webpage_url": wp,
            "total_plays": 1, "plays": [{"by": "A", "ts": 1.0}],
            "requesters": {"A": 1}, "reactions": {}, "connections": [],
        },
    })
    mm1 = MusicMemory(path=str(p))
    snapshot = json.dumps(mm1._data, sort_keys=True)
    # 再開一次（強制 _load 重跑遷移）
    mm2 = MusicMemory(path=str(p))
    assert json.dumps(mm2._data, sort_keys=True) == snapshot


def test_migration_handles_empty_or_missing_songs(tmp_path):
    """空檔 / 沒有 songs 欄位 → 不炸。"""
    p = tmp_path / "mm.json"
    p.write_text(json.dumps({}))
    mm = MusicMemory(path=str(p))
    assert mm._data.get("songs", {}) == {}


def test_record_play_now_uses_webpage_url_as_key(tmp_path):
    """整合：record_play 後 songs key 是 webpage_url，不是 stream url。"""
    mm = MusicMemory(path=str(tmp_path / "mm.json"))
    wp = "https://www.youtube.com/watch?v=integ"
    info = _info(title="新歌", uploader="X", webpage_url=wp,
                 url="https://stream.googlevideo.com/?expire=999")
    mm.record_play(info, "TestUser")
    assert wp in mm._data["songs"]
    assert mm._data["songs"][wp]["total_plays"] == 1
