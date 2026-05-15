"""
TDD for memory-based theme candidate selection.

Human setter: candidates drawn from setter's emotional_highlights + behavioral_patterns.
Marvin setter (setter_display_name=None): drawn from all cached players' highlights.
Fallback to CONCRETE_OBJECTS when memory has no matching keywords.
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from game.suki_topic_picker import pick_theme_candidates, CONCRETE_OBJECTS


def _mock_mem(cache: dict) -> MagicMock:
    mem = MagicMock()
    mem._cache = cache
    mem.get_proactive_topics.return_value = []
    return mem


def _player_with_highlight(moment: str, valence: str = "warm") -> dict:
    return {
        "emotional_highlights": [{"moment": moment, "valence": valence, "timestamp": 0.0}],
        "behavioral_patterns": {},
    }


def _player_with_pattern(key: str, value: str) -> dict:
    return {
        "emotional_highlights": [],
        "behavioral_patterns": {key: value},
    }


# ── 1. Human setter uses own highlights ───────────────────────────────────────

def test_human_setter_theme_from_highlight_keyword():
    """If setter's highlight mentions a concrete keyword, it appears in candidates."""
    mem = _mock_mem({"Jack": _player_with_highlight("Jack 很喜歡彈吉他，聊了很久")})
    for _ in range(10):
        candidates = pick_theme_candidates(mem, setter_display_name="Jack", n=3)
        assert "吉他" in candidates, (
            f"'吉他' from highlight should be in candidates, got {candidates}"
        )


def test_human_setter_theme_from_behavioral_pattern():
    """If setter's behavioral_pattern value contains a keyword, it appears in candidates."""
    mem = _mock_mem({"Alice": _player_with_pattern("興趣", "喜歡玩搖桿打電動")})
    for _ in range(10):
        candidates = pick_theme_candidates(mem, setter_display_name="Alice", n=3)
        assert "搖桿" in candidates, (
            f"'搖桿' from behavioral_pattern should be in candidates, got {candidates}"
        )


def test_human_setter_only_sees_own_memory():
    """Human setter should NOT see other players' highlights."""
    mem = _mock_mem({
        "Jack": _player_with_highlight("Jack 提到了鋼琴"),
        "Alice": _player_with_highlight("Alice 討論過耳機"),
    })
    # Jack is setter — should get 鋼琴, not necessarily 耳機 (Alice's)
    for _ in range(10):
        candidates = pick_theme_candidates(mem, setter_display_name="Jack", n=3)
        assert "鋼琴" in candidates, (
            f"setter Jack should see '鋼琴' from own memory, got {candidates}"
        )


# ── 2. Marvin scans all players ───────────────────────────────────────────────

def test_marvin_setter_scans_all_players():
    """When setter_display_name=None (Marvin), candidates can come from any player's memory."""
    mem = _mock_mem({
        "Jack": _player_with_highlight("Jack 常常用電吉他"),
        "Alice": _player_with_highlight("Alice 喜歡看電影"),
    })
    found_electric = False
    found_movie = False
    for _ in range(30):
        candidates = pick_theme_candidates(mem, setter_display_name=None, n=3)
        if "電吉他" in candidates:
            found_electric = True
        if "電影" in candidates:
            found_movie = True
    assert found_electric or found_movie, (
        "Marvin should surface keywords from any player's memory"
    )


# ── 3. Fallback to CONCRETE_OBJECTS ───────────────────────────────────────────

def test_empty_memory_falls_back_to_concrete_objects():
    """With no cache at all, all candidates come from CONCRETE_OBJECTS."""
    mem = _mock_mem({})
    for _ in range(20):
        candidates = pick_theme_candidates(mem, setter_display_name="Jack", n=3)
        for c in candidates:
            assert c in CONCRETE_OBJECTS, (
                f"fallback candidate {c!r} not in CONCRETE_OBJECTS"
            )


def test_partial_memory_fills_rest_from_concrete():
    """Only 1 keyword in memory → other 2 slots filled from CONCRETE_OBJECTS."""
    mem = _mock_mem({"Jack": _player_with_highlight("Jack 喜歡彈吉他")})
    candidates = pick_theme_candidates(mem, setter_display_name="Jack", n=3)
    assert len(candidates) == 3
    assert "吉他" in candidates
    # the other 2 should be from CONCRETE_OBJECTS
    others = [c for c in candidates if c != "吉他"]
    for c in others:
        assert c in CONCRETE_OBJECTS, f"fill-in {c!r} should be from CONCRETE_OBJECTS"


# ── 4. Always returns n items ─────────────────────────────────────────────────

def test_always_returns_exactly_n_items():
    """Returns exactly n items regardless of memory state."""
    mem = _mock_mem({})
    for n in (1, 2, 3, 5):
        assert len(pick_theme_candidates(mem, n=n)) == n


def test_returns_n_with_rich_memory():
    """Returns exactly n even when memory has many matches."""
    cache = {
        "Jack": {
            "emotional_highlights": [
                {"moment": f"Jack 提到了{kw}", "valence": "warm", "timestamp": 0.0}
                for kw in ("吉他", "鋼琴", "耳機", "電吉他", "搖桿")
            ],
            "behavioral_patterns": {},
        }
    }
    mem = _mock_mem(cache)
    candidates = pick_theme_candidates(mem, setter_display_name="Jack", n=3)
    assert len(candidates) == 3


def test_no_duplicate_candidates():
    """Returned candidates must be distinct."""
    cache = {
        "Jack": {
            "emotional_highlights": [
                {"moment": "Jack 很喜歡吉他", "valence": "warm", "timestamp": 0.0},
                {"moment": "Jack 又說到吉他了", "valence": "warm", "timestamp": 1.0},
            ],
            "behavioral_patterns": {},
        }
    }
    mem = _mock_mem(cache)
    for _ in range(10):
        candidates = pick_theme_candidates(mem, setter_display_name="Jack", n=3)
        assert len(candidates) == len(set(candidates)), f"duplicates found: {candidates}"
