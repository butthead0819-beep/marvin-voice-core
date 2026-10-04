"""10/4 使用者定：依新舊歌決定素材池 + 同一首歌重播換素材。

- 少播歌（focus=song）只抽「歌本身」的素材（pick_song_facet）。
- 同一首歌最近 2 次口白用過的素材（mode / 歌曲素材 / 歌詞句 / facet / 笑話）這輪避開。
"""
from __future__ import annotations

import asyncio
import json
import random
from unittest.mock import AsyncMock, MagicMock

import pytest

import dj_narration_log
from dj_gacha_narrator import pick_gacha_motivation
from dj_lyric_pick import pick_chorus_line, pick_lyric_line
from dj_narration_log import recent_narrations_for_song, used_materials
from dj_narration_orchestrator import pick_song_facet, pick_song_material, select_narration_mode
from dj_topic_selector import TopicCooldownStore, select_mode
from tests.test_dj_story_context import _ctx_str, _info, _make_cog, _only

AUTO = "Marvin推薦（為大肚）"
REPEAT_LYRICS = "我很平庸的人生啊\n我很平庸的人生啊\n我很平庸的人生啊\n"


def _write_log(path, rows):
    lines = []
    for r in rows:
        lines.append("not json{" if r is None else json.dumps(r, ensure_ascii=False))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ── 1. recent_narrations_for_song ────────────────────────────────────────────

def test_recent_narrations_returns_last_two_old_to_new_both_song_forms(tmp_path):
    p = tmp_path / "log.jsonl"
    _write_log(p, [
        {"song": "周杰倫 - 夜曲", "text": "a"},
        None,  # 壞行
        {"song": "別首 - 別歌", "text": "x"},
        {"song": "夜曲", "text": "b"},
        {"song": "周杰倫 - 夜曲", "text": "c"},
    ])
    got = recent_narrations_for_song("夜曲", n=2, path=p)
    assert [r["text"] for r in got] == ["b", "c"]


def test_recent_narrations_missing_file_returns_empty(tmp_path):
    assert recent_narrations_for_song("夜曲", path=tmp_path / "nope.jsonl") == []


def test_recent_narrations_blank_title_returns_empty(tmp_path):
    p = tmp_path / "log.jsonl"
    _write_log(p, [{"song": " - ", "text": "a"}])
    assert recent_narrations_for_song("", path=p) == []


# ── 2. used_materials ────────────────────────────────────────────────────────

def test_used_materials_collects_every_field():
    records = [
        {"mode": "song", "lyric_material": None, "facet": "lyric",
         "facet_text": "歌詞：『我很平庸』", "source": "llm", "text": "x"},
        {"mode": "memory_match", "song_material": "喜好線索：x",
         "lyric_material": "歌詞：『副歌句子』", "facet": "album", "source": "joke", "text": "笑話甲"},
        {"mode": "life", "lyric_material": "歌詞呼應：不算引號", "facet": None, "source": "quick"},
    ]
    used = used_materials(records)
    assert used["modes"] == {"song", "memory_match", "life"}
    assert used["song_materials"] == {"喜好線索：x"}
    assert used["lyric_quotes"] == {"我很平庸", "副歌句子"}
    assert used["facets"] == {"lyric", "album"}
    assert used["jokes"] == {"笑話甲"}


# ── 3. pick_lyric_line ───────────────────────────────────────────────────────

_LYRICS_RANKED = (
    "我很平庸的人生啊\n我很平庸的人生啊\n我很平庸的人生啊\n"   # 3 次
    "夜深了還在等你回\n夜深了還在等你回\n夜深了還在等你回\n"   # 3 次
    "有一天會好起來\n有一天會好起來\n"                        # 2 次（前 3 名之一）
    "孤單一個人走路\n孤單一個人走路\n"                        # 2 次，第 4 名以後
    "沒有人知道我\n"                                          # 只出現 1 次
)


