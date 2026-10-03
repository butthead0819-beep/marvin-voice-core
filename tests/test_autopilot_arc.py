"""T1 一組三首（懷舊 → 過門 → 新歌）的純函式與接線守門。"""
from __future__ import annotations

import inspect

from music_recommender import (
    Candidate,
    arc_nostalgia_candidates,
    assemble_arc,
    filter_unfamiliar,
    song_video_id_for_title,
)

NOW = 1_700_000_000.0
DAY = 86400.0
URL_A = "https://www.youtube.com/watch?v=VID00000001"
URL_B = "https://www.youtube.com/watch?v=VID00000002"
URL_NEW = "https://www.youtube.com/watch?v=VID09999999"


def _song(title: str, url: str, days_ago: float | None, uploader: str = "歌手") -> dict:
    plays = [] if days_ago is None else [{"ts": NOW - days_ago * DAY, "by": "A"}]
    return {
        "title": title,
        "uploader": uploader,
        "webpage_url": url,
        "plays": plays,
        "requesters": {"A": 1} if plays else {},
    }


def _cand(title: str, *, lane: str = "long_tail", mode: str = "direct",
          target: str | None = "A", direct_url: str = "", state_reason: str = "") -> Candidate:
    return Candidate(
        anchor_title=title, anchor_artist="歌手", lane=lane, mode=mode,
        target_member=target, score=10.0, direct_url=direct_url, state_reason=state_reason,
    )


# ── 1. nostalgia：>30 天才留、嚴格大於、改成 direct/long_tail ───────────────────
def test_nostalgia_keeps_only_songs_older_than_30_days_and_forces_direct():
    songs = {
        "u_old": _song("舊歌A", URL_A, days_ago=45),
        "u_new": _song("新歌B", URL_B, days_ago=10),
        "u_edge": _song("剛好C", URL_NEW, days_ago=30),
    }
    pool = [
        _cand("舊歌A", lane="spotlight", mode="cover"),
        _cand("新歌B"),
        _cand("剛好C"),
    ]
    out = arc_nostalgia_candidates(pool, songs, now=NOW)
    assert [c.anchor_title for c in out] == ["舊歌A"]
    c = out[0]
    assert c.lane == "long_tail"
    assert c.mode == "direct"
    assert c.arc_role == "nostalgia"
    assert c.target_member == "A"


# ── 2. nostalgia：同歌多上傳取最大 ts ─────────────────────────────────────────
def test_nostalgia_uses_latest_play_across_uploads_of_same_normalized_title():
    songs = {
        "u1": _song("同歌", URL_A, days_ago=60),
        "u2": _song("同歌 (acoustic)", URL_B, days_ago=3),
    }
    out = arc_nostalgia_candidates([_cand("同歌")], songs, now=NOW)
    assert out == []


# ── 3. nostalgia：pool 查不到 song → 丟 ───────────────────────────────────────
def test_nostalgia_drops_candidates_without_matching_song():
    songs = {"u1": _song("別首", URL_A, days_ago=60)}
    out = arc_nostalgia_candidates([_cand("查無此歌")], songs, now=NOW)
    assert out == []


# ── 4. song_video_id_for_title ───────────────────────────────────────────────
def test_song_video_id_for_title_hit_and_miss():
    songs = {"u1": _song("晴天", URL_A, days_ago=1)}
    assert song_video_id_for_title(songs, "晴天 (cover)") == "VID00000001"
    assert song_video_id_for_title(songs, "不存在的歌") == ""


# ── 5. filter_unfamiliar：歌名或 videoId 命中就丟，保持順序 ─────────────────────
def test_filter_unfamiliar_drops_by_title_or_video_id_and_keeps_order():
    songs = {
        "u1": _song("已聽歌", URL_A, days_ago=1),
        "u2": _song("另一首", URL_B, days_ago=1),
    }
    cands = [
        _cand("已聽歌"),
        _cand("不同名", direct_url=URL_B),
        _cand("全新"),
        _cand("全新二", direct_url=URL_NEW),
    ]
    out = filter_unfamiliar(cands, songs)
    assert [c.anchor_title for c in out] == ["全新", "全新二"]


# ── 6. assemble_arc：順序與 arc_role ─────────────────────────────────────────
def test_assemble_arc_orders_segments_and_sets_roles():
    out = assemble_arc(
        [_cand("n1", lane="long_tail")],
        [_cand("b1", lane="discovery")],
        [_cand("d1", lane="discovery")],
        [_cand("f1", lane="spotlight", mode="direct")],
    )
    assert [c.anchor_title for c in out] == ["n1", "b1", "d1", "f1"]
    assert [c.arc_role for c in out] == ["nostalgia", "bridge", "discovery", ""]


# ── 7. assemble_arc：跨段同名去重，先出現的留下 ───────────────────────────────
def test_assemble_arc_dedups_across_segments_keeping_first():
    out = assemble_arc(
        [],
        [_cand("共同", lane="discovery", direct_url=URL_NEW)],
        [],
        [_cand("共同", lane="spotlight", mode="direct")],
    )
    assert len(out) == 1
    assert out[0].arc_role == "bridge"
    assert out[0].direct_url == URL_NEW


# ── 8. assemble_arc：分段 demote，cover 在同段後面但仍在 bridge 前 ────────────
def test_assemble_arc_demotes_cover_within_segment_only():
    # looks_like_cover：標題含 "cover"（且無 official 標記）即判為 cover
    out = assemble_arc(
        [_cand("Cover X cover"), _cand("原曲甲")],
        [_cand("橋接")],
        [],
        [],
    )
    assert [c.anchor_title for c in out] == ["原曲甲", "Cover X cover", "橋接"]


