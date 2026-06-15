"""TDD: 口味指紋（deterministic）+ 漂移偵測（2026-06-15）。

從 music_memory 真人點播統計出群組/個人口味摘要，供每週 review 觀測 + 未來
「錨定式驚喜」當地板。純統計、無 IO/無 LLM、可測。
"""
from __future__ import annotations

import pytest

from taste_fingerprint import (
    artist_of,
    classify_language,
    compute_taste_fingerprint,
    diff_fingerprints,
)


def _song(title, requesters):
    return {"title": title, "requesters": requesters}


# ── 純 helper ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("title,expected", [
    ("周杰倫 Jay Chou【晴天 Sunny Day】-Official MV", "周杰倫"),
    ("陶喆 David Tao – 找自己 Rain (官方完整版MV)", "陶喆"),
    ("Michael Jackson - Billie Jean (Official Video)", "Michael Jackson"),
    ("離歌", "離歌"),
    ("", ""),
])
def test_artist_of(title, expected):
    assert artist_of(title) == expected


@pytest.mark.parametrize("title,lang", [
    ("周杰倫【晴天】", "華語"),
    ("Michael Jackson - Billie Jean", "英文"),
    ("...", "其他"),
])
def test_classify_language(title, lang):
    assert classify_language(title) == lang


# ── 指紋計算 ─────────────────────────────────────────────────────────────────

def test_fingerprint_excludes_marvin_and_counts_humans():
    songs = {
        "u1": _song("周杰倫【晴天】", {"狗與露": 3, "Marvin推薦（為狗與露）": 5}),
        "u2": _song("陶喆 - 找自己", {"showay": 2}),
        "u3": _song("純自薦", {"Marvin推薦（為x）": 9}),  # 全自薦 → 不計
    }
    fp = compute_taste_fingerprint(songs, today="2026-06-15")
    assert fp["total_human_requests"] == 5          # 3 + 2，自薦不算
    assert fp["distinct_songs"] == 2                 # u3 全自薦不計
    assert ["周杰倫", 3] in fp["core_artists"]
    assert fp["updated"] == "2026-06-15"


def test_fingerprint_language_ratio():
    songs = {
        "a": _song("周杰倫【晴天】", {"u": 9}),
        "b": _song("Michael Jackson - Billie Jean", {"u": 1}),
    }
    fp = compute_taste_fingerprint(songs)
    assert fp["language"]["華語"] == 0.9
    assert fp["language"]["英文"] == 0.1


def test_fingerprint_per_user_core_artists():
    songs = {
        "a": _song("周杰倫【晴天】", {"狗與露": 4}),
        "b": _song("陶喆 - 找自己", {"showay": 2}),
    }
    fp = compute_taste_fingerprint(songs)
    assert fp["per_user"]["狗與露"]["requests"] == 4
    assert fp["per_user"]["狗與露"]["core_artists"][0] == ["周杰倫", 4]
    assert "showay" in fp["per_user"]


def test_fingerprint_empty_songs():
    fp = compute_taste_fingerprint({})
    assert fp["total_human_requests"] == 0
    assert fp["core_artists"] == []
    assert fp["language"] == {}


# ── 漂移偵測 ─────────────────────────────────────────────────────────────────

def test_diff_detects_new_and_dropped_artists():
    old = {"core_artists": [["周杰倫", 40], ["陶喆", 20]], "language": {"華語": 0.9}}
    new = {"core_artists": [["周杰倫", 41], ["五月天", 6]], "language": {"華語": 0.9}}
    drift = diff_fingerprints(old, new)
    assert drift["new_core_artists"] == ["五月天"]
    assert drift["dropped_core_artists"] == ["陶喆"]


def test_diff_language_shift_threshold():
    old = {"core_artists": [], "language": {"華語": 0.9, "英文": 0.1}}
    new = {"core_artists": [], "language": {"華語": 0.8, "英文": 0.2}}
    drift = diff_fingerprints(old, new)
    # 0.1 變化 >= 0.05 門檻 → 兩項都列
    assert drift["language_shift"]["華語"] == -0.1
    assert drift["language_shift"]["英文"] == pytest.approx(0.1)


def test_diff_ignores_tiny_language_shift():
    old = {"core_artists": [], "language": {"華語": 0.90}}
    new = {"core_artists": [], "language": {"華語": 0.92}}   # 0.02 < 0.05
    assert diff_fingerprints(old, new)["language_shift"] == {}
