"""Autopilot 最終安全網：三層(T1/T2/T3)全枯竭→從播放歷史回收重播，永不靜默停。

背景（2026-07-08 使用者報「自動隨機播放沒候選歌就結束」）：歌庫 1061 首在長時間連播下
24h 內被播光→T2/T3 找到的候選全被『已播過 video-id』濾掉→enqueued=0→串流靜默結束。
最終安全網從本場歷史挑舊歌重播（避開最近 min(5,歷史-1) 首防立即重複+skip 過的）。這裡測純選池邏輯。

2026-08-10 追加：舊門檻「歷史 <6 首才回空」在短場次(剛重啟/才聽幾首)一律打不開安全網，
且失敗時 `_last_resort_replay` 沒留下任何 log（真正的事故根因是「窮盡了但看不出為什麼」）。
門檻降到 <2 首，排除窗跟著縮小成 min(5, 歷史-1)，短場次也能回收。
"""
from cogs.music_cog import MusicCog

_pool = MusicCog._eligible_replay_pool


def _h(vid: str, title: str = "") -> dict:
    return {"webpage_url": f"https://www.youtube.com/watch?v={vid}", "title": title or vid}


def test_too_short_history_returns_empty():
    # 歷史 <2 首 → 沒得循環（真沒東西）
    hist = [_h("vid0000000")]
    assert _pool(hist, set()) == []


def test_short_history_relaxes_recent_window():
    # 歷史 3 首：排除窗縮成 min(5,2)=2 首，最舊那 1 首仍可回收
    hist = [_h(f"ddddddd{i:04d}"[:11]) for i in range(3)]
    out = _pool(hist, set())
    out_vids = {s["webpage_url"] for s in out}
    assert hist[0]["webpage_url"] in out_vids
    assert hist[1]["webpage_url"] not in out_vids
    assert hist[2]["webpage_url"] not in out_vids


def test_excludes_last_five_and_returns_older():
    hist = [_h(f"aaaaaaaa{i:03d}"[:11]) for i in range(10)]  # 10 首
    out = _pool(hist, set())
    out_vids = {s["webpage_url"] for s in out}
    # 最近 5 首(index 5-9)不該出現，較舊的(0-4)可回收
    for s in hist[-5:]:
        assert s["webpage_url"] not in out_vids
    assert len(out) >= 1


def test_excludes_skipped_vids():
    hist = [_h(f"bbbbbbbb{i:03d}"[:11]) for i in range(10)]
    from music_memory import extract_video_id
    skip = {extract_video_id(hist[0]["webpage_url"])}
    out = _pool(hist, skip)
    assert all(extract_video_id(s["webpage_url"]) not in skip for s in out)


def test_ignores_entries_without_webpage_url():
    hist = [_h(f"cccccccc{i:03d}"[:11]) for i in range(8)] + [{"title": "no url"}]
    out = _pool(hist, set())
    assert all(s.get("webpage_url") for s in out)
