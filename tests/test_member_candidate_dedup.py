"""TDD — autopilot 候選改成「per-member 候選池 → 跨使用者唯一歸屬」。

Bug：同一首歌會被分別指定點播給不同使用者。根因＝原 build_recommendation_pool
產 shared pool，group_resonance / long_tail 兩條 lane 的歌沒有擁有者，最後全標給
當輪 spotlight；跨輪去重視窗過期後，同一首團體歌下一輪輪到別人又被指定一次。

修法（純函式、無 Discord/LLM）：
  - build_member_pools：對每個在場者各自產候選池 dict[member -> [Candidate]]
  - assign_unique_owners：每首歌只歸一人；同首被多人搶＝高分候選時 round-robin 平手代表
"""
from __future__ import annotations

from music_recommender import (
    assign_unique_owners,
    build_member_pools,
    normalize_title,
)

NOW = 1_700_000_000.0
DAY = 86400.0


def _song(title, *, requesters=None, connections=None, last_play_age_days=0.0, uploader="orig"):
    ts = NOW - last_play_age_days * DAY
    return {
        "title": title,
        "uploader": uploader,
        "url": f"http://x/{title}",
        "total_plays": sum((requesters or {}).values()) or 1,
        "plays": [{"by": b, "ts": ts} for b in (requesters or {"a": 1})],
        "requesters": dict(requesters or {}),
        "connections": list(connections or []),
    }


# ── build_member_pools：每人各自的候選 ─────────────────────────────────────────

def test_member_pool_contains_only_each_members_songs():
    songs = {
        "x": _song("阿明的歌", requesters={"阿明": 3}),
        "y": _song("小華的歌", requesters={"小華": 4}),
    }
    pools = build_member_pools(
        members=["阿明", "小華"], songs=songs, exclude_titles=[], now=NOW,
    )
    amin = {normalize_title(c.anchor_title) for c in pools["阿明"]}
    ahua = {normalize_title(c.anchor_title) for c in pools["小華"]}
    assert normalize_title("阿明的歌") in amin
    assert normalize_title("小華的歌") not in amin
    assert normalize_title("小華的歌") in ahua
    assert normalize_title("阿明的歌") not in ahua


def test_member_pool_sets_target_member_to_owner():
    songs = {"x": _song("阿明的歌", requesters={"阿明": 3})}
    pools = build_member_pools(
        members=["阿明", "小華"], songs=songs, exclude_titles=[], now=NOW,
    )
    assert pools["阿明"], "阿明 應有候選"
    assert all(c.target_member == "阿明" for c in pools["阿明"])


def test_member_pool_excludes_by_normalized_title():
    songs = {"x": _song("阿明的歌", requesters={"阿明": 3})}
    pools = build_member_pools(
        members=["阿明"], songs=songs,
        exclude_titles=["阿明的歌 (cover)"], now=NOW,
    )
    assert pools["阿明"] == []


def test_member_pool_empty_for_member_with_no_history():
    songs = {"x": _song("阿明的歌", requesters={"阿明": 3})}
    pools = build_member_pools(
        members=["阿明", "路人"], songs=songs, exclude_titles=[], now=NOW,
    )
    assert pools["路人"] == []


# ── assign_unique_owners：跨使用者去重（核心 bug 修復）─────────────────────────

def test_same_song_assigned_to_exactly_one_member():
    """同一首團體歌被 A、B 都列為候選 → 去重後只歸一人，絕不兩人各播一次。"""
    songs = {
        "g": _song("大家的歌", requesters={"A": 2, "B": 2}, connections=["A", "B"]),
    }
    pools = build_member_pools(
        members=["A", "B"], songs=songs, exclude_titles=[], now=NOW,
    )
    deduped = assign_unique_owners(pools, rotation_order=["A", "B"])
    owners = [m for m, cands in deduped.items()
              if any(normalize_title(c.anchor_title) == normalize_title("大家的歌") for c in cands)]
    assert owners == ["A"] or owners == ["B"], f"應恰好一人擁有，實得 {owners}"
    assert len(owners) == 1


def test_no_song_appears_in_two_member_pools():
    songs = {
        "g1": _song("共鳴一", requesters={"A": 2, "B": 2}, connections=["A", "B"]),
        "g2": _song("共鳴二", requesters={"A": 1, "B": 3}, connections=["A", "B"]),
        "solo": _song("A獨享", requesters={"A": 5}),
    }
    pools = build_member_pools(
        members=["A", "B"], songs=songs, exclude_titles=[], now=NOW,
    )
    deduped = assign_unique_owners(pools, rotation_order=["A", "B"])
    seen: dict[str, str] = {}
    for m, cands in deduped.items():
        for c in cands:
            nt = normalize_title(c.anchor_title)
            assert nt not in seen, f"{nt} 同時出現在 {seen.get(nt)} 與 {m}"
            seen[nt] = m


def test_round_robin_splits_contested_songs_evenly():
    """兩首歌都是 A、B 的同分高分候選 → round-robin 各分一首（平均代表，不單人通吃）。"""
    songs = {
        "g1": _song("共鳴一", requesters={"A": 2, "B": 2}, connections=["A", "B"]),
        "g2": _song("共鳴二", requesters={"A": 2, "B": 2}, connections=["A", "B"]),
    }
    pools = build_member_pools(
        members=["A", "B"], songs=songs, exclude_titles=[], now=NOW,
    )
    deduped = assign_unique_owners(pools, rotation_order=["A", "B"])
    assert len(deduped["A"]) == 1
    assert len(deduped["B"]) == 1


def test_uncontested_song_stays_with_its_only_candidate():
    songs = {"solo": _song("A獨享", requesters={"A": 5})}
    pools = build_member_pools(
        members=["A", "B"], songs=songs, exclude_titles=[], now=NOW,
    )
    deduped = assign_unique_owners(pools, rotation_order=["A", "B"])
    assert any(normalize_title(c.anchor_title) == normalize_title("A獨享") for c in deduped["A"])
    assert deduped["B"] == []
