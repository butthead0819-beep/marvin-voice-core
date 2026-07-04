"""TDD: 使用者點歌兩道防護（2026-07-04 使用者拍板「都做」）。

① RecentRequestLedger — 同 speaker + 同 videoId + 30s 窗去重：
   佇列去重（check_history=False）只看佇列，第一發已 pop 去播時佇列是空的
   → 第二發漏過（7/3-4 實錘）。ledger 不看佇列狀態，治所有重複形式
   （debounce 殘餘/語音+手動混點/誤觸）。
② ResolveCache — videoId→info TTL 快取：yt-dlp 抽流 ~2s 是 5 秒延遲的
   最大可壓縮塊；他們的使用模式重複點播極多（愛很簡單×5、左邊的人×5），
   重複點播免重抽。TTL 60min（yt-dlp 串流 URL 壽命 ~6h，保守取 1h）。
"""
from __future__ import annotations

from music_request_guard import RecentRequestLedger, ResolveCache


# ── ① RecentRequestLedger ────────────────────────────────────────────────────

def test_ledger_dup_within_window():
    lg = RecentRequestLedger(window_s=30.0)
    lg.mark("狗與露", "vidA", now=1000.0)
    assert lg.is_dup("狗與露", "vidA", now=1013.0) is True   # 13s 後（實案場景）


def test_ledger_expires_after_window():
    lg = RecentRequestLedger(window_s=30.0)
    lg.mark("狗與露", "vidA", now=1000.0)
    assert lg.is_dup("狗與露", "vidA", now=1031.0) is False  # 真心想再聽一次


def test_ledger_different_speaker_not_dup():
    lg = RecentRequestLedger(window_s=30.0)
    lg.mark("狗與露", "vidA", now=1000.0)
    assert lg.is_dup("showay", "vidA", now=1005.0) is False


def test_ledger_different_video_not_dup():
    lg = RecentRequestLedger(window_s=30.0)
    lg.mark("狗與露", "vidA", now=1000.0)
    assert lg.is_dup("狗與露", "vidB", now=1005.0) is False


def test_ledger_prunes_old_entries():
    lg = RecentRequestLedger(window_s=30.0)
    for i in range(100):
        lg.mark(f"u{i}", "vid", now=1000.0)
    lg.mark("new", "vid", now=2000.0)   # mark 觸發 prune
    assert lg.size() <= 2


# ── ② ResolveCache ───────────────────────────────────────────────────────────

def _info(vid="vidA"):
    return {"title": "晴天", "url": f"https://stream/{vid}",
            "webpage_url": f"https://www.youtube.com/watch?v={vid}", "duration": 269}


def test_cache_hit_within_ttl_returns_copy():
    c = ResolveCache(ttl_s=3600.0)
    c.put("vidA", _info(), now=1000.0)
    hit = c.get("vidA", now=2000.0)
    assert hit is not None and hit["title"] == "晴天"
    # 必須回 copy：caller 會就地改 requested_by/_lane，不得污染快取
    hit["requested_by"] = "阿明"
    hit2 = c.get("vidA", now=2001.0)
    assert "requested_by" not in hit2


def test_cache_expires_after_ttl():
    c = ResolveCache(ttl_s=3600.0)
    c.put("vidA", _info(), now=1000.0)
    assert c.get("vidA", now=1000.0 + 3601) is None


def test_cache_miss_unknown_video():
    c = ResolveCache(ttl_s=3600.0)
    assert c.get("nope", now=1000.0) is None


def test_cache_put_none_ignored():
    c = ResolveCache(ttl_s=3600.0)
    c.put("vidA", None, now=1000.0)
    assert c.get("vidA", now=1001.0) is None
