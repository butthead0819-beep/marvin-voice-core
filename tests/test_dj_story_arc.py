"""DJ 故事弧線（Story Arc）5步驟管線：找敘事流 → 共同/個人回憶(純) → 大綱+選歌(Call1,
真候選池) → 口白(Call2) → resolve + 記錄。"""
from __future__ import annotations

import datetime as _dt

import pytest

from dj_story_arc import (
    BgmCursor,
    ShowIntro,
    StoryArc,
    StoryBrief,
    StoryNode,
    build_interjection_prompt,
    build_outline_prompt,
    build_show_intro,
    build_staged_show,
    build_story_arc_record,
    build_story_candidate_pools,
    clear_staged_show,
    curate_story_interjections,
    curate_story_outline,
    estimate_interjection_duration_s,
    gather_story_brief,
    load_staged_show,
    parse_story_interjections,
    parse_story_outline,
    record_story_arc,
    resolve_story_arc,
    save_staged_show,
    tag_taste_match,
)
from music_recommender import LONG_TAIL_DAYS, Candidate


def _ts(dt_obj):
    return dt_obj.strftime("%Y-%m-%d %H:%M:%S")


class _Entry:
    """DiaryEntry-like 物件，帶 speakers/meme_id（給敘事流偵測+共同/個人分桶用）。"""
    def __init__(self, ts_str, core, speakers, meme_id="", salience="中"):
        self.ts_str = ts_str
        self.core = core
        self.speakers = speakers
        self.meme_id = meme_id
        self.salience = salience
        self.is_sensitive = False
        self.participants = None


def _cand(artist, title, lane, score=50.0, target_member=None):
    return Candidate(anchor_title=title, anchor_artist=artist, lane=lane,
                     mode="direct", target_member=target_member, score=score)


# ── Step 1+2: gather_story_brief（純函式，narrative_day 偵測 + 共同/個人分桶）───

def test_gather_story_brief_picks_day_with_meme_repeat_over_scattered_days():
    """同日 meme_id 重複的那天優先於材料更多但分散在別天的核心句。"""
    now = _dt.datetime(2026, 8, 17, 22, 0).timestamp()
    day1 = _dt.datetime(2026, 8, 15, 21, 0)   # 同日 meme_id 重複兩次
    day2 = _dt.datetime(2026, 8, 16, 21, 0)   # 共同回憶則數較多但無 meme 重複
    entries = [
        _Entry(_ts(day1), "去海邊烤肉", ["狗與露", "showay"], meme_id="海邊烤肉"),
        _Entry(_ts(day1 + _dt.timedelta(minutes=10)), "烤肉烤到一半下雨",
              ["狗與露", "showay"], meme_id="海邊烤肉"),
        _Entry(_ts(day2), "聊到天氣", ["狗與露", "showay"]),
        _Entry(_ts(day2 + _dt.timedelta(minutes=10)), "聊到工作", ["狗與露", "showay"]),
        _Entry(_ts(day2 + _dt.timedelta(minutes=20)), "聊到電影", ["狗與露", "showay"]),
    ]
    brief = gather_story_brief(
        entries, ["狗與露", "showay"], [], [], now=now, target_duration_s=1200.0)
    assert brief.narrative_day == "2026-08-15"
    assert any("烤肉" in c for c in brief.shared_cores)


def test_gather_story_brief_falls_back_to_richest_shared_day_without_meme_signal():
    """完全沒有 meme_id 訊號時，fallback 選共同回憶則數最多的一天。"""
    now = _dt.datetime(2026, 8, 17, 22, 0).timestamp()
    day1 = _dt.datetime(2026, 8, 15, 21, 0)
    day2 = _dt.datetime(2026, 8, 16, 21, 0)
    entries = [
        _Entry(_ts(day1), "共同事一", ["狗與露", "showay"]),
        _Entry(_ts(day2), "共同事二", ["狗與露", "showay"]),
        _Entry(_ts(day2 + _dt.timedelta(minutes=10)), "共同事三", ["狗與露", "showay"]),
    ]
    brief = gather_story_brief(
        entries, ["狗與露", "showay"], [], [], now=now, target_duration_s=1200.0)
    assert brief.narrative_day == "2026-08-16"


def test_gather_story_brief_none_when_no_day_has_enough_shared_material():
    now = _dt.datetime(2026, 8, 17, 22, 0).timestamp()
    base = _dt.datetime(2026, 8, 17, 21, 0)
    entries = [_Entry(_ts(base), "唯一一則", ["狗與露", "showay"])]
    assert gather_story_brief(
        entries, ["狗與露", "showay"], [], [], now=now,
        target_duration_s=1200.0, min_cores=2) is None