# ── 9. assemble_arc：有 state_reason 的 fallback[0] 排第一並去重 ─────────────
def test_assemble_arc_puts_state_pick_first_as_nostalgia_and_dedups_fallback():
    out = assemble_arc(
        [_cand("懷舊甲")],
        [],
        [],
        [_cand("狀態歌", state_reason="感冒"), _cand("其他")],
    )
    assert [c.anchor_title for c in out] == ["狀態歌", "懷舊甲", "其他"]
    assert out[0].arc_role == "nostalgia"
    assert out[0].state_reason == "感冒"


# ── 10. 接線：_auto_recommend 必須走 arc 路徑 ─────────────────────────────────
def test_auto_recommend_wires_arc_assembly_and_roles():
    from cogs.music_cog_story_arc import MusicStoryArcMixin

    src = inspect.getsource(MusicStoryArcMixin._auto_recommend)
    assert "_assemble_arc_candidates(" in src
    assert "cand.arc_role in _filled_roles" in src
    assert 'cand.arc_role == "bridge"' in src
    assert "info['_arc_role']" in src


# ── 11. 行為：_auto_recommend 依角色各收一首，順序 懷舊→過門→新歌 ─────────────
import pytest  # noqa: E402

from cogs.music_cog_story_arc import MusicStoryArcMixin  # noqa: E402


class _FakeMM:
    def __init__(self, songs):
        self._songs = songs

    def all_songs(self):
        return self._songs

    def get_recent_recommendation_titles(self):
        return []

    def get_skipped_titles(self, members):
        return []

    def get_skipped_video_ids(self):
        return set()

    def get_recently_played_video_ids(self, ttl):
        return set()

    def get_recently_played_titles(self, ttl):
        return []

    def add_recent_recommendation(self, title):
        pass

    def is_requester(self, info, who):
        return False


class _FakeCog(MusicStoryArcMixin):
    _round_size = 3
    _PLAYED_EXCLUDE_TTL_S = 7 * 86400
    _T3_PLAYED_EXCLUDE_TTL_S = 86400

    def __init__(self, songs, radio, fresh):
        self.bot = type("B", (), {"music_memory": _FakeMM(songs),
                                  "router": type("R", (), {"memory": None})()})()
        self.stream_history = []
        self.stream_queue = []
        self._mood_sensor = None
        self._cover_blacklist = object()
        self._recommend_spotlight_idx = -1
        self._prefetch_cache = {}
        self._radio, self._fresh = radio, fresh
        self.radio_seeds = []

    def _vc(self):
        return None

    def _current_bpm_filter(self):
        return None

    def _load_taste_fingerprint(self):
        return {}

    async def _maybe_state_pick(self, spotlight, members, cands):
        return cands

    async def _t2_radio_for_seed(self, seed, exclude):
        self.radio_seeds.append(seed)
        return self._radio

    async def _t4_fresh_discovery(self, members, spotlight, exclude):
        return self._fresh

    async def _resolve_yt_query(self, query):
        title = query.rsplit("|", 1)[-1] if query.startswith("http") else query.split(" ", 1)[-1]
        return {"title": title, "url": f"stream:{title}", "webpage_url": "", "duration": 200}

    def _check_song_duplicate(self, **kw):
        return False

    def _attribution_with_suki(self, mm, info, spotlight):
        return f"Marvin推薦（為{spotlight}）"

    def _compute_recommend_explanation(self, mm, cand):
        return None

    def _recommend_blurb(self, *a, **kw):
        return ""

    def _republish_queue_snapshot(self):
        pass


@pytest.mark.asyncio
async def test_auto_recommend_enqueues_one_song_per_arc_role_in_order(monkeypatch):
    import cogs.music_cog_story_arc as mod
    import taste_fingerprint
    import track_quality

    async def _ok(*a, **kw):
        return True, ""
    monkeypatch.setattr(track_quality, "assess_track_quality", _ok)
    monkeypatch.setattr(taste_fingerprint, "explore_matches_floor", lambda t, fp: True)
    monkeypatch.setattr(mod, "append_recommendation", lambda rec: None)

    songs = {URL_A: {"title": "老歌甲", "uploader": "歌手", "webpage_url": URL_A,
                     "plays": [{"ts": NOW - 45 * DAY, "by": "A"}], "requesters": {"A": 3},
                     "connections": []},
             URL_B: {"title": "老歌乙", "uploader": "歌手", "webpage_url": URL_B,
                     "plays": [{"ts": NOW - 40 * DAY, "by": "A"}], "requesters": {"A": 2},
                     "connections": []}}
    radio = [{"title": f"過門{i}", "artist": "X", "url": f"http://r/|過門{i}"} for i in range(3)]
    fresh = [Candidate(anchor_title=f"新歌{i}", anchor_artist="Y", lane="discovery", mode="direct",
                       target_member=None, score=0.0, direct_url=f"http://f/|新歌{i}") for i in range(3)]
    monkeypatch.setattr(mod.time, "time", lambda: NOW)
    cog = _FakeCog(songs, radio, fresh)

    await cog._auto_recommend("A")

    roles = [i["_arc_role"] for i in cog.stream_queue]
    assert roles == ["nostalgia", "bridge", "discovery"]
    assert cog.stream_queue[0]["title"] in ("老歌甲", "老歌乙")
    assert cog.radio_seeds and cog.radio_seeds[0] in ("VID00000001", "VID00000002")