def test_pick_chorus_line_unchanged():
    assert pick_chorus_line(_LYRICS_RANKED) == "我很平庸的人生啊"


def test_pick_lyric_line_only_top_three_repeats():
    seen = {pick_lyric_line(_LYRICS_RANKED, rng=random.Random(s)) for s in range(200)}
    assert seen == {"我很平庸的人生啊", "夜深了還在等你回", "有一天會好起來"}


def test_pick_lyric_line_respects_exclude():
    for s in range(50):
        got = pick_lyric_line(_LYRICS_RANKED, exclude={"我很平庸的人生啊"}, rng=random.Random(s))
        assert got in {"夜深了還在等你回", "有一天會好起來"}


def test_pick_lyric_line_all_excluded_returns_none():
    ex = {"我很平庸的人生啊", "夜深了還在等你回", "有一天會好起來"}
    assert pick_lyric_line(_LYRICS_RANKED, exclude=ex) is None


def test_pick_lyric_line_no_repeats_returns_none():
    assert pick_lyric_line("沒有重複的句子一\n沒有重複的句子二") is None
    assert pick_lyric_line(None) is None


# ── 4. pick_gacha_motivation(exclude=...) ────────────────────────────────────

_CARD = {
    "audiophile_guide": "前奏木吉他刷弦一出來，就是千禧年代的校園回憶。",
    "lyric_hook": {"quote": "我很平庸", "subtext": "自嘲"},
}


def test_gacha_exclude_never_picks_excluded_mode():
    for _ in range(60):
        m = pick_gacha_motivation(_CARD, exclude=["irony", "tea"])
        assert m is not None and m.mode == "hook"


def test_gacha_all_excluded_returns_none():
    assert pick_gacha_motivation(_CARD, exclude=["irony", "tea", "hook"]) is None


# ── 5. pick_song_facet ───────────────────────────────────────────────────────

def test_song_facet_respects_exclude():
    avail = {"album": "歌曲資料：A", "lyric": "歌詞：『L』"}
    for s in range(40):
        facet, text = pick_song_facet(avail, exclude=["album"], rng=random.Random(s))
        assert facet == "lyric" and text == "歌詞：『L』"


def test_song_facet_all_excluded_picks_from_all():
    avail = {"album": "歌曲資料：A", "lyric": "歌詞：『L』"}
    got = pick_song_facet(avail, exclude=["album", "lyric"], rng=random.Random(1))
    assert got is not None and got[1] == avail[got[0]]


def test_song_facet_empty_returns_none():
    assert pick_song_facet({}) is None


# ── 6. select_mode(exclude_modes=...) ────────────────────────────────────────

def _fresh_store(tmp_path, name):
    return TopicCooldownStore(path=str(tmp_path / name))


def test_select_mode_excluded_modes_never_drawn(tmp_path):
    for s in range(60):
        _, mode = select_mode([], ["露營"], _fresh_store(tmp_path, f"a{s}.json"),
                              has_conversation=True, exclude_modes={"interest", "conversation"},
                              rng=random.Random(s))
        assert mode == "atmosphere"


def test_select_mode_keeps_original_pool_when_exclusion_empties_it(tmp_path):
    seen = set()
    for s in range(60):
        _, mode = select_mode([], ["露營"], _fresh_store(tmp_path, f"b{s}.json"),
                              has_conversation=True,
                              exclude_modes={"interest", "conversation", "atmosphere", "quick"},
                              rng=random.Random(s))
        seen.add(mode)
    assert seen <= {"interest", "conversation", "atmosphere"} and seen


# ── 7. select_narration_mode ─────────────────────────────────────────────────

def test_memory_match_excluded_falls_through(tmp_path):
    out = select_narration_mode(life=[], interests=[], topic_store=_fresh_store(tmp_path, "m.json"),
                                memory_evidence="大肚說過超愛這個歌手",
                                exclude_modes={"memory_match"})
    assert out[1] != "memory_match"