def test_gather_story_brief_splits_shared_and_member_cores_within_chosen_day():
    now = _dt.datetime(2026, 8, 17, 22, 0).timestamp()
    base = _dt.datetime(2026, 8, 17, 21, 0)
    entries = [
        _Entry(_ts(base), "大家一起去海邊烤肉", ["狗與露", "showay"]),
        _Entry(_ts(base + _dt.timedelta(minutes=10)), "又聊到海邊烤肉的糗事", ["狗與露", "showay"]),
        _Entry(_ts(base + _dt.timedelta(minutes=20)), "狗與露小時候外婆買糖果", ["狗與露"]),
        _Entry(_ts(base + _dt.timedelta(minutes=30)), "showay最近在追一部劇", ["showay"]),
    ]
    brief = gather_story_brief(
        entries, ["狗與露", "showay"], ["狗與露喜歡周杰倫"], ["最近聊天氣"],
        now=now, target_duration_s=1200.0)
    assert isinstance(brief, StoryBrief)
    assert brief.narrative_day == "2026-08-17"
    assert any("烤肉" in c for c in brief.shared_cores)
    assert any("外婆" in c for c in brief.member_cores["狗與露"])
    assert not any("外婆" in c for c in brief.member_cores["showay"])
    assert any("追一部劇" in c for c in brief.member_cores["showay"])


def test_gather_story_brief_window_is_seven_days():
    now = _dt.datetime(2026, 8, 17, 12, 0).timestamp()
    entries = [
        _Entry(_ts(_dt.datetime(2026, 8, 9, 12, 0)), "八天前的共同舊事", ["狗與露", "showay"]),
        _Entry(_ts(_dt.datetime(2026, 8, 11, 12, 0)), "六天前的共同事一", ["狗與露", "showay"]),
        _Entry(_ts(_dt.datetime(2026, 8, 11, 12, 10)), "六天前的共同事二", ["狗與露", "showay"]),
    ]
    brief = gather_story_brief(
        entries, ["狗與露", "showay"], [], [], now=now, target_duration_s=1200.0)
    assert brief is not None
    assert brief.narrative_day == "2026-08-11"
    assert not any("八天前" in c for c in brief.shared_cores)


@pytest.mark.parametrize("target_s,expected", [(100.0, 3), (1200.0, 5), (5000.0, 8)])
def test_gather_story_brief_node_count_clamped(target_s, expected):
    now = _dt.datetime(2026, 8, 17, 22, 0).timestamp()
    base = _dt.datetime(2026, 8, 17, 21, 0)
    entries = [
        _Entry(_ts(base), "共同核心句一", ["狗與露", "showay"]),
        _Entry(_ts(base + _dt.timedelta(minutes=10)), "共同核心句二", ["狗與露", "showay"]),
    ]
    brief = gather_story_brief(
        entries, ["狗與露", "showay"], [], [], now=now, target_duration_s=target_s)
    assert brief.node_count == expected


def test_gather_story_brief_node_count_at_least_member_count():
    now = _dt.datetime(2026, 8, 17, 22, 0).timestamp()
    base = _dt.datetime(2026, 8, 17, 21, 0)
    members = ["A", "B", "C", "D"]
    entries = [
        _Entry(_ts(base), "共同核心句一", members),
        _Entry(_ts(base + _dt.timedelta(minutes=10)), "共同核心句二", members),
    ]
    brief = gather_story_brief(entries, members, [], [], now=now, target_duration_s=100.0)
    assert brief.node_count >= 4


# ── Step 3a: build_story_candidate_pools（純函式，薄封裝 music_recommender）───

def test_build_story_candidate_pools_shared_from_group_resonance():
    songs = {
        "v1": {"title": "晴天", "uploader": "周杰倫",
              "requesters": {"狗與露": 3, "showay": 2}, "likes": {},
              "connections": ["狗與露", "showay"]},
        "v2": {"title": "流沙", "uploader": "陶喆",
              "requesters": {"狗與露": 5}, "likes": {}, "connections": []},
    }
    pools = build_story_candidate_pools(["狗與露", "showay"], songs, [], now=1.0)
    shared_titles = {c.anchor_title for c in pools["shared"]}
    assert "晴天" in shared_titles          # 兩人共同點過 → group_resonance
    assert "流沙" not in shared_titles       # 只有狗與露點過 → 不會進 group_resonance


def test_build_story_candidate_pools_member_from_liked_and_spotlight():
    songs = {
        "v1": {"title": "晴天", "uploader": "周杰倫",
              "requesters": {}, "likes": {"狗與露": True}, "connections": []},
    }
    pools = build_story_candidate_pools(["狗與露"], songs, [], now=1.0)
    assert any(c.anchor_title == "晴天" for c in pools["狗與露"])


