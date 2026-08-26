"""TDD: DJ Interjection 串場拓展功能端到端與品管兜底測試。

測試涵蓋：
1. 社交喜好自然表述（無「第 N 次」生硬計數，改為「常聽/多人常聽」）。
2. 新聞模式整合與負面新聞（社會/死亡/政治）安全過濾。
3. 新聞 2 小時冷卻機制。
4. LLM 產出品管檢查（字數超長/過短/假文青禁用詞）與 Threads 生活小幽默兜底。
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from dj_comedy_fallback import COMEDY_FALLBACK_SCRIPTS, get_comedy_fallback, build_news_interjection_template
from dj_topic_selector import TopicCooldownStore, NEWS_COOLDOWN_S, select_mode
from news_fetch import is_safe_news_title


# ── 1. 社交喜好無生硬計數 ──────────────────────────────────────────

def test_social_affinity_uses_natural_habit_phrasing():
    """驗證社交偏好採用自然常聽/喜好表述，無 '第 X 次'。"""
    from dj_social_affinity import find_song_social_affinity
    from music_memory import MusicMemory
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        mm = MusicMemory(tmp.name)
        mm._data = {
            "songs": {
                "key1": {
                    "title": "晴天",
                    "requesters": {"Alice": 5, "Bob": 3},
                    "likes": {},
                    "reactions": {},
                }
            }
        }
        info = {"title": "晴天", "webpage_url": "key1"}
        # Alice 點歌，Bob 在場
        affinity = find_song_social_affinity(mm, info, requester="Alice", present_members={"Alice", "Bob"})
        assert affinity is not None
        assert "Bob" in affinity
        assert "常聽" in affinity or "點過" in affinity
        assert "第" not in affinity
        assert "次" not in affinity


# ── 2. 新聞安全過濾 ────────────────────────────────────────────────

def test_news_safety_filtering():
    """驗證新聞標題安全過濾器有效過濾負面、受傷、死亡、政治與八卦。"""
    unsafe_titles = [
        "國道重大車禍造成 2 死 3 傷",
        "某立委捲入貪污收賄案件開庭",
        "知名藝人遭爆出軌婚變醜聞",
        "男子酒後墜樓命危搶救中",
        "政黨立院表決引發激烈肢體衝突",
    ]
    for title in unsafe_titles:
        assert not is_safe_news_title(title), f"不應通過安全檢查: {title}"

    safe_titles = [
        "氣象署：週末全台晴朗高溫上看32度",
        "台積電宣布最新先進製程技術突破",
        "台北動物園水豚寶寶滿月萌翻遊客",
        "天文奇景超級月亮將於今晚登場",
    ]
    for title in safe_titles:
        assert is_safe_news_title(title), f"應該通過安全檢查: {title}"


# ── 3. 新聞 2 小時冷卻與選題 ──────────────────────────────────────

def test_news_mode_selection_and_cooldown(tmp_path):
    """驗證新聞在有可用素材時會被選中，且受 2 小時冷卻保護。"""
    t = [10000.0]
    now = lambda: t[0]
    store = TopicCooldownStore(str(tmp_path / "cd.json"), now=now)

    topic, mode = select_mode(
        life_cores=[],
        interests=[],
        store=store,
        news_items=["台北週末好天氣"],
    )
    assert mode == "news"
    assert topic == "台北週末好天氣"

    # 立即再次請求 → 進入 2 小時冷卻，不再選取該新聞
    topic2, mode2 = select_mode(
        life_cores=[],
        interests=[],
        store=store,
        news_items=["台北週末好天氣"],
    )
    assert mode2 != "news"

    # 經過 2 小時 + 1 秒後 → 冷卻結束
    t[0] += NEWS_COOLDOWN_S + 1
    topic3, mode3 = select_mode(
        life_cores=[],
        interests=[],
        store=store,
        news_items=["台北週末好天氣"],
    )
    assert mode3 == "news"
    assert topic3 == "台北週末好天氣"


# ── 4. LLM 品質守門與兜底 ──────────────────────────────────────────

def test_comedy_fallback_random_pool():
    """驗證 Threads 生活小幽默段子庫品質與隨機性。"""
    scripts = set()
    for _ in range(20):
        scripts.add(get_comedy_fallback())
    assert len(scripts) > 1  # 具有隨機抽取性
    for s in COMEDY_FALLBACK_SCRIPTS:
        assert len(s) >= 30
        assert len(s) <= 55


def test_build_news_interjection_template():
    """驗證新聞快報接歌模板。"""
    text = build_news_interjection_template("台北天文館舉辦星空特展", "夜曲")
    assert "台北天文館舉辦星空特展" in text
    assert len(text) <= 55