def test_song_focus_ignores_guide_and_exclusions(tmp_path):
    out = select_narration_mode(life=[], interests=[], topic_store=_fresh_store(tmp_path, "s.json"),
                                has_guide=True, focus="song", exclude_modes={"song", "guide"})
    assert out == (None, "song")


# ── 8. pick_song_material(exclude=...) ───────────────────────────────────────

def test_pick_song_material_exclude_and_all_excluded():
    cands = ["喜好線索：a", "情感記錄：b"]
    for s in range(30):
        assert pick_song_material(cands, exclude=["喜好線索：a"], rng=random.Random(s)) == "情感記錄：b"
    assert pick_song_material(cands, exclude=cands) is None


# ── 9. _fetch_dj_interjection_raw 整合 ───────────────────────────────────────

@pytest.fixture
def narration_log(tmp_path, monkeypatch):
    p = tmp_path / "dj_narration.jsonl"
    monkeypatch.setattr(dj_narration_log, "_LOG_PATH", p)
    return p


def _lyrics_task(text):
    async def _run():
        return text
    return asyncio.ensure_future(_run())


def _canon_album():
    return ({"artist": "周杰倫", "title": "夜曲", "album": "十一月的蕭邦", "year": 2005}, None)


@pytest.mark.asyncio
async def test_rare_autopilot_song_avoids_facet_used_twice(tmp_path, narration_log):
    cog = _make_cog(tmp_path=tmp_path)
    cog.stream_history = []
    cog._dj_song_material = AsyncMock(return_value=_canon_album())
    cog._dj_heat_bank().take = MagicMock(return_value=[])
    _write_log(narration_log, [
        {"song": "周杰倫 - 夜曲", "facet": "album", "text": "x"},
        {"song": "周杰倫 - 夜曲", "facet": "related", "text": "y"},
    ])
    info = _info(title="周杰倫 - 夜曲", requester=AUTO)
    info.update(_arc_role="discovery", _server_plays=1)

    await cog._fetch_dj_interjection_raw(info, lyrics_task=_lyrics_task(REPEAT_LYRICS))

    ctx = _ctx_str(cog)
    assert "十一月的蕭邦" not in ctx
    assert "歌詞：『我很平庸的人生啊』" in ctx


@pytest.mark.asyncio
async def test_repeat_song_avoids_lyric_quote_used_last_time(tmp_path, narration_log, monkeypatch):
    _only(monkeypatch, "atmosphere")
    cog = _make_cog(tmp_path=tmp_path)
    cog.stream_history = []
    lyrics = ("我很平庸的人生啊\n" * 3) + ("夜深了還在等你回\n" * 3)
    _write_log(narration_log, [
        {"song": "周杰倫 - 夜曲", "mode": "atmosphere", "lyric_material": "歌詞：『我很平庸的人生啊』"},
    ])

    await cog._fetch_dj_interjection_raw(_info(title="周杰倫 - 夜曲", requester="大肚"),
                                         lyrics_task=_lyrics_task(lyrics))

    ctx = _ctx_str(cog)
    assert "歌詞：『夜深了還在等你回』" in ctx
    assert "歌詞：『我很平庸的人生啊』" not in ctx


@pytest.mark.asyncio
async def test_narration_log_records_facet_fields(tmp_path, narration_log):
    cog = _make_cog(tmp_path=tmp_path)
    cog.stream_history = []
    cog._dj_song_material = AsyncMock(return_value=_canon_album())
    cog._dj_heat_bank().take = MagicMock(return_value=[])
    info = _info(title="周杰倫 - 夜曲", requester=AUTO)
    info.update(_arc_role="discovery", _server_plays=0)

    await cog._fetch_dj_interjection_raw(info)

    rec = json.loads(narration_log.read_text(encoding="utf-8").splitlines()[-1])
    assert rec["facet"] == "album"
    assert rec["facet_text"].startswith("歌曲資料：")