def test_build_story_candidate_pools_excludes_titles():
    songs = {
        "v1": {"title": "晴天", "uploader": "周杰倫",
              "requesters": {"狗與露": 3, "showay": 2}, "likes": {}, "connections": []},
    }
    pools = build_story_candidate_pools(["狗與露", "showay"], songs, ["晴天"], now=1.0)
    assert pools["shared"] == []


def test_build_story_candidate_pools_shared_fallback_when_no_group_resonance():
    """放寬：group_resonance 空（connections 沒標）時，同一首歌出現在≥2人候選池 → 照樣算共同候選。"""
    songs = {
        "v1": {"title": "晴天", "uploader": "周杰倫",
              "requesters": {"狗與露": 5, "showay": 3}, "likes": {}, "connections": []},
    }
    pools = build_story_candidate_pools(["狗與露", "showay"], songs, [], now=1.0)
    assert any(c.anchor_title == "晴天" for c in pools["shared"])


def _long_tail_fixture():
    """3首高點播數佔滿 top3（→不會被判成 spotlight），第4首「老歌」點播數低+
    很久沒播 → 只夠格 long_tail，不會被 spotlight 蓋過（_offer 每首歌只留最高分那個 lane）。"""
    old_ts = 1_000_000.0
    now = old_ts + (LONG_TAIL_DAYS + 1.0) * 86400.0
    songs = {
        "v1": {"title": "熱門一", "uploader": "五月天", "requesters": {"狗與露": 10},
              "likes": {}, "connections": []},
        "v2": {"title": "熱門二", "uploader": "五月天", "requesters": {"狗與露": 9},
              "likes": {}, "connections": []},
        "v3": {"title": "熱門三", "uploader": "五月天", "requesters": {"狗與露": 8},
              "likes": {}, "connections": []},
        "v4": {"title": "老歌", "uploader": "五月天", "requesters": {"狗與露": 1},
              "likes": {}, "connections": [], "plays": [{"ts": old_ts, "by": "狗與露"}]},
    }
    return songs, now


def test_build_story_candidate_pools_includes_long_tail_by_default():
    """放寬：個人候選池預設也吃 long_tail lane（點過但久沒播），擴大候選數量。"""
    songs, now = _long_tail_fixture()
    pools = build_story_candidate_pools(["狗與露"], songs, [], now=now)
    assert any(c.lane == "long_tail" and c.anchor_title == "老歌" for c in pools["狗與露"])


def test_build_story_candidate_pools_long_tail_can_be_disabled():
    songs, now = _long_tail_fixture()
    pools = build_story_candidate_pools(["狗與露"], songs, [], now=now, include_long_tail=False)
    assert not any(c.lane == "long_tail" for c in pools["狗與露"])


# ── build_outline_prompt / parse_story_outline（純函式）─────────────────────

def test_build_outline_prompt_includes_pools_cores_and_exclusions():
    brief = StoryBrief(narrative_day="2026-08-17", shared_cores=["大家一起去海邊烤肉"],
                       member_cores={"狗與露": ["小時候外婆買糖果"], "showay": ["最近在追一部劇"]},
                       liked_items=["狗與露喜歡周杰倫"], conv_snippets=["最近聊天氣"],
                       members=["狗與露", "showay"], target_duration_s=1200.0, node_count=5)
    pools = {"shared": [_cand("周杰倫", "晴天", "group_resonance")],
            "狗與露": [_cand("陶喆", "流沙", "liked")], "showay": []}
    system, user = build_outline_prompt(brief, pools, ["稻香"])
    assert "5" in system
    assert "狗與露" in system and "showay" in system
    assert "烤肉" in user and "外婆" in user and "追一部劇" in user
    assert "晴天" in user and "流沙" in user   # 候選歌進 prompt
    assert "周杰倫" in user
    assert "稻香" in user
    assert "口味參考" in system and "不是唯一選項" in system   # 候選池是引導不是硬限制
    assert "驚喜" in system                                      # 鼓勵推薦候選池外的新發現
    assert "song_query 也適用不捏造規則" in system               # song_query 欄位也受反捏造規則約束


def test_parse_story_outline_valid_json_without_interjection():
    resp = ('{"arc_title":"糖果與外婆","nodes":['
            '{"position":1,"emotion_tag":"懷舊","spotlight_member":"狗與露",'
            '"resonance_link":null,'
            '"song_query":{"period":"小時候","people":["外婆"],"motif":"糖果","free_text":""},'
            '"artist":"周杰倫","song":"晴天","bpm_target":80,"volume_delta_db":-2}]}')
    arc = parse_story_outline(resp)
    assert isinstance(arc, StoryArc)
    assert arc.nodes[0].artist == "周杰倫" and arc.nodes[0].song == "晴天"
    assert arc.nodes[0].interjection_script == ""
    assert arc.nodes[0].spotlight_member == "狗與露"


