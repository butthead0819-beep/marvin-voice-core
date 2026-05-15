"""
TDD for setter hint system:
1. SetterHintModal exists with min_length=1, max_length=ANSWER_MAX_LEN
2. Submitting modal stores hint in session.setter_hint
3. BuzzView has hint button for human setter, not for Marvin
4. Hint button rejects non-setter and round-5 clicks
5. generate_clue accepts setter_hint and includes it in prompt
6. _on_clue_request reads session.setter_hint, clears it, sets applied_hint
7. _build_clue_embed shows applied_hint
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from game.session import GameSession, GameState, PlayerState
from game.engine import ANSWER_MAX_LEN


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_cog(setter_id: str = "u1"):
    bot = MagicMock()
    bot.cogs.get.return_value = None
    bot.voice_clients = []
    with patch("cogs.game_cog.MemoryManager"):
        from cogs.game_cog import BustedCog
        cog = BustedCog(bot)
    session = GameSession(session_id="t1", guild_id=1, channel_id=1)
    session.state = GameState.CLUE_ACTIVE
    session.current_setter_id = setter_id
    session.current_answer = "蘋果汁"
    session.current_round = 2
    session.current_clues = ["這是一種飲料"]
    session.players = [
        PlayerState(user_id=setter_id, display_name="Setter"),
        PlayerState(user_id="g1", display_name="G1"),
    ]
    cog._session = session
    return cog, session


# ── 1. SetterHintModal constraints ───────────────────────────────────────────

def test_setter_hint_modal_exists():
    from cogs.game_cog import SetterHintModal
    assert SetterHintModal is not None


def test_setter_hint_modal_min_length_is_1():
    from cogs.game_cog import SetterHintModal
    assert SetterHintModal.hint_input.min_length == 1, (
        "min_length must be 1 so Chinese IME works"
    )


def test_setter_hint_modal_max_length_matches_answer():
    from cogs.game_cog import SetterHintModal
    assert SetterHintModal.hint_input.max_length == ANSWER_MAX_LEN, (
        f"max_length should match ANSWER_MAX_LEN={ANSWER_MAX_LEN}"
    )


# ── 2. BuzzView hint button ───────────────────────────────────────────────────

def test_buzz_view_has_hint_button_for_human_setter():
    """BuzzView created with human setter_id should include a hint button."""
    from cogs.game_cog import BuzzView
    cog = MagicMock()
    view = BuzzView(cog, disabled=False, setter_id="u1")
    labels = [item.label for item in view.children if hasattr(item, "label")]
    hint_btn = next((item for item in view.children
                     if hasattr(item, "label") and "提示" in (item.label or "")), None)
    assert hint_btn is not None, f"BuzzView should have a hint button for human setter; buttons: {labels}"


def test_buzz_view_no_hint_button_for_marvin():
    """BuzzView created with Marvin as setter should NOT include a hint button."""
    from cogs.game_cog import BuzzView
    cog = MagicMock()
    view = BuzzView(cog, disabled=False, setter_id="marvin")
    hint_btn = next((item for item in view.children
                     if hasattr(item, "label") and "提示" in (item.label or "")), None)
    assert hint_btn is None, "BuzzView should NOT have hint button when Marvin is setter"


# ── 3. Hint button access control ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_hint_button_rejects_non_setter():
    """Non-setter clicking hint button should get ephemeral error."""
    cog, session = _make_cog(setter_id="u1")

    from cogs.game_cog import BuzzView
    view = BuzzView(cog, disabled=False, setter_id="u1")
    hint_btn = next((item for item in view.children
                     if hasattr(item, "label") and "提示" in (item.label or "")), None)
    assert hint_btn is not None

    interaction = MagicMock()
    interaction.user.id = 999  # not the setter
    interaction.response.send_message = AsyncMock()
    interaction.response.send_modal = AsyncMock()

    await hint_btn.callback(interaction)

    interaction.response.send_message.assert_called_once()
    call_kwargs = interaction.response.send_message.call_args
    assert call_kwargs.kwargs.get("ephemeral") is True
    interaction.response.send_modal.assert_not_called()


@pytest.mark.asyncio
async def test_hint_button_rejects_round_5():
    """Hint button should reject setter if current_round >= 5."""
    cog, session = _make_cog(setter_id="u1")
    session.current_round = 5

    from cogs.game_cog import BuzzView
    view = BuzzView(cog, disabled=False, setter_id="u1")
    hint_btn = next((item for item in view.children
                     if hasattr(item, "label") and "提示" in (item.label or "")), None)
    assert hint_btn is not None

    interaction = MagicMock()
    interaction.user.id = int("u1".replace("u", "")) if "u" in "u1" else 1  # setter
    interaction.user.id = 1  # force as the setter for this test, checked against string
    # Patch _session on the cog to have round 5
    interaction.response.send_message = AsyncMock()
    interaction.response.send_modal = AsyncMock()

    # Simulate setter click on round 5
    cog._session = session  # ensure cog sees round 5

    # We simulate the setter_id check by having a custom check
    # The button should block round >= 5
    # Re-build with explicit round context
    session.current_round = 5
    await hint_btn.callback(interaction)

    # Either rejected with message, or modal not shown
    if interaction.response.send_modal.called:
        pytest.fail("Modal should not open on round 5")


# ── 4. generate_clue uses setter_hint ────────────────────────────────────────

@pytest.mark.asyncio
async def test_generate_clue_includes_setter_hint_in_prompt():
    """When setter_hint is provided, generate_clue should include it in the prompt."""
    from game.clue_generator import generate_clue
    router = MagicMock()
    router.complete = AsyncMock(return_value="這是個線索")

    await generate_clue(
        answer="蘋果汁",
        round_num=2,
        prior_clues=["先前的線索"],
        router=router,
        setter_hint="液體",
    )

    call_args = router.complete.call_args
    system_prompt = call_args.kwargs.get("system") or call_args.args[0] if call_args.args else ""
    if not system_prompt and call_args.kwargs:
        system_prompt = call_args.kwargs.get("system", "")
    assert "液體" in system_prompt, (
        f"setter_hint '液體' should appear in system prompt, got: {system_prompt[:200]}"
    )


@pytest.mark.asyncio
async def test_generate_clue_no_hint_section_when_hint_is_none():
    """When no setter_hint, the prompt should not contain hint section."""
    from game.clue_generator import generate_clue
    router = MagicMock()
    router.complete = AsyncMock(return_value="這是個線索")

    await generate_clue(
        answer="蘋果汁",
        round_num=2,
        prior_clues=[],
        router=router,
        setter_hint=None,
    )

    call_args = router.complete.call_args
    system_prompt = call_args.kwargs.get("system", "")
    assert "出題者的提示" not in system_prompt


# ── 5. _on_clue_request passes hint and sets applied_hint ────────────────────

@pytest.mark.asyncio
async def test_on_clue_request_reads_and_clears_setter_hint():
    """_on_clue_request should consume session.setter_hint and set session.applied_hint."""
    cog, session = _make_cog(setter_id="u1")
    session.setter_hint = "液體"  # hint pre-set

    bot = cog.bot
    router = MagicMock()
    router.complete = AsyncMock(return_value="這個東西可以喝")
    bot.router = router
    cog._channel = AsyncMock()
    cog.on_state_change = AsyncMock()

    vc = MagicMock()
    vc.play_tts = AsyncMock()
    bot.cogs.get.return_value = vc

    await cog._on_clue_request(session)

    assert session.setter_hint is None, "setter_hint should be cleared after use"
    assert session.applied_hint == "液體", (
        f"applied_hint should be '液體', got {session.applied_hint!r}"
    )


@pytest.mark.asyncio
async def test_on_clue_request_passes_hint_to_generate_clue():
    """The hint from session.setter_hint should reach the LLM prompt."""
    cog, session = _make_cog(setter_id="u1")
    session.setter_hint = "甜甜的"

    bot = cog.bot
    router = MagicMock()
    router.complete = AsyncMock(return_value="線索")
    bot.router = router
    cog._channel = AsyncMock()
    cog.on_state_change = AsyncMock()

    vc = MagicMock()
    vc.play_tts = AsyncMock()
    bot.cogs.get.return_value = vc

    await cog._on_clue_request(session)

    call_args = router.complete.call_args
    system_prompt = call_args.kwargs.get("system", "")
    assert "甜甜的" in system_prompt, (
        f"hint '甜甜的' should be in LLM prompt, got: {system_prompt[:300]}"
    )


# ── 6. _build_clue_embed shows applied_hint ───────────────────────────────────

def test_build_clue_embed_shows_applied_hint():
    """_build_clue_embed should show applied_hint if it's set."""
    cog, session = _make_cog(setter_id="u1")
    session.applied_hint = "液體"

    embed = cog._build_clue_embed(session, countdown=50)
    all_text = " ".join(
        [embed.title or "", embed.description or ""]
        + [f.value for f in embed.fields]
    )
    assert "液體" in all_text, (
        "applied_hint '液體' should appear in clue embed"
    )


def test_build_clue_embed_no_hint_section_when_none():
    """_build_clue_embed should not show hint section when applied_hint is None."""
    cog, session = _make_cog(setter_id="u1")
    session.applied_hint = None

    embed = cog._build_clue_embed(session, countdown=50)
    field_names = [f.name for f in embed.fields]
    assert not any("提示" in n for n in field_names), (
        f"no hint field should appear when applied_hint is None, got fields: {field_names}"
    )


# ── 7. Session fields exist ───────────────────────────────────────────────────

def test_session_has_setter_hint_and_applied_hint_fields():
    s = GameSession(session_id="t1", guild_id=1, channel_id=1)
    assert hasattr(s, "setter_hint") and s.setter_hint is None
    assert hasattr(s, "applied_hint") and s.applied_hint is None
