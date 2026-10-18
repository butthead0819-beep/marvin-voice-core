"""10/18 DJ 關係句（dj_relation）：autopilot 選歌講清楚這首跟在場的人有什麼關係。"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from dj_relation import Relation, build_relation, render_relation, song_counts, valid_relation_tail
from music_memory import extract_video_id
from song_name_clean import dj_display_name

from tests.test_dj_group_tone import _make_cog, _no_quick


def _display(entry):
    return dj_display_name(entry, extract_vid=extract_video_id)


def _song(title, requesters, *, url="https://www.youtube.com/watch?v=aaaaaaaaaaa"):
    return {"title": title, "uploader": "x", "webpage_url": url, "requesters": requesters}


# ── build_relation ───────────────────────────────────────────────────────────

def test_listens_song_hits_at_threshold():
    songs = {"u1": _song("周傳雄 - 黃昏", {"showay": 3})}
    rel = build_relation(songs=songs, title="黃昏", artist="周傳雄",
                          present={"showay", "狗與露"}, display=_display)
    assert rel == Relation("listens_song", "showay", "黃昏", "周傳雄")


def test_listens_song_below_threshold_is_none():
    songs = {"u1": _song("周傳雄 - 黃昏", {"showay": 2})}
    rel = build_relation(songs=songs, title="黃昏", artist="周傳雄",
                          present={"showay", "狗與露"}, display=_display)
    assert rel is None


def test_absent_listener_counts_do_not_leak_in():
    songs = {"u1": _song("周傳雄 - 黃昏", {"Alice": 5, "狗與露": 0})}
    rel = build_relation(songs=songs, title="黃昏", artist="周傳雄",
                          present={"showay", "狗與露"}, display=_display)
    assert rel is None


def test_present_none_is_none():
    songs = {"u1": _song("周傳雄 - 黃昏", {"showay": 3})}
    rel = build_relation(songs=songs, title="黃昏", artist="周傳雄",
                          present=None, display=_display)
    assert rel is None


def test_marvin_for_everyone_not_counted():
    songs = {"u1": _song("周傳雄 - 黃昏", {"Marvin推薦（點給大家）": 10})}
    rel = build_relation(songs=songs, title="黃昏", artist="周傳雄",
                          present={"showay", "狗與露"}, display=_display)
    assert rel is None


def test_marvin_picked_for_x_counts_as_x_listening():
    """10/18：「我們大多數都是常常聽」——挑給 showay 聽的 3 次算 showay 常聽。"""
    songs = {"u1": _song("周傳雄 - 黃昏", {"Marvin推薦（為showay）": 2, "showay": 1})}
    rel = build_relation(songs=songs, title="黃昏", artist="周傳雄",
                          present={"showay", "狗與露"}, display=_display)
    assert rel == Relation("listens_song", "showay", "黃昏", "周傳雄")


def test_group_listened_two_present_members():
    songs = {"u1": _song("周傳雄 - 黃昏", {"showay": 2, "狗與露": 1})}
    rel = build_relation(songs=songs, title="黃昏", artist="周傳雄",
                          present={"showay", "狗與露"}, display=_display)
    assert rel == Relation("group_listened", "showay 跟 狗與露", "黃昏", "周傳雄")


def test_group_listened_needs_two_people():
    songs = {"u1": _song("周傳雄 - 黃昏", {"showay": 2})}
    rel = build_relation(songs=songs, title="黃昏", artist="周傳雄",
                          present={"showay", "狗與露"}, display=_display)
    assert rel is None


def test_anchor_song_hits():
    songs = {
        "u1": _song("錨地歌", {"showay": 3}, url="https://www.youtube.com/watch?v=bbbbbbbbbbb"),
        "u2": _song("周傳雄 - 黃昏", {}, url="https://www.youtube.com/watch?v=aaaaaaaaaaa"),
    }
    rel = build_relation(songs=songs, title="黃昏", artist="周傳雄",
                          present={"showay", "狗與露"}, display=_display,
                          anchor_title="錨地歌")
    assert rel == Relation("anchor_song", "showay", "黃昏", "周傳雄", anchor="錨地歌")


def test_anchor_song_same_as_main_title_skipped():
    # 主歌是「某歌手 - 黃昏」，錨點「周傳雄 - 黃昏」同名 → 不當錨點（同名不同歌手也不算 listens_song）
    songs = {"u1": _song("周傳雄 - 黃昏", {"showay": 3})}
    rel = build_relation(songs=songs, title="黃昏", artist="某歌手",
                          present={"showay", "狗與露"}, display=_display,
                          anchor_title="周傳雄 - 黃昏")
    assert rel is None


def test_same_artist_hits_with_three_other_songs():
    songs = {
        "u1": _song("周傳雄 - 歌一", {"showay": 1}, url="https://www.youtube.com/watch?v=bbbbbbbbbbb"),
        "u2": _song("周傳雄 - 歌二", {"showay": 1}, url="https://www.youtube.com/watch?v=ccccccccccc"),
        "u3": _song("周傳雄 - 歌三", {"showay": 1}, url="https://www.youtube.com/watch?v=ddddddddddd"),
    }
    rel = build_relation(songs=songs, title="黃昏", artist="周傳雄",
                          present={"showay", "狗與露"}, display=_display)
    assert rel == Relation("same_artist", "showay", "黃昏", "周傳雄")


def test_same_artist_below_threshold_is_none():
    songs = {
        "u1": _song("周傳雄 - 歌一", {"showay": 1}, url="https://www.youtube.com/watch?v=bbbbbbbbbbb"),
        "u2": _song("周傳雄 - 歌二", {"showay": 1}, url="https://www.youtube.com/watch?v=ccccccccccc"),
    }
    rel = build_relation(songs=songs, title="黃昏", artist="周傳雄",
                          present={"showay", "狗與露"}, display=_display)
    assert rel is None


def test_personal_list_present():
    rel = build_relation(songs={}, title="黃昏", artist="周傳雄",
                          present={"showay", "狗與露"}, display=_display,
                          personal_user="showay")
    assert rel == Relation("personal_list", "showay", "黃昏", "周傳雄")


def test_personal_list_absent_is_none():
    rel = build_relation(songs={}, title="黃昏", artist="周傳雄",
                          present={"狗與露"}, display=_display,
                          personal_user="showay")
    assert rel is None


def test_listens_song_takes_priority_over_same_artist():
    songs = {
        "u1": _song("周傳雄 - 黃昏", {"showay": 3}, url="https://www.youtube.com/watch?v=aaaaaaaaaaa"),
        "u2": _song("周傳雄 - 歌一", {"showay": 1}, url="https://www.youtube.com/watch?v=bbbbbbbbbbb"),
        "u3": _song("周傳雄 - 歌二", {"showay": 1}, url="https://www.youtube.com/watch?v=ccccccccccc"),
        "u4": _song("周傳雄 - 歌三", {"showay": 1}, url="https://www.youtube.com/watch?v=ddddddddddd"),
    }
    rel = build_relation(songs=songs, title="黃昏", artist="周傳雄",
                          present={"showay", "狗與露"}, display=_display)
    assert rel.kind == "listens_song"


def test_same_title_different_artist_not_counted_as_listens_song():
    songs = {"u1": _song("B - 黃昏", {"showay": 3})}
    counts = song_counts(songs, "黃昏", "A", display=_display)
    assert counts == {}


# ── render_relation ──────────────────────────────────────────────────────────

def _relation_lines():
    from cogs.music_cog import MusicCog
    return MusicCog._RELATION_LINES


def test_render_relation_respects_recent():
    lines = _relation_lines()
    pool = lines["listens_song"]
    recent = tuple(pool[1:])
    rel = Relation("listens_song", "showay", "黃昏", "周傳雄")
    for _ in range(20):
        text, tmpl = render_relation(rel, lines, recent=recent)
        assert tmpl == pool[0]


def test_render_relation_no_artist_uses_title_only_song():
    lines = _relation_lines()
    rel = Relation("listens_song", "showay", "黃昏", "")
    for _ in range(30):
        text, tmpl = render_relation(rel, lines)
        assert text == tmpl.format(who="showay", title="黃昏", artist="", anchor="", song="《黃昏》")


@pytest.mark.parametrize("kind", ["listens_song", "group_listened", "anchor_song", "same_artist", "personal_list"])
def test_render_relation_all_templates_format(kind):
    lines = _relation_lines()
    rel = Relation(kind, "showay 跟 狗與露", "黃昏", "周傳雄", anchor="錨地歌")
    for tmpl in lines[kind]:
        text, used = render_relation(rel, {kind: [tmpl]})
        assert used == tmpl
        assert text


# ── valid_relation_tail ───────────────────────────────────────────────────────

def test_valid_relation_tail_accepts_clean_line():
    assert valid_relation_tail("『黄昏的地平线』那句一出來就對了。", {"showay"}) is True


@pytest.mark.parametrize("tail", [
    "好聽",
    "showay 一定愛",
    "大家常聽的副歌",
    "我最愛這段",
    "身為AI的我覺得這段很棒很棒很棒很棒",
    "這句超長完全超過四十個字的限制測試用句子超過上限就該被拒絕才對啊真的超長超長超長超長超長",
])
def test_valid_relation_tail_rejects(tail):
    assert valid_relation_tail(tail, {"showay"}) is False


# ── 模板守門 ─────────────────────────────────────────────────────────────────

def test_relation_templates_avoid_banned_words():
    lines = _relation_lines()
    for kind, pool in lines.items():
        for t in pool:
            for bad in ("最近", "這陣子", "點", "我"):
                assert bad not in t, (kind, t)


# ── 接線：_fetch_dj_interjection_raw ───────────────────────────────────────────

def _autopilot_info():
    return {
        "title": "周傳雄 - 黃昏", "uploader": "x", "requested_by": "Marvin推薦（為showay）",
        "url": "https://www.youtube.com/watch?v=aaaaaaaaaaa",
        "_spotlight": "showay", "_lane": "spotlight",
    }


@pytest.mark.asyncio
async def test_autopilot_relation_line_wins(monkeypatch):
    _no_quick(monkeypatch)
    cog = _make_cog(online_members=["showay", "狗與露"])
    cog.bot.music_memory._data = {"songs": {"u1": _song("周傳雄 - 黃昏", {"showay": 3})}}
    cog.bot.router.generate_dynamic_system_msg = AsyncMock(
        return_value="這是一句不該出現的長口白內容測試用。")
    dj = await cog._fetch_dj_interjection_raw(_autopilot_info())
    from cogs.music_cog import MusicCog
    pool_texts = set()
    rel = Relation("listens_song", "showay", "黃昏", "周傳雄")
    for tmpl in MusicCog._RELATION_LINES["listens_song"]:
        text, _ = render_relation(rel, {"listens_song": [tmpl]})
        pool_texts.add(text)
    assert any(dj["text"].startswith(p) for p in pool_texts)
    assert "showay" in dj["text"]
    assert "黃昏" in dj["text"]


@pytest.mark.asyncio
async def test_autopilot_no_history_llm_context_has_no_recommend_reason(monkeypatch):
    _no_quick(monkeypatch)
    cog = _make_cog(online_members=["showay", "狗與露"])
    cog.bot.music_memory._data = {"songs": {}}
    dj = await cog._fetch_dj_interjection_raw(_autopilot_info())
    call = cog.bot.router.generate_dynamic_system_msg.call_args
    if call is not None:
        ctx = call.kwargs.get("context", "") or (call.args[1] if len(call.args) > 1 else "")
        assert "推薦理由" not in ctx


@pytest.mark.asyncio
async def test_real_request_untouched_by_relation(monkeypatch):
    _no_quick(monkeypatch)
    cog = _make_cog(online_members=["showay", "狗與露"])
    cog.bot.music_memory._data = {"songs": {"u1": _song("周傳雄 - 黃昏", {"showay": 3})}}
    cog.bot.router.generate_dynamic_system_msg = AsyncMock(return_value="真人點歌正常口白內容測試")
    info = {"title": "周傳雄 - 黃昏", "uploader": "x", "requested_by": "showay",
            "url": "https://www.youtube.com/watch?v=aaaaaaaaaaa"}
    dj = await cog._fetch_dj_interjection_raw(info)
    from cogs.music_cog import MusicCog
    rel = Relation("listens_song", "showay", "黃昏", "周傳雄")
    relation_texts = {render_relation(rel, {"listens_song": [t]})[0]
                       for t in MusicCog._RELATION_LINES["listens_song"]}
    assert dj["text"] not in relation_texts


def test_quick_segue_template_need_song_only_picks_song_templates():
    from cogs.music_cog import MusicCog
    for n in (1, 2, 5):
        for _ in range(20):
            assert "{song}" in MusicCog._quick_segue_template(n, need_song=True)


@pytest.mark.asyncio
async def test_rare_song_with_relation_still_speaks_relation(monkeypatch):
    """少播歌（focus=song）也要先講關係：relation 排在 song 之前。"""
    _no_quick(monkeypatch)
    cog = _make_cog(online_members=["showay", "狗與露"])
    cog.bot.music_memory._data = {"songs": {"u1": _song("周傳雄 - 黃昏", {"showay": 3})}}
    info = _autopilot_info()
    info["_server_plays"] = 1
    dj = await cog._fetch_dj_interjection_raw(info)
    from cogs.music_cog import MusicCog
    assert any(dj["text"].startswith(t.split("{")[0]) for t in MusicCog._RELATION_LINES["listens_song"])
    assert "showay" in dj["text"]


@pytest.mark.asyncio
async def test_seed_title_becomes_anchor_relation(monkeypatch):
    """T2 discovery 的種子歌（_seed_title）showay 常聽 → 講「常聽《堅持到底》」。"""
    _no_quick(monkeypatch)
    cog = _make_cog(online_members=["showay", "狗與露"])
    cog.bot.music_memory._data = {"songs": {
        "u2": _song("阿杜 - 堅持到底", {"showay": 3}, url="https://www.youtube.com/watch?v=bbbbbbbbbbb"),
    }}
    info = _autopilot_info()
    info["_lane"] = "discovery"
    info["_seed_title"] = "阿杜 - 堅持到底"
    dj = await cog._fetch_dj_interjection_raw(info)
    assert "《堅持到底》" in dj["text"]
    assert "showay" in dj["text"]