def test_parse_story_outline_drops_nodes_missing_artist_or_song():
    resp = ('{"arc_title":"x","nodes":['
            '{"position":1,"artist":"a","song":"b"},'
            '{"position":2,"artist":"","song":"沒歌手"},'
            '{"position":3,"song":"沒藝人欄"}]}')
    arc = parse_story_outline(resp)
    assert len(arc.nodes) == 1


@pytest.mark.parametrize("resp", ["", "不是 JSON", '{"arc_title":"x","nodes":[]}',
                                  '{"nodes":[{"artist":"a","song":"b"}]}'])
def test_parse_story_outline_invalid_returns_none(resp):
    assert parse_story_outline(resp) is None


# ── tag_taste_match（純函式，選歌是否命中候選池的資訊性標記，不砍節點）───────

def test_tag_taste_match_flags_in_pool_and_out_of_pool_without_dropping():
    arc = StoryArc(arc_title="x", nodes=[
        StoryNode(1, "", {}, "周杰倫", "晴天", "", None, 0.0, spotlight_member=None),
        StoryNode(2, "", {}, "驚喜歌手", "驚喜歌名", "", None, 0.0, spotlight_member=None),
        StoryNode(3, "", {}, "陶喆", "流沙", "", None, 0.0, spotlight_member="狗與露"),
    ])
    pools = {"shared": [_cand("周杰倫", "晴天", "group_resonance")],
            "狗與露": [_cand("陶喆", "流沙", "liked")]}
    tagged = tag_taste_match(arc, pools)
    assert len(tagged.nodes) == 3   # 沒有節點被砍——候選池外的「驚喜」歌照樣保留
    by_song = {n.song: n.taste_match for n in tagged.nodes}
    assert by_song["晴天"] is True
    assert by_song["流沙"] is True
    assert by_song["驚喜歌名"] is False   # 池外的歌標記 taste_match=False，不是丟掉


def test_tag_taste_match_renumbers_positions_contiguously():
    """節點1、3、5(parse階段丟過缺欄位節點留下缺口)重編成連續1、2、3——避免Call2 position對錯。"""
    arc = StoryArc(arc_title="x", nodes=[
        StoryNode(1, "", {}, "周杰倫", "晴天", "", None, 0.0),
        StoryNode(3, "", {}, "陶喆", "流沙", "", None, 0.0),
        StoryNode(5, "", {}, "五月天", "倔強", "", None, 0.0),
    ])
    pools = {"shared": [_cand("周杰倫", "晴天", "group_resonance"),
                        _cand("陶喆", "流沙", "group_resonance"),
                        _cand("五月天", "倔強", "group_resonance")]}
    tagged = tag_taste_match(arc, pools)
    assert [n.position for n in tagged.nodes] == [1, 2, 3]
    assert [n.song for n in tagged.nodes] == ["晴天", "流沙", "倔強"]   # 順序不變，只重編號


# ── curate_story_outline（協調，注入 call_fn）────────────────────────────────

@pytest.mark.asyncio
async def test_curate_story_outline_calls_llm_parses_and_tags_taste_match():
    brief = StoryBrief(narrative_day="2026-08-17", shared_cores=["外婆買糖果"], member_cores={},
                       liked_items=[], conv_snippets=[], members=[],
                       target_duration_s=1200.0, node_count=1)
    pools = {"shared": [_cand("周杰倫", "晴天", "group_resonance")]}
    captured = {}

    async def fake_call(content, *, system, **kw):
        captured["content"] = content
        return ('{"arc_title":"糖果","nodes":[{"position":1,"artist":"周杰倫","song":"晴天",'
                '"bpm_target":80,"volume_delta_db":0}]}')

    arc = await curate_story_outline(brief, pools, ["稻香"], call_fn=fake_call)
    assert arc.arc_title == "糖果" and arc.nodes[0].song == "晴天"
    assert arc.nodes[0].taste_match is True
    assert "稻香" in captured["content"]


@pytest.mark.asyncio
async def test_curate_story_outline_keeps_surprise_selection_outside_pool():
    """LLM 推薦候選池外的「驚喜」歌 → 節點保留，只是 taste_match=False（不再硬擋丟棄）。"""
    brief = StoryBrief(narrative_day="2026-08-17", shared_cores=["外婆買糖果"], member_cores={},
                       liked_items=[], conv_snippets=[], members=[],
                       target_duration_s=1200.0, node_count=1)
    pools = {"shared": [_cand("周杰倫", "晴天", "group_resonance")]}

    async def fake_call(content, *, system, **kw):
        return ('{"arc_title":"糖果","nodes":[{"position":1,"artist":"驚喜","song":"驚喜歌",'
                '"bpm_target":80,"volume_delta_db":0}]}')

    arc = await curate_story_outline(brief, pools, [], call_fn=fake_call)
    assert len(arc.nodes) == 1
    assert arc.nodes[0].song == "驚喜歌"
    assert arc.nodes[0].taste_match is False


