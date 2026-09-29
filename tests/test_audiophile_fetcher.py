"""TDD：audiophile_fetcher.fetch_audiophile_guide（docs/PLAN_audiophile_music_tour.md Phase 2）。

設計：不自開 client、不寫死 model——重用 intent_agents.grounded_qa_agent.grounded_answer
（free→付費鏈 + PaidUsageGuard 記帳 + L1 拒答 / L2 無 grounding_chunks 幻覺 guard），
只換 system_prompt（build_audiophile_guide_prompt）與記帳 caller。

快取：SongKnowledgeStore 同一個 records/song_knowledge.json，但用獨立 key
「audiophile::<artist> - <title>」——不跟 get_or_extract_insight 的「<artist> - <title>」
記錄共用 dict（那邊 set 是整份覆寫，共用會互相洗掉欄位）。
失敗 → 保底台詞（不含任何事實），且**不寫快取**（下次還有機會查到真的）。
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from song_knowledge_store import SongKnowledgeStore

GUIDE = "別被雙截棍的快嘴騙了，" + "仔細聽左聲道那把二胡，" * 6 + "戴上耳機。"
KEY = "audiophile::周杰倫 - 雙截棍"


def _resp(text, *, chunks=1, finish="STOP"):
    chunk_objs = [SimpleNamespace(web=SimpleNamespace(uri="https://example.com/a"))
                  for _ in range(chunks)]
    gm = SimpleNamespace(grounding_chunks=chunk_objs)
    cand = SimpleNamespace(grounding_metadata=gm, finish_reason=finish)
    return SimpleNamespace(
        text=text, candidates=[cand],
        usage_metadata=SimpleNamespace(prompt_token_count=100, candidates_token_count=50),
    )


def _client(resp=None, exc=None):
    cli = MagicMock()
    if exc is not None:
        cli.aio.models.generate_content = AsyncMock(side_effect=exc)
    else:
        cli.aio.models.generate_content = AsyncMock(return_value=resp)
    return cli


def _guard(allow=True):
    g = MagicMock()
    g.allow.return_value = allow
    g.record = MagicMock()
    return g


@pytest.fixture
def store(tmp_path):
    return SongKnowledgeStore(path=str(tmp_path / "song_knowledge.json"))


@pytest.mark.asyncio
async def test_cache_hit_does_not_call_api(store):
    from audiophile_fetcher import fetch_audiophile_guide

    store.set(KEY, {"audiophile_guide": "快取裡的導聆台詞"})
    free = _client(_resp("不該被呼叫"))
    paid = _client(_resp("不該被呼叫"))

    out = await fetch_audiophile_guide("雙截棍", "周杰倫", free_client=free,
                                       paid_client=paid, guard=_guard(), store=store)

    assert out == "快取裡的導聆台詞"
    free.aio.models.generate_content.assert_not_awaited()
    paid.aio.models.generate_content.assert_not_awaited()


@pytest.mark.asyncio
async def test_cache_miss_calls_grounded_with_google_search_and_persists(store, tmp_path):
    from audiophile_fetcher import fetch_audiophile_guide
    from dj_prompt_builder import build_audiophile_guide_prompt

    # 同一首歌既有的賞析記錄（get_or_extract_insight 寫的）不可被洗掉
    store.set("周杰倫 - 雙截棍", {"lyricist": "方文山"})
    free = _client(_resp(GUIDE))

    out = await fetch_audiophile_guide("雙截棍", "周杰倫", free_client=free,
                                       paid_client=None, guard=_guard(), store=store)

    assert out == GUIDE
    free.aio.models.generate_content.assert_awaited_once()
    config = free.aio.models.generate_content.await_args.kwargs["config"]
    assert config.tools and config.tools[0].google_search is not None
    assert config.system_instruction == build_audiophile_guide_prompt("周杰倫 - 雙截棍")

    # 永久快取：重新從檔案讀也在
    reloaded = SongKnowledgeStore(path=str(tmp_path / "song_knowledge.json"))
    assert reloaded.get(KEY)["audiophile_guide"] == GUIDE
    assert reloaded.get("周杰倫 - 雙截棍") == {"lyricist": "方文山"}


@pytest.mark.asyncio
async def test_second_call_is_zero_api_cost(store):
    from audiophile_fetcher import fetch_audiophile_guide

    free = _client(_resp(GUIDE))
    for _ in range(2):
        out = await fetch_audiophile_guide("雙截棍", "周杰倫", free_client=free,
                                           paid_client=None, guard=_guard(), store=store)
        assert out == GUIDE
    free.aio.models.generate_content.assert_awaited_once()


@pytest.mark.asyncio
async def test_paid_call_recorded_with_audiophile_caller(store):
    from audiophile_fetcher import fetch_audiophile_guide

    free = _client(exc=RuntimeError("RESOURCE_EXHAUSTED"))
    paid = _client(_resp(GUIDE))
    guard = _guard(allow=True)

    out = await fetch_audiophile_guide("雙截棍", "周杰倫", free_client=free,
                                       paid_client=paid, guard=guard, store=store)

    assert out == GUIDE
    guard.record.assert_called()
    assert guard.record.call_args.kwargs["caller"] == "audiophile_guide"


@pytest.mark.asyncio
async def test_api_failure_returns_fallback_and_does_not_cache(store):
    from audiophile_fetcher import FALLBACK_GUIDE_TEMPLATE, fetch_audiophile_guide

    free = _client(exc=RuntimeError("boom"))

    out = await fetch_audiophile_guide("雙截棍", "周杰倫", free_client=free,
                                       paid_client=None, guard=_guard(), store=store)

    assert out == FALLBACK_GUIDE_TEMPLATE.format(title="雙截棍")
    assert "雙截棍" in out
    assert store.get(KEY) is None


@pytest.mark.asyncio
async def test_no_grounding_sources_returns_fallback(store):
    """L2 guard：沒真的搜到網頁（grounding_chunks 空）＝疑似幻覺 → 保底，不快取。"""
    from audiophile_fetcher import FALLBACK_GUIDE_TEMPLATE, fetch_audiophile_guide

    free = _client(_resp(GUIDE, chunks=0))

    out = await fetch_audiophile_guide("雙截棍", "周杰倫", free_client=free,
                                       paid_client=None, guard=_guard(), store=store)

    assert out == FALLBACK_GUIDE_TEMPLATE.format(title="雙截棍")
    assert store.get(KEY) is None


@pytest.mark.asyncio
async def test_llm_refusal_returns_fallback(store):
    """L1 guard：prompt 要求查不到就回「無」→ grounded_answer 擋下 → 保底。"""
    from audiophile_fetcher import FALLBACK_GUIDE_TEMPLATE, fetch_audiophile_guide

    free = _client(_resp("無"))

    out = await fetch_audiophile_guide("雙截棍", "周杰倫", free_client=free,
                                       paid_client=None, guard=_guard(), store=store)

    assert out == FALLBACK_GUIDE_TEMPLATE.format(title="雙截棍")
    assert store.get(KEY) is None


@pytest.mark.asyncio
async def test_no_artist_uses_title_only_key(store):
    from audiophile_fetcher import fetch_audiophile_guide

    free = _client(_resp(GUIDE))
    out = await fetch_audiophile_guide("雙截棍", "", free_client=free,
                                       paid_client=None, guard=_guard(), store=store)
    assert out == GUIDE
    assert store.get("audiophile::雙截棍")["audiophile_guide"] == GUIDE


def test_fallback_template_has_no_factual_claims():
    """保底台詞只准是「不多嘴、直接聽」這類無事實內容（說錯不如沒說）。"""
    from audiophile_fetcher import FALLBACK_GUIDE_TEMPLATE

    assert "{title}" in FALLBACK_GUIDE_TEMPLATE
    text = FALLBACK_GUIDE_TEMPLATE.format(title="X")
    assert "耳機" in text
    assert len(text) <= 40


# ── render_audiophile_guide（Phase 4.1：抓稿 + TTS 預渲染 + 量真實秒數，就地標記 info）──

def _tts(path="/tmp/guide.mp3", exc=None):
    eng = MagicMock()
    eng.generate_audio = AsyncMock(side_effect=exc) if exc else AsyncMock(return_value=path)
    return eng


async def _render(info, store, *, free=None, tts=None, dur=19.5):
    from audiophile_fetcher import render_audiophile_guide
    probe = AsyncMock(return_value=dur)
    await render_audiophile_guide(
        info, title="雙截棍", artist="周杰倫",
        free_client=free if free is not None else _client(_resp(GUIDE)),
        paid_client=None, guard=_guard(), store=store,
        tts_engine=tts if tts is not None else _tts(), probe_duration=probe,
    )
    return probe


@pytest.mark.asyncio
async def test_render_marks_info_with_text_audio_and_probed_duration(store):
    info = {"title": "周杰倫 Jay Chou【雙截棍】"}
    tts = _tts("/tmp/guide.mp3")
    probe = await _render(info, store, tts=tts, dur=19.5)

    tts.generate_audio.assert_awaited_once_with(GUIDE)
    probe.assert_awaited_once_with("/tmp/guide.mp3")
    assert info["_audiophile_guide"] is True
    assert info["_audiophile_guide_text"] == GUIDE
    assert info["_audiophile_guide_audio"] == "/tmp/guide.mp3"
    assert info["_audiophile_guide_dur"] == 19.5


@pytest.mark.asyncio
async def test_render_tts_failure_still_marks_guide_without_audio(store):
    """TTS 掛掉：仍標記導聆歌（Phase 3 會跳過 pre-roll、照樣從 0 播），不拋例外。"""
    for tts in (_tts(None), _tts(exc=RuntimeError("edge-tts 429"))):
        info = {"title": "x"}
        probe = await _render(info, store, tts=tts)
        probe.assert_not_awaited()
        assert info["_audiophile_guide"] is True
        assert info["_audiophile_guide_text"] == GUIDE
        assert info["_audiophile_guide_audio"] is None
        assert info["_audiophile_guide_dur"] == 0.0


@pytest.mark.asyncio
async def test_render_probe_failure_means_no_audio(store):
    """量不到秒數（ffprobe 回 0）就不能保證「講完才開播」→ 當作沒音檔。"""
    info = {"title": "x"}
    await _render(info, store, dur=0.0)
    assert info["_audiophile_guide_audio"] is None
    assert info["_audiophile_guide_dur"] == 0.0


# ── fetch_album_tracklist（Phase 4.2 /tour：Gemini grounded 查曲目）────────────

ALBUM_KEY = "album_tracklist::周杰倫 - 范特西"
TRACKS_TEXT = "1. 愛在西元前\n2、爸我回來了\n3) 簡單愛\n這行不是曲目\n4. 《忍者》\n5. 簡單愛\n"


async def _tracks(store, free=None, paid=None, guard=None):
    from audiophile_fetcher import fetch_album_tracklist
    return await fetch_album_tracklist(
        "周杰倫", "范特西",
        free_client=free if free is not None else _client(_resp(TRACKS_TEXT)),
        paid_client=paid, guard=guard or _guard(), store=store)


@pytest.mark.asyncio
async def test_tracklist_parses_numbered_lines_dedups_and_caches(store, tmp_path):
    free = _client(_resp(TRACKS_TEXT))
    out = await _tracks(store, free=free)

    assert out == ["愛在西元前", "爸我回來了", "簡單愛", "忍者"]
    config = free.aio.models.generate_content.await_args.kwargs["config"]
    from dj_prompt_builder import build_album_tracklist_prompt
    assert config.system_instruction == build_album_tracklist_prompt("周杰倫", "范特西")
    reloaded = SongKnowledgeStore(path=str(tmp_path / "song_knowledge.json"))
    assert reloaded.get(ALBUM_KEY)["tracks"] == out


@pytest.mark.asyncio
async def test_tracklist_cache_hit_zero_api(store):
    store.set(ALBUM_KEY, {"tracks": ["A", "B"]})
    free = _client(_resp("不該被呼叫"))
    assert await _tracks(store, free=free) == ["A", "B"]
    free.aio.models.generate_content.assert_not_awaited()


@pytest.mark.asyncio
async def test_tracklist_long_album_not_truncated(store):
    """AmbientQA 預設 140 字截斷會把長專輯砍半——曲目查詢要放寬。"""
    text = "\n".join(f"{i}. 很長很長的一首歌名第{i}首" for i in range(1, 16))
    out = await _tracks(store, free=_client(_resp(text)))
    assert len(out) == 15


@pytest.mark.asyncio
async def test_tracklist_caps_track_count(store):
    from audiophile_fetcher import MAX_TOUR_TRACKS
    text = "\n".join(f"{i}. 歌{i}" for i in range(1, MAX_TOUR_TRACKS + 10))
    out = await _tracks(store, free=_client(_resp(text)))
    assert len(out) == MAX_TOUR_TRACKS


@pytest.mark.asyncio
async def test_tracklist_first_track_starting_with_wu_not_rejected(store):
    """「無與倫比的美麗」開頭是「無」——編號格式讓它不撞 L1 拒答 guard。"""
    out = await _tracks(store, free=_client(_resp("1. 無與倫比的美麗\n2. 小情歌")))
    assert out == ["無與倫比的美麗", "小情歌"]


@pytest.mark.asyncio
async def test_tracklist_failures_return_empty_and_do_not_cache(store):
    for free in (_client(_resp("無")), _client(exc=RuntimeError("boom")),
                 _client(_resp(TRACKS_TEXT, chunks=0)), _client(_resp("沒有編號的一段話"))):
        assert await _tracks(store, free=free) == []
    assert store.get(ALBUM_KEY) is None


@pytest.mark.asyncio
async def test_tracklist_paid_recorded_with_tour_caller(store):
    guard = _guard(allow=True)
    await _tracks(store, free=_client(exc=RuntimeError("429")),
                  paid=_client(_resp(TRACKS_TEXT)), guard=guard)
    assert guard.record.call_args.kwargs["caller"] == "album_tour_tracklist"


# ── resolved_matches_track（YouTube 髒標題 / 配錯歌守門，2026-09-29 真機：「告五人 過場」
#     搜出〈在這座城市遺失了你〉MV）──────────────────────────────────────────────

@pytest.mark.parametrize("title,track", [
    ("周杰倫 Jay Chou【雙截棍 Nunchucks】Official MV", "雙截棍"),
    ("周杰伦 双截棍 (官方MV)", "雙截棍"),                       # 簡體標題 vs 繁體曲名
    ("告五人 Accusefive [ WEWE ] Official Music Video", "WEWE"),
    ("告五人 Accusefive [ wewe ]", "WEWE"),                     # 大小寫
    ("老王樂隊｜我還年輕 我還年輕 Official Music Video", "我還年輕我還年輕"),  # 空白差異
    ("老王樂隊 - 我還年輕，我還年輕", "我還年輕 我還年輕"),       # 標點差異
])
def test_resolved_matches_track_true(title, track):
    from audiophile_fetcher import resolved_matches_track
    assert resolved_matches_track({"title": title}, track) is True


def test_resolved_matches_track_uses_track_metadata_too():
    from audiophile_fetcher import resolved_matches_track
    assert resolved_matches_track({"title": "Topic upload 123", "track": "簡單愛"}, "簡單愛") is True


@pytest.mark.parametrize("info,track", [
    ({"title": "告五人 Accusefive [ 在這座城市遺失了你 Where I Lost Us ] '遺失的情人節'版MV"}, "過場"),
    ({"title": "周杰倫 Jay Chou【簡單愛 Simple Love】Official MV"}, "雙截棍"),
    ({}, "雙截棍"),
])
def test_resolved_matches_track_false(info, track):
    from audiophile_fetcher import resolved_matches_track
    assert resolved_matches_track(info, track) is False


# ── _fetch_guide（無保底台詞版本，song_guide_for_dj 的共用核心）───────────────

@pytest.mark.asyncio
async def test_fetch_guide_cache_hit_zero_api(store):
    from audiophile_fetcher import _fetch_guide

    store.set(KEY, {"audiophile_guide": "快取裡的導聆台詞"})
    free = _client(_resp("不該被呼叫"))
    out = await _fetch_guide("周杰倫 - 雙截棍", free_client=free, paid_client=None,
                             guard=_guard(), store=store)
    assert out == "快取裡的導聆台詞"
    free.aio.models.generate_content.assert_not_awaited()


@pytest.mark.asyncio
async def test_fetch_guide_success_writes_cache(store):
    from audiophile_fetcher import _fetch_guide

    free = _client(_resp(GUIDE))
    out = await _fetch_guide("周杰倫 - 雙截棍", free_client=free, paid_client=None,
                             guard=_guard(), store=store)
    assert out == GUIDE
    assert store.get(KEY)["audiophile_guide"] == GUIDE


@pytest.mark.asyncio
async def test_fetch_guide_failure_returns_none_and_does_not_cache(store):
    from audiophile_fetcher import _fetch_guide

    free = _client(exc=RuntimeError("boom"))
    out = await _fetch_guide("周杰倫 - 雙截棍", free_client=free, paid_client=None,
                             guard=_guard(), store=store)
    assert out is None
    assert store.get(KEY) is None


# ── AutoGuideBudget（DJ 串場自動觸發的免費層預算）─────────────────────────────

def test_auto_guide_budget_min_interval():
    from audiophile_fetcher import AUTO_MIN_INTERVAL_S, AutoGuideBudget

    b = AutoGuideBudget()
    assert b.allow("a", 1000.0) is True
    assert b.allow("b", 1000.0 + AUTO_MIN_INTERVAL_S - 1) is False
    assert b.allow("c", 1000.0 + AUTO_MIN_INTERVAL_S) is True


def test_auto_guide_budget_daily_cap_and_reset_next_day():
    from audiophile_fetcher import AUTO_DAILY_CAP, AUTO_MIN_INTERVAL_S, AutoGuideBudget

    assert AUTO_DAILY_CAP == 18  # 免費一天 20 次，只留 2 次給「馬文幫我查」（9/29 使用者定：很少用）
    b = AutoGuideBudget()
    base = time.mktime(time.strptime("2026-09-29 08:00:00", "%Y-%m-%d %H:%M:%S"))
    now = base
    for i in range(AUTO_DAILY_CAP):
        assert b.allow(f"k{i}", now) is True
        now += AUTO_MIN_INTERVAL_S
    assert b.allow("k_over", now) is False  # 第 11 次

    next_day = base + 86400
    assert b.allow("k_next_day", next_day) is True  # 換日恢復


def test_auto_guide_budget_fail_cooldown_blocks_same_key():
    from audiophile_fetcher import AUTO_MIN_INTERVAL_S, AutoGuideBudget

    b = AutoGuideBudget()
    assert b.allow("x", 1000.0) is True
    b.record("x", 1000.0, ok=False)
    assert b.allow("x", 1000.0 + AUTO_MIN_INTERVAL_S) is False  # 同 key 冷卻中
    assert b.allow("y", 1000.0 + AUTO_MIN_INTERVAL_S) is True   # 別的 key 不受影響


def test_auto_guide_budget_success_clears_fail_cooldown():
    from audiophile_fetcher import AUTO_MIN_INTERVAL_S, AutoGuideBudget

    b = AutoGuideBudget()
    b.allow("x", 1000.0)
    b.record("x", 1000.0, ok=False)
    b.allow("x", 1000.0 + AUTO_MIN_INTERVAL_S)  # 仍會被擋（示範冷卻存在），但下面驗證清除
    b.record("x", 1000.0 + AUTO_MIN_INTERVAL_S, ok=True)
    assert b.allow("x", 1000.0 + AUTO_MIN_INTERVAL_S * 2) is True


# ── resolve_canon（iTunes 正規化）────────────────────────────────────────────

def _itunes_meta(title="雙截棍", artist="周杰倫", album="范特西", year=2001):
    return {"title": title, "artist": artist, "album": album, "year": year}


@pytest.mark.asyncio
async def test_resolve_canon_cache_hit_zero_query(store):
    from audiophile_fetcher import resolve_canon

    store.set("canon::vid1", {"artist": "周杰倫", "title": "雙截棍", "album": "范特西",
                              "year": 2001, "source": "itunes", "ts": 1.0})

    async def _must_not_call(*a, **kw):
        raise AssertionError("快取命中不該查 iTunes")

    out = await resolve_canon(store, "vid1", "雙截棍", "周杰倫", fetch=_must_not_call)
    assert out["title"] == "雙截棍"


@pytest.mark.asyncio
async def test_resolve_canon_writes_canon_with_year_and_source(store):
    from audiophile_fetcher import resolve_canon

    async def _fetch(term, **kw):
        return {"results": [{"trackName": "雙截棍", "artistName": "周杰倫",
                             "collectionName": "范特西", "releaseDate": "2001-09-14T00:00:00Z",
                             "artworkUrl100": "https://x/100x100bb.jpg"}]}

    out = await resolve_canon(store, "vid1", "雙截棍", "周杰倫", fetch=_fetch)
    assert out == {"artist": "周杰倫", "title": "雙截棍", "album": "范特西",
                   "year": 2001, "source": "itunes", "ts": out["ts"]}
    assert store.get("canon::vid1") == out


@pytest.mark.asyncio
async def test_resolve_canon_single_suffix_album_becomes_none(store):
    from audiophile_fetcher import resolve_canon

    async def _fetch(term, **kw):
        return {"results": [{"trackName": "晴天", "artistName": "周杰倫",
                             "collectionName": "晴天 - Single", "releaseDate": "2003-01-01T00:00:00Z",
                             "artworkUrl100": "https://x/100x100bb.jpg"}]}

    out = await resolve_canon(store, "vid2", "晴天", "周杰倫", fetch=_fetch)
    assert out["album"] is None


@pytest.mark.asyncio
async def test_resolve_canon_title_pinyin_mismatch_returns_none_not_written(store):
    from audiophile_fetcher import resolve_canon

    async def _fetch(term, **kw):
        return {"results": [{"trackName": "完全不同的歌", "artistName": "周杰倫",
                             "collectionName": "某專輯", "releaseDate": "2001-01-01T00:00:00Z",
                             "artworkUrl100": "https://x/100x100bb.jpg"}]}

    out = await resolve_canon(store, "vid3", "雙截棍", "周杰倫", fetch=_fetch)
    assert out is None
    assert store.get("canon::vid3") is None


@pytest.mark.asyncio
async def test_resolve_canon_meta_none_returns_none(store):
    from audiophile_fetcher import resolve_canon

    async def _fetch(term, **kw):
        return {"results": []}

    out = await resolve_canon(store, "vid4", "雙截棍", "周杰倫", fetch=_fetch)
    assert out is None


@pytest.mark.asyncio
async def test_resolve_canon_no_video_id_returns_none(store):
    from audiophile_fetcher import resolve_canon

    async def _must_not_call(*a, **kw):
        raise AssertionError("沒 video_id 不該查")

    out = await resolve_canon(store, "", "雙截棍", "周杰倫", fetch=_must_not_call)
    assert out is None


@pytest.mark.asyncio
async def test_resolve_canon_default_fetch_uses_country_tw(store, monkeypatch):
    from audiophile_fetcher import resolve_canon
    import itunes_cover

    captured = {}

    async def _fake_default_fetch(term, *, timeout_s=6.0, country=None):
        captured["country"] = country
        return {"results": [{"trackName": "雙截棍", "artistName": "周杰倫",
                             "collectionName": "范特西", "releaseDate": "2001-01-01T00:00:00Z",
                             "artworkUrl100": "https://x/100x100bb.jpg"}]}

    monkeypatch.setattr(itunes_cover, "_default_fetch", _fake_default_fetch)
    await resolve_canon(store, "vid5", "雙截棍", "周杰倫")
    assert captured["country"] == "TW"


# ── song_guide_for_dj ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_song_guide_for_dj_cache_hit_zero_calls(store):
    from audiophile_fetcher import AutoGuideBudget, song_guide_for_dj

    store.set(KEY, {"audiophile_guide": "快取裡的導聆台詞"})
    free = _client(_resp("不該被呼叫"))
    out = await song_guide_for_dj(
        "周杰倫 - 雙截棍", human=False, free_client=free, paid_client=None,
        guard=None, store=store, budget=AutoGuideBudget(), inflight={},
    )
    assert out == "快取裡的導聆台詞"
    free.aio.models.generate_content.assert_not_awaited()


@pytest.mark.asyncio
async def test_song_guide_for_dj_autopilot_uses_free_only_and_budget(store):
    from audiophile_fetcher import AutoGuideBudget, song_guide_for_dj

    free = _client(_resp(GUIDE))
    paid = _client(_resp("不該被呼叫"))
    out = await song_guide_for_dj(
        "周杰倫 - 雙截棍", human=False, free_client=free, paid_client=paid,
        guard=_guard(), store=store, budget=AutoGuideBudget(), inflight={},
    )
    assert out == GUIDE
    paid.aio.models.generate_content.assert_not_awaited()


@pytest.mark.asyncio
async def test_song_guide_for_dj_autopilot_free_fails_never_falls_to_paid(store):
    """免費層 429 時 autopilot 也不准掉到付費鏈（使用者 9/29 定：只有真人點歌可以付費）。"""
    from audiophile_fetcher import AutoGuideBudget, song_guide_for_dj

    free = _client(exc=RuntimeError("429 RESOURCE_EXHAUSTED"))
    paid = _client(_resp(GUIDE))
    out = await song_guide_for_dj(
        "周杰倫 - 雙截棍", human=False, free_client=free, paid_client=paid,
        guard=_guard(), store=store, budget=AutoGuideBudget(), inflight={},
    )
    assert out is None
    paid.aio.models.generate_content.assert_not_awaited()


@pytest.mark.asyncio
async def test_song_guide_for_dj_autopilot_budget_denied_returns_none_zero_calls(store):
    from audiophile_fetcher import AutoGuideBudget, song_guide_for_dj

    budget = AutoGuideBudget()
    budget._daily_count = 10**9  # 模擬已用盡
    budget._daily_date = time.localtime()[:3]
    free = _client(_resp("不該被呼叫"))
    out = await song_guide_for_dj(
        "周杰倫 - 雙截棍", human=False, free_client=free, paid_client=None,
        guard=None, store=store, budget=budget, inflight={},
    )
    assert out is None
    free.aio.models.generate_content.assert_not_awaited()


@pytest.mark.asyncio
async def test_song_guide_for_dj_human_uses_paid_client_and_bypasses_budget(store):
    from audiophile_fetcher import AutoGuideBudget, song_guide_for_dj

    budget = AutoGuideBudget()
    budget._daily_count = 10**9  # 用盡也不影響真人點歌
    budget._daily_date = time.localtime()[:3]
    free = _client(exc=RuntimeError("RESOURCE_EXHAUSTED"))
    paid = _client(_resp(GUIDE))
    guard = _guard(allow=True)
    out = await song_guide_for_dj(
        "周杰倫 - 雙截棍", human=True, free_client=free, paid_client=paid,
        guard=guard, store=store, budget=budget, inflight={},
    )
    assert out == GUIDE
    paid.aio.models.generate_content.assert_awaited_once()


@pytest.mark.asyncio
async def test_song_guide_for_dj_concurrent_same_label_single_call(store):
    from audiophile_fetcher import AutoGuideBudget, song_guide_for_dj

    free = _client(_resp(GUIDE))
    budget = AutoGuideBudget()
    inflight = {}
    results = await asyncio.gather(*[
        song_guide_for_dj("周杰倫 - 雙截棍", human=False, free_client=free, paid_client=None,
                          guard=None, store=store, budget=budget, inflight=inflight)
        for _ in range(3)
    ])
    assert results == [GUIDE, GUIDE, GUIDE]
    free.aio.models.generate_content.assert_awaited_once()


@pytest.mark.asyncio
async def test_song_guide_for_dj_timeout_returns_none_but_task_caches_after(store):
    from audiophile_fetcher import AutoGuideBudget, song_guide_for_dj

    async def _slow_generate(*a, **kw):
        await asyncio.sleep(0.05)
        return _resp(GUIDE)

    free = MagicMock()
    free.aio.models.generate_content = AsyncMock(side_effect=_slow_generate)
    inflight = {}
    out = await song_guide_for_dj(
        "周杰倫 - 雙截棍", human=False, free_client=free, paid_client=None,
        guard=None, store=store, budget=AutoGuideBudget(), inflight=inflight, wait_s=0.001,
    )
    assert out is None
    # task 仍在背景跑，等它完成後快取應該已寫好
    task = list(inflight.values())[0] if inflight else None
    if task is not None:
        await task
    assert store.get(KEY)["audiophile_guide"] == GUIDE


def _itunes(track, artist, album="某專輯"):
    async def _fetch(term, **kw):
        return {"results": [{"trackName": track, "artistName": artist, "collectionName": album,
                             "releaseDate": "2013-01-01T00:00:00Z",
                             "artworkUrl100": "https://x/100x100bb.jpg"}]}
    return _fetch


@pytest.mark.asyncio
async def test_resolve_canon_artist_mismatch_rejected(store):
    """9/29 真機：髒標題夾歌詞「…一點點靠近」→ iTunes 配成吳莫愁〈靠近〉，曲名互相包含會放行，
    歌手對不上才擋得住（說錯不如沒說）。"""
    from audiophile_fetcher import resolve_canon

    out = await resolve_canon(
        store, "vid9", "我的秘密『我們之間的距離每天一點點靠近』", "顏人中",
        artist_hay="我的秘密 - 颜人中『我们之间的距离每天一点点靠近』 LZ Music Channel",
        fetch=_itunes("靠近", "吳莫愁"),
    )
    assert out is None
    assert store.get("canon::vid9") is None


@pytest.mark.asyncio
async def test_resolve_canon_artist_found_in_hay_when_clean_artist_empty(store):
    from audiophile_fetcher import resolve_canon

    out = await resolve_canon(store, "vid10", "帶我去找夜生活", "",
                              artist_hay="帶我去找夜生活 告五人Accusefive",
                              fetch=_itunes("帶我去找夜生活", "告五人"))
    assert out["artist"] == "告五人"


@pytest.mark.asyncio
async def test_resolve_canon_multi_artist_any_part_matches(store):
    from audiophile_fetcher import resolve_canon

    out = await resolve_canon(store, "vid11", "清空", "王忻辰, 蘇星婕",
                              fetch=_itunes("清空", "蘇星婕 & 王忻辰"))
    assert out["artist"] == "蘇星婕 & 王忻辰"


@pytest.mark.asyncio
async def test_resolve_canon_tribute_act_rejected(store):
    """9/29 真機：Dr. Dre〈Still D.R.E.〉配到致敬團 Mixmaster Throwback。"""
    from audiophile_fetcher import resolve_canon

    out = await resolve_canon(store, "vid12", "Still D.R.E.", "Dr. Dre",
                              artist_hay="Dr. Dre - Still D.R.E. ft. Snoop Dogg Octava",
                              fetch=_itunes("Still D.R.E.", "Mixmaster Throwback"))
    assert out is None
