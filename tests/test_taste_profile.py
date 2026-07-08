"""taste_profile — LLM 從點播史生品味 profile + 鄰近歌手 seed（2026-06-04）。

離線 biased expert：LLM 讀 liked/played 歌 → profile 人話 + adjacent_artists（破回音室、
跳出史外鄰近歌手）→ ytmusic search 解析成真 videoId（resolve-then-trust 防幻覺）。
每日快取，T2 runtime 只讀快取 videoId（不在語音熱路徑打 LLM/search）。
純函式 + 可注入 IO，全可單測無網路。
"""
from __future__ import annotations

import json
import time

import pytest

import taste_profile as tp


# ── 1. parse：合法 JSON ──
def test_parse_valid():
    raw = json.dumps({"profile": "愛抒情", "adjacent_artists": ["伍佰", "Beyond"],
                      "suggested_songs": [{"artist": "伍佰", "title": "挪威的森林"}]})
    out = tp.parse_taste_response(raw)
    assert out["profile"] == "愛抒情"
    assert out["adjacent_artists"] == ["伍佰", "Beyond"]


# ── 2. parse：壞 JSON / 缺欄位 → graceful 預設 ──
def test_parse_bad_json_returns_empty_shape():
    out = tp.parse_taste_response("not json至少有結構")
    assert out == {"profile": "", "adjacent_artists": [], "suggested_songs": [], "avoid_artists": []}


def test_parse_missing_fields_defaults():
    out = tp.parse_taste_response(json.dumps({"profile": "x"}))
    assert out["profile"] == "x"
    assert out["adjacent_artists"] == []
    assert out["suggested_songs"] == []


# ── 3. build_taste_input：含歌與興趣 ──
def test_build_input_includes_songs_and_likes():
    s = tp.build_taste_input(["晴天", "山丘"], ["張國榮", "茄子蛋"])
    assert "晴天" in s and "山丘" in s and "張國榮" in s


# ── 4. generate：注入 call_fn ──
@pytest.mark.asyncio
async def test_generate_uses_call_fn():
    async def fake_call(content, system):
        return json.dumps({"profile": "p", "adjacent_artists": ["A"], "suggested_songs": []})
    out = await tp.generate_taste_profile(["s1"], ["l1"], call_fn=fake_call)
    assert out["adjacent_artists"] == ["A"]


@pytest.mark.asyncio
async def test_generate_call_fn_none_returns_none():
    async def fail_call(content, system):
        return None
    assert await tp.generate_taste_profile(["s1"], [], call_fn=fail_call) is None


@pytest.mark.asyncio
async def test_generate_empty_songs_skips_llm():
    called = False
    async def fake_call(content, system):
        nonlocal called; called = True
        return "{}"
    out = await tp.generate_taste_profile([], [], call_fn=fake_call)
    assert out is None and called is False     # 沒歌不打 LLM


# ── 5. resolve_artist_seeds：ytmusic search → videoId，去重、跳無果 ──
@pytest.mark.asyncio
async def test_resolve_artist_seeds():
    class FakeYT:
        def search(self, q, filter=None, limit=1):
            table = {"伍佰": [{"videoId": "v1"}], "Beyond": [{"videoId": "v2"}],
                     "幻覺歌手": []}
            return table.get(q, [])
    seeds = await tp.resolve_artist_seeds(["伍佰", "Beyond", "幻覺歌手", "伍佰"], client=FakeYT())
    assert seeds == ["v1", "v2"]               # 去重、幻覺(無果)跳過


# ── 6. cache：寫入 + 依在場成員 + 新鮮度讀回 seed ──
def test_cache_write_and_fresh_read(tmp_path):
    p = tmp_path / "taste.json"
    tp.write_profile(p, "weakgogo", {"profile": "x", "seed_video_ids": ["v1", "v2"]})
    tp.write_profile(p, "showay", {"profile": "y", "seed_video_ids": ["v3"]})
    seeds = tp.fresh_seed_ids(p, ["weakgogo", "showay"], max_age_s=99999)
    assert set(seeds) == {"v1", "v2", "v3"}


def test_cache_stale_excluded(tmp_path):
    p = tmp_path / "taste.json"
    tp.write_profile(p, "weakgogo", {"seed_video_ids": ["v1"]})
    # 手動把 ts 改老
    data = json.loads(p.read_text())
    data["weakgogo"]["ts"] = time.time() - 100000
    p.write_text(json.dumps(data))
    assert tp.fresh_seed_ids(p, ["weakgogo"], max_age_s=3600) == []


def test_cache_missing_user_empty(tmp_path):
    p = tmp_path / "taste.json"
    tp.write_profile(p, "weakgogo", {"seed_video_ids": ["v1"]})
    assert tp.fresh_seed_ids(p, ["不在的人"], max_age_s=99999) == []


def test_fresh_adjacent_artists_union_dedup(tmp_path):
    # T4 讀 LLM 相近歌手：在場成員聯集、去重保序
    p = tmp_path / "taste.json"
    tp.write_profile(p, "weakgogo", {"adjacent_artists": ["林憶蓮", "張學友"]})
    tp.write_profile(p, "大肚", {"adjacent_artists": ["張學友", "趙傳"]})   # 張學友 去重
    out = tp.fresh_adjacent_artists(p, ["weakgogo", "大肚"], max_age_s=99999)
    assert out == ["林憶蓮", "張學友", "趙傳"]
    assert tp.fresh_adjacent_artists(p, ["不在的人"], max_age_s=99999) == []


def test_fresh_read_missing_file(tmp_path):
    assert tp.fresh_seed_ids(tmp_path / "nope.json", ["x"], max_age_s=99999) == []


# ── 7. avoid 負空間：parse 帶 avoid_artists ──
def test_parse_includes_avoid_artists():
    raw = json.dumps({"profile": "x", "avoid_artists": ["重金屬團", "嘻哈X"]})
    out = tp.parse_taste_response(raw)
    assert out["avoid_artists"] == ["重金屬團", "嘻哈X"]


def test_parse_avoid_defaults_empty():
    out = tp.parse_taste_response(json.dumps({"profile": "x"}))
    assert out["avoid_artists"] == []


# ── 8. fresh_avoid_artists：在場成員 avoid 聯集（新鮮）──
def test_fresh_avoid_union(tmp_path):
    p = tmp_path / "taste.json"
    tp.write_profile(p, "weakgogo", {"avoid_artists": ["A", "B"]})
    tp.write_profile(p, "showay", {"avoid_artists": ["B", "C"]})
    avoid = tp.fresh_avoid_artists(p, ["weakgogo", "showay"], max_age_s=99999)
    assert set(avoid) == {"A", "B", "C"}


# ── 9. filter_avoided：剔除 artist 命中 avoid 的候選（純函式）──
def test_filter_avoided_drops_matching_artist():
    cands = [{"title": "歌1", "artist": "伍佰"}, {"title": "歌2", "artist": "重金屬團"}]
    out = tp.filter_avoided(cands, ["重金屬團"])
    assert [c["title"] for c in out] == ["歌1"]


def test_filter_avoided_empty_avoid_keeps_all():
    cands = [{"title": "歌1", "artist": "伍佰"}]
    assert tp.filter_avoided(cands, []) == cands