@pytest.mark.asyncio
async def test_curate_story_outline_none_brief_returns_none():
    async def fake_call(content, *, system, **kw):
        raise AssertionError("brief=None 不該打 LLM")
    assert await curate_story_outline(None, {}, [], call_fn=fake_call) is None


@pytest.mark.asyncio
async def test_curate_story_outline_llm_failure_returns_none():
    async def fake_call(content, *, system, **kw):
        return None
    brief = StoryBrief(narrative_day="x", shared_cores=["x", "y"], member_cores={},
                       liked_items=[], conv_snippets=[], members=[],
                       target_duration_s=1200.0, node_count=5)
    assert await curate_story_outline(brief, {}, [], call_fn=fake_call) is None


# ── build_interjection_prompt / parse_story_interjections（純函式）──────────

def test_build_interjection_prompt_lists_finalized_songs_in_order():
    arc = StoryArc(arc_title="糖果與外婆", nodes=[
        StoryNode(1, "懷舊", {}, "周杰倫", "晴天", "", 80.0, -2.0, spotlight_member="狗與露"),
        StoryNode(2, "溫馨", {}, "陶喆", "流沙", "", 90.0, 0.0, spotlight_member=None,
                  resonance_link="showay"),
    ])
    brief = StoryBrief(narrative_day="2026-08-17", shared_cores=["大家一起去海邊烤肉"],
                       member_cores={"狗與露": ["小時候外婆買糖果"]}, liked_items=[],
                       conv_snippets=[], members=["狗與露", "showay"],
                       target_duration_s=600.0, node_count=2)
    system, user = build_interjection_prompt(arc, brief)
    assert "過渡" in system
    assert "roast" in system and "嘲諷" in system
    assert "晴天" in user and "流沙" in user
    assert "外婆" in user and "烤肉" in user
    assert "showay" in user   # resonance_link 資訊有帶進去


def test_parse_story_interjections_valid_json():
    resp = '{"scripts":[{"position":1,"interjection_script":"想起小時候外婆"},' \
          '{"position":2,"interjection_script":"接著這股暖意"}]}'
    scripts = parse_story_interjections(resp)
    assert scripts == {1: "想起小時候外婆", 2: "接著這股暖意"}


@pytest.mark.parametrize("resp", ["", "不是 JSON", '{"scripts":[]}',
                                  '{"scripts":[{"position":1,"interjection_script":""}]}'])
def test_parse_story_interjections_invalid_returns_none(resp):
    assert parse_story_interjections(resp) is None


# ── curate_story_interjections（協調，注入 call_fn）──────────────────────────

@pytest.mark.asyncio
async def test_curate_story_interjections_merges_scripts_by_position():
    arc = StoryArc(arc_title="x", nodes=[
        StoryNode(1, "", {}, "周杰倫", "晴天", "", None, 0.0),
        StoryNode(2, "", {}, "陶喆", "流沙", "", None, 0.0),
    ])
    brief = StoryBrief(narrative_day="x", shared_cores=[], member_cores={}, liked_items=[],
                       conv_snippets=[], members=[], target_duration_s=1.0, node_count=2)

    async def fake_call(content, *, system, **kw):
        return ('{"scripts":[{"position":1,"interjection_script":"口白一"},'
                '{"position":2,"interjection_script":"口白二"}]}')

    result = await curate_story_interjections(arc, brief, call_fn=fake_call)
    assert result.nodes[0].interjection_script == "口白一"
    assert result.nodes[1].interjection_script == "口白二"


@pytest.mark.asyncio
async def test_curate_story_interjections_none_arc_skips_llm():
    async def fake_call(content, *, system, **kw):
        raise AssertionError("arc=None 不該打 LLM")
    assert await curate_story_interjections(None, None, call_fn=fake_call) is None


@pytest.mark.asyncio
async def test_curate_story_interjections_llm_failure_keeps_arc_with_empty_scripts():
    arc = StoryArc(arc_title="x", nodes=[StoryNode(1, "", {}, "a", "b", "", None, 0.0)])
    brief = StoryBrief(narrative_day="x", shared_cores=[], member_cores={}, liked_items=[],
                       conv_snippets=[], members=[], target_duration_s=1.0, node_count=1)

    async def fake_call(content, *, system, **kw):
        return None

    result = await curate_story_interjections(arc, brief, call_fn=fake_call)
    assert result.nodes[0].interjection_script == ""


# ── StoryArc.spotlight_coverage ───────────────────────────────────────────────

