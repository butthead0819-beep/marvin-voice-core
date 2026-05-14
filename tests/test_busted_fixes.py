"""
Tests for 6 Busted game fixes:
1. Clue TTS uses already_in_channel=False (interrupt guard bypass)
2. Theme candidates are concrete physical objects only
3. Timing constants multiplied ×5
4. Selected theme shown in subsequent embeds
5. SetAnswerModal min_length=1 (Chinese IME fix)
6. BUZZ_COOLDOWN_SECONDS = 10.0
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from game.session import GameSession


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_bot():
    bot = MagicMock()
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    return bot


def _make_cog(bot=None):
    if bot is None:
        bot = _make_bot()
    with patch("cogs.game_cog.MemoryManager"):
        from cogs.game_cog import BustedCog
        return BustedCog(bot)


def _session_with_theme(theme: str = "吉他") -> GameSession:
    s = GameSession(session_id="t1", guild_id=1, channel_id=1)
    s.current_theme = theme
    s.current_answer = "吉他"
    s.current_round = 2
    s.current_clues = ["這是一種樂器"]
    return s


# ── Fix 1: TTS interrupt guard bypass ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_clue_tts_uses_already_in_channel_false():
    """Clue TTS must pass already_in_channel=False so the interrupt guard doesn't fire."""
    bot = _make_bot()
    vc = MagicMock()
    vc.play_tts = AsyncMock()
    bot.cogs.get.return_value = vc

    cog = _make_cog(bot)
    session = _session_with_theme()
    cog._session = session
    cog._channel = None
    cog.on_state_change = AsyncMock()

    router = MagicMock()
    router.complete = AsyncMock(return_value="有六條弦")
    bot.router = router

    await cog._on_clue_request(session)

    vc.play_tts.assert_called_once()
    call_kwargs = vc.play_tts.call_args
    already = call_kwargs.kwargs.get("already_in_channel", call_kwargs.args[1] if len(call_kwargs.args) > 1 else None)
    assert already is False, f"expected already_in_channel=False, got {already!r}"


# ── Fix 2: Concrete theme candidates ──────────────────────────────────────────

def test_theme_candidates_are_all_concrete():
    """pick_theme_candidates must return concrete physical objects from the curated list."""
    from game.suki_topic_picker import pick_theme_candidates, CONCRETE_OBJECTS

    mem = MagicMock()
    mem.get_proactive_topics.return_value = []
    mem._cache = {}

    for _ in range(20):
        candidates = pick_theme_candidates(mem, n=3)
        for c in candidates:
            assert c in CONCRETE_OBJECTS, (
                f"Theme {c!r} is not in CONCRETE_OBJECTS — themes must be concrete physical objects"
            )


def test_theme_candidates_returns_n_items():
    """pick_theme_candidates returns exactly n items even with empty memory."""
    from game.suki_topic_picker import pick_theme_candidates

    mem = MagicMock()
    mem.get_proactive_topics.return_value = []
    mem._cache = {}

    candidates = pick_theme_candidates(mem, n=3)
    assert len(candidates) == 3


def test_concrete_objects_list_has_enough_items():
    """CONCRETE_OBJECTS must have at least 20 items so themes don't repeat too quickly."""
    from game.suki_topic_picker import CONCRETE_OBJECTS
    assert len(CONCRETE_OBJECTS) >= 20, f"only {len(CONCRETE_OBJECTS)} items — add more"


# ── Fix 3: Timing ×5 ──────────────────────────────────────────────────────────

def test_buzz_lock_seconds_is_50():
    """BUZZ_LOCK_SECONDS must be 50 — extended for voice answer input."""
    from game.engine import BUZZ_LOCK_SECONDS
    assert BUZZ_LOCK_SECONDS == 50.0, f"expected 50.0, got {BUZZ_LOCK_SECONDS}"


def test_clue_deadline_is_50():
    """Clue deadline must be 50 seconds."""
    import inspect
    import cogs.game_cog as gc_mod
    src = inspect.getsource(gc_mod.BustedCog.on_state_change)
    assert "50.0" in src, "clue deadline should be 50.0 seconds in on_state_change"
    assert "75.0" not in src, "old 75.0 s deadline should be removed"


def test_setter_timeout_is_120():
    """Setter timeout task must sleep 120 seconds (user-requested change from 150)."""
    import inspect
    import cogs.game_cog as gc_mod
    src = inspect.getsource(gc_mod.BustedCog._setter_timeout_task)
    assert "120" in src, "setter timeout should be 120 seconds"


def test_auto_next_round_is_50():
    """Auto next round task must sleep 50 seconds (was 10, ×5)."""
    import inspect
    import cogs.game_cog as gc_mod
    src = inspect.getsource(gc_mod.BustedCog._auto_next_round)
    assert "50" in src, "auto next round should be 50 seconds"


# ── Fix 4: Theme shown in embeds ──────────────────────────────────────────────

def test_clue_embed_shows_selected_theme():
    """_build_clue_embed must display the session's current_theme."""
    cog = _make_cog()
    session = _session_with_theme("吉他")
    embed = cog._build_clue_embed(session, countdown=75)
    all_text = " ".join(
        [embed.title or "", embed.description or ""]
        + [f.value for f in embed.fields]
    )
    assert "吉他" in all_text, "clue embed should show the selected theme '吉他'"


def test_setter_input_embed_shows_selected_theme():
    """_build_setter_input_embed must display the session's current_theme."""
    cog = _make_cog()
    session = _session_with_theme("吉他")
    embed = cog._build_setter_input_embed(session)
    all_text = " ".join(
        [embed.title or "", embed.description or ""]
        + [f.value for f in embed.fields]
    )
    assert "吉他" in all_text, "setter input embed should show the selected theme '吉他'"


# ── Fix 5: Chinese IME — min_length=1 ─────────────────────────────────────────

def test_set_answer_modal_min_length_is_1():
    """SetAnswerModal.answer_input.min_length must be 1 to allow Chinese IME composition."""
    from cogs.game_cog import SetAnswerModal
    assert SetAnswerModal.answer_input.min_length == 1, (
        "min_length must be 1 so Chinese IME composition doesn't get rejected mid-input"
    )


# ── Fix 6: Buzz cooldown = 10 s ───────────────────────────────────────────────

def test_buzz_cooldown_seconds_is_10():
    """BUZZ_COOLDOWN_SECONDS must be 10 seconds."""
    from game.engine import BUZZ_COOLDOWN_SECONDS
    assert BUZZ_COOLDOWN_SECONDS == 10.0, f"expected 10.0, got {BUZZ_COOLDOWN_SECONDS}"