def test_spotlight_coverage_reports_missing_members():
    arc = StoryArc(arc_title="x", nodes=[
        StoryNode(1, "", {}, "a", "s1", "c1", None, 0.0, spotlight_member="狗與露"),
        StoryNode(2, "", {}, "b", "s2", "c2", None, 0.0, spotlight_member=None),
    ])
    assert arc.spotlight_coverage(["狗與露", "showay"]) == ["showay"]


def test_spotlight_coverage_empty_when_everyone_covered():
    arc = StoryArc(arc_title="x", nodes=[
        StoryNode(1, "", {}, "a", "s1", "c1", None, 0.0, spotlight_member="狗與露"),
        StoryNode(2, "", {}, "b", "s2", "c2", None, 0.0, spotlight_member="showay"),
    ])
    assert arc.spotlight_coverage(["狗與露", "showay"]) == []


# ── build_show_intro（純函式，模板組字，零 LLM call）────────────────────────

def test_build_show_intro_templates_from_verified_data():
    arc = StoryArc(arc_title="糖果與外婆", nodes=[
        StoryNode(1, "", {}, "a", "s1", "c1", None, 0.0),
        StoryNode(2, "", {}, "b", "s2", "c2", None, 0.0),
    ])
    brief = StoryBrief(narrative_day="2026-08-17", shared_cores=[], member_cores={},
                       liked_items=[], conv_snippets=[], members=["狗與露", "showay"],
                       target_duration_s=600.0, node_count=2)
    intro = build_show_intro(arc, brief)
    assert isinstance(intro, ShowIntro)
    assert "糖果與外婆" in intro.intro_script
    assert "狗與露" in intro.intro_script and "showay" in intro.intro_script
    assert "2" in intro.intro_script          # node 數量進口白
    assert intro.intro_music_path             # 有預設路徑


def test_build_show_intro_custom_music_path():
    arc = StoryArc(arc_title="x", nodes=[StoryNode(1, "", {}, "a", "s1", "c1", None, 0.0)])
    brief = StoryBrief(narrative_day="x", shared_cores=[], member_cores={}, liked_items=[],
                       conv_snippets=[], members=["狗與露"], target_duration_s=60.0,
                       node_count=1)
    intro = build_show_intro(arc, brief, intro_music_path="assets/dj_sfx/custom_intro.mp3")
    assert intro.intro_music_path == "assets/dj_sfx/custom_intro.mp3"


# ── BgmCursor（口白BGM接續播放：位置記憶重觸發）──────────────────────────────

def test_bgm_cursor_peek_defaults_to_zero_when_no_state(tmp_path):
    cursor = BgmCursor(path=str(tmp_path / "bgm.json"))
    assert cursor.peek() == 0.0


def test_bgm_cursor_advance_persists_and_next_peek_continues(tmp_path):
    cursor = BgmCursor(path=str(tmp_path / "bgm.json"))
    cursor.advance(12.5)
    assert cursor.peek() == 12.5
    cursor.advance(8.0)
    assert cursor.peek() == 20.5   # 接續上次位置，不是從頭


def test_bgm_cursor_advance_wraps_around_track_duration(tmp_path):
    cursor = BgmCursor(path=str(tmp_path / "bgm.json"))
    cursor.advance(100.0, track_duration_s=142.0)
    new_offset = cursor.advance(50.0, track_duration_s=142.0)  # 100+50=150 > 142 → 回捲
    assert new_offset == pytest.approx(8.0)
    assert cursor.peek() == pytest.approx(8.0)


def test_bgm_cursor_reset_zeroes_position(tmp_path):
    cursor = BgmCursor(path=str(tmp_path / "bgm.json"))
    cursor.advance(50.0)
    cursor.reset()
    assert cursor.peek() == 0.0


def test_bgm_cursor_fail_open_on_corrupt_state(tmp_path):
    path = tmp_path / "bgm.json"
    path.write_text("不是 JSON", encoding="utf-8")
    cursor = BgmCursor(path=str(path))
    assert cursor.peek() == 0.0   # 壞檔不炸，退化成從頭播


# ── estimate_interjection_duration_s（純函式，離線預覽粗估用）───────────────

def test_estimate_interjection_duration_s_scales_with_length():
    short = estimate_interjection_duration_s("短口白")
    long = estimate_interjection_duration_s("這是一段長很多的口白內容用來測試估計時長")
    assert long > short
    assert estimate_interjection_duration_s("") == 0.0


# ── resolve_story_arc（resolve + 品質閘，全注入式，邏輯不變）─────────────────

def _arc(*triples):
    return StoryArc(arc_title="今夜的故事", nodes=[
        StoryNode(position=i + 1, emotion_tag="", song_query={}, artist=a, song=s,
                  interjection_script=script, bpm_target=90.0, volume_delta_db=0.0)
        for i, (a, s, script) in enumerate(triples)
    ])


@pytest.mark.asyncio
async def test_resolve_story_arc_resolves_and_tags():
    async def resolve_fn(q):
        return {"title": q, "webpage_url": f"https://youtu.be/{q[:11]:_<11}", "url": "x"}
    infos = await resolve_story_arc(
        _arc(("周杰倫", "晴天", "口白一"), ("陶喆", "流沙", "口白二")),
        resolve_fn=resolve_fn, extract_vid_fn=lambda u: u.split("/")[-1])
    assert len(infos) == 2
    assert infos[0]["_story_arc_title"] == "今夜的故事"
    assert infos[0]["_story_node_position"] == 1
    assert infos[0]["_story_interjection_script"] == "口白一"
    assert infos[0]["_story_spotlight_member"] is None
    assert infos[0]["_story_resonance_link"] is None
    assert infos[1]["_story_node_position"] == 2


@pytest.mark.asyncio
async def test_resolve_story_arc_drops_resolve_failures_without_aborting():
    async def resolve_fn(q):
        if "壞" in q:
            return None
        if "炸" in q:
            raise RuntimeError("yt-dlp boom")
        return {"title": q, "webpage_url": "https://youtu.be/aaaaaaaaaaa", "url": "x"}
    infos = await resolve_story_arc(
        _arc(("a", "壞歌", "s1"), ("b", "炸歌", "s2"), ("c", "好歌", "s3")),
        resolve_fn=resolve_fn, extract_vid_fn=lambda u: u.split("/")[-1])
    assert len(infos) == 1 and "好歌" in infos[0]["title"]


@pytest.mark.asyncio
async def test_resolve_story_arc_drops_non_song_and_excluded_vid():
    async def resolve_fn(q):
        return {"title": q, "webpage_url": f"https://youtu.be/{q[-11:]}", "url": "x"}
    def is_non_song(title, dur):
        return ("合輯" in title, "compilation")
    infos = await resolve_story_arc(
        _arc(("a", "正常歌aaaaaaaaaaa", "s1"), ("b", "古典合輯bbbbbbbbbb", "s2"),
             ("c", "已播ccccccccccc", "s3")),
        resolve_fn=resolve_fn, is_non_song_fn=is_non_song,
        exclude_vids={"ccccccccccc"}, extract_vid_fn=lambda u: u.split("/")[-1])
    titles = [i["title"] for i in infos]
    assert any("正常歌" in t for t in titles)
    assert not any("合輯" in t for t in titles)
    assert not any("已播" in t for t in titles)


@pytest.mark.asyncio
async def test_resolve_story_arc_rejects_wrong_song_resolve():
    async def resolve_fn(q):
        if "足夠" in q:
            return {"title": "關喆 - 曾經你說（原唱：趙乃吉）",
                    "webpage_url": "https://youtu.be/wrongggggggg", "url": "x"}
        return {"title": "周杰倫 - 晴天 Official MV",
                "webpage_url": "https://youtu.be/rightttttttt", "url": "x"}
    infos = await resolve_story_arc(
        _arc(("關喆", "足夠", "s1"), ("周杰倫", "晴天", "s2")),
        resolve_fn=resolve_fn, extract_vid_fn=lambda u: u.split("/")[-1])
    titles = [i["title"] for i in infos]
    assert not any("曾經你說" in t for t in titles)
    assert any("晴天" in t for t in titles)


# ── build_story_arc_record / record_story_arc ────────────────────────────────

def test_build_story_arc_record_includes_narrative_day():
    infos = [
        {"title": "晴天", "duration": 269, "_story_node_position": 1,
         "_story_interjection_script": "口白一", "_story_emotion_tag": "懷舊",
         "_story_spotlight_member": "狗與露", "_story_resonance_link": "showay",
         "webpage_url": "https://youtu.be/x"},
    ]
    rec = build_story_arc_record("今夜的故事", infos, target_duration_s=300.0, ts=123.0,
                                 narrative_day="2026-08-17")
    assert rec["narrative_day"] == "2026-08-17"
    assert rec["actual_duration_s"] == 269.0
    assert rec["nodes"][0]["spotlight_member"] == "狗與露"
    assert rec["nodes"][0]["resonance_link"] == "showay"
    assert rec["intro"] is None    # 沒傳 intro → None，不是必填


def test_build_story_arc_record_includes_intro_when_given():
    intro = ShowIntro(intro_script="歡迎收聽", intro_music_path="assets/dj_sfx/show_intro.mp3")
    rec = build_story_arc_record("今夜的故事", [], target_duration_s=300.0, ts=123.0, intro=intro)
    assert rec["intro"] == {"script": "歡迎收聽", "music_path": "assets/dj_sfx/show_intro.mp3"}


def test_record_story_arc_writes_jsonl(tmp_path):
    infos = [{"title": "晴天", "duration": 269, "_story_node_position": 1,
             "_story_interjection_script": "口白", "_story_emotion_tag": "懷舊",
             "_story_spotlight_member": None, "_story_resonance_link": None,
             "webpage_url": "https://youtu.be/x"}]
    path = str(tmp_path / "dj_story_arcs.jsonl")
    rec = record_story_arc("今夜的故事", infos, target_duration_s=300.0, ts=123.0,
                           narrative_day="2026-08-17", path=path)
    assert rec["nodes"]
    with open(path, encoding="utf-8") as f:
        line = f.readline()
    assert "今夜的故事" in line and "2026-08-17" in line


def test_record_story_arc_skips_empty_nodes(tmp_path):
    path = str(tmp_path / "dj_story_arcs.jsonl")
    rec = record_story_arc("空故事", [], target_duration_s=300.0, ts=123.0, path=path)
    assert rec["nodes"] == []
    import os
    assert not os.path.exists(path)


# ── 待播節目（Prepare/Play 兩階段拆分）───────────────────────────────────────

def test_build_staged_show_keeps_original_info_dicts_for_stream_queue():
    """歌曲不重新設計 schema——原樣保留 resolve_story_arc 給的 info dict（含 url，
    不是只有 webpage_url），播放時才能直接丟進 stream_queue，不用另外重新 resolve。"""
    intro = ShowIntro(intro_script="歡迎收聽", intro_music_path="assets/dj_sfx/show_intro.mp3")
    infos = [
        {"title": "晴天", "duration": 269, "url": "https://real-cdn/x",
         "webpage_url": "https://youtu.be/x",
         "_story_arc_title": "今夜的故事", "_story_node_position": 1,
         "_story_interjection_script": "口白一",
         "_story_interjection_audio_path": "/tmp/a.mp3", "_story_interjection_duration_s": 12.0,
         "_story_emotion_tag": "懷舊", "_story_spotlight_member": "狗與露",
         "_story_resonance_link": None, "_story_taste_match": True,
         "_story_bpm_target": 90.0, "_story_volume_delta_db": 0.0},
        {"title": "流沙", "duration": 300, "url": "https://real-cdn/y",
         "webpage_url": "https://youtu.be/y",
         "_story_arc_title": "今夜的故事", "_story_node_position": 2,
         "_story_interjection_script": "",   # 沒拿到口白 → 沒有 audio_path，播放時該優雅跳過
         "_story_emotion_tag": "溫馨", "_story_spotlight_member": None,
         "_story_resonance_link": None, "_story_taste_match": False,
         "_story_bpm_target": 100.0, "_story_volume_delta_db": 1.0},
    ]
    staged = build_staged_show(infos, intro, intro_audio_path="/tmp/intro.mp3",
                               intro_audio_duration_s=8.0, ts=123.0,
                               narrative_day="2026-08-17", target_duration_s=600.0)
    assert staged["arc_title"] == "今夜的故事"
    assert staged["narrative_day"] == "2026-08-17"
    assert staged["intro"]["audio_path"] == "/tmp/intro.mp3"
    assert staged["intro"]["audio_duration_s"] == 8.0
    assert len(staged["infos"]) == 2
    assert staged["infos"][0]["url"] == "https://real-cdn/x"   # 真正可播的直連網址有保留
    assert staged["infos"][0]["_story_interjection_audio_path"] == "/tmp/a.mp3"
    assert staged["infos"][1].get("_story_interjection_audio_path") is None
    assert staged["infos"][1]["_story_taste_match"] is False


def test_save_and_load_staged_show_roundtrip(tmp_path):
    path = str(tmp_path / "staged.json")
    staged = {"arc_title": "今夜的故事", "nodes": [{"position": 1, "title": "晴天"}]}
    save_staged_show(staged, path=path)
    loaded = load_staged_show(path=path)
    assert loaded == staged


def test_load_staged_show_missing_file_returns_none(tmp_path):
    assert load_staged_show(path=str(tmp_path / "nope.json")) is None


def test_load_staged_show_corrupt_file_returns_none(tmp_path):
    path = tmp_path / "staged.json"
    path.write_text("不是 JSON", encoding="utf-8")
    assert load_staged_show(path=str(path)) is None


def test_clear_staged_show_removes_file(tmp_path):
    path = tmp_path / "staged.json"
    path.write_text("{}", encoding="utf-8")
    clear_staged_show(path=str(path))
    assert not path.exists()


def test_clear_staged_show_missing_file_is_noop(tmp_path):
    clear_staged_show(path=str(tmp_path / "nope.json"))   # 不該丟例外
