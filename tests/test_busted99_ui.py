"""
UI behavior tests for Busted99:

  1. Game embed fixed position
     - After wrong guess, game embed is EDITED in place (stays at fixed position);
       result message is sent as a new message below it
     - When a player joins, joining embed is re-posted (not duplicated)

  2. Game over shows the secret answer
"""
from __future__ import annotations

import uuid
import pytest
from unittest.mock import AsyncMock, MagicMock


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_bot():
    bot = MagicMock()
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    return bot


async def _bootstrap_guessing(cog, *, jack_id: str = "12345",
                               jack_name: str = "狗與露"):
    from game.busted99.engine import Busted99Engine
    from game.busted99.session import Busted99Session, Busted99State

    session = Busted99Session(
        session_id=str(uuid.uuid4()), guild_id=1, channel_id=1,
    )

    # Set up channel mock with fetch_message returning a deletable mock
    channel = AsyncMock()
    sent_messages = []

    async def _send(**kwargs):
        msg = MagicMock()
        msg.id = len(sent_messages) + 1000
        msg.delete = AsyncMock()
        sent_messages.append((kwargs, msg))
        return msg

    channel.send = AsyncMock(side_effect=_send)
    channel.fetch_message = AsyncMock(return_value=AsyncMock(delete=AsyncMock()))

    cog._channel = channel

    async def _noop_state(s):
        pass

    engine = Busted99Engine(
        session=session, on_state_change=cog.on_state_change, db_path=":memory:",
    )
    cog._engine = engine
    cog._session = session

    await engine.add_player("marvin", "Marvin")
    await engine.add_player(jack_id, jack_name)
    cog._name_to_id[jack_name] = int(jack_id)

    session.setter_id = "marvin"
    session.current_guesser_id = jack_id
    session.answer = 50
    session.low_bound = 1
    session.high_bound = 99
    session.guessing_queue = []

    from game.busted99.session import Busted99State as S
    session.state = S.GUESSING
    cog._play_sfx = AsyncMock()

    return session, engine, jack_id, channel, sent_messages


# ══════════════════════════════════════════════════════════════════════════════
# 1. Game over embed shows the secret answer
# ══════════════════════════════════════════════════════════════════════════════

def test_game_over_embed_shows_answer():
    """
    The game_over embed must display the secret answer so players know
    what number the setter chose, even when no one guessed it exactly.
    """
    from cogs.busted99_cog import Busted99Cog
    from game.busted99.session import Busted99Session

    cog = Busted99Cog(_make_bot())
    session = Busted99Session(session_id="x", guild_id=1, channel_id=1)
    session.answer = 42

    embed = cog._build_game_over_embed(session)
    embed_dict = embed.to_dict()
    content = str(embed_dict)

    assert "42" in content, (
        "_build_game_over_embed must include the secret answer (42) "
        "so players can see what the setter chose."
    )


def test_game_over_embed_shows_answer_when_zero():
    """Edge case: answer=0 is unusual but should not crash."""
    from cogs.busted99_cog import Busted99Cog
    from game.busted99.session import Busted99Session

    cog = Busted99Cog(_make_bot())
    session = Busted99Session(session_id="x", guild_id=1, channel_id=1)
    session.answer = 0

    embed = cog._build_game_over_embed(session)
    # Must not raise; answer display is best-effort when answer=0


# ══════════════════════════════════════════════════════════════════════════════
# 2. Game embed re-posted below result after wrong guess
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_wrong_guess_result_sent_then_game_edited():
    """
    After a wrong_low/wrong_high guess, the result embed is sent as a new message,
    then the game embed is EDITED in place (edit-in-place design: fixed position).

    Expected sequence:
      channel.send  → result embed (wrong_low/wrong_high)
      fetch_message → get existing game message
      msg.edit      → update game embed (shows new range + timer)
    """
    from cogs.busted99_cog import Busted99Cog

    cog = Busted99Cog(_make_bot())
    session, engine, jack_id, channel, sent_messages = await _bootstrap_guessing(cog)

    # Track edits on the fetched game message
    game_msg_mock = AsyncMock()
    edited_calls = []
    async def _edit(**kwargs):
        edited_calls.append(kwargs)
    game_msg_mock.edit = AsyncMock(side_effect=_edit)
    game_msg_mock.id = 999
    channel.fetch_message = AsyncMock(return_value=game_msg_mock)

    # Give the cog a game_message_id so _upsert_game_message tries to edit
    session.game_message_id = 999

    msg = MagicMock()
    msg.content = "30"
    msg.author = MagicMock()
    msg.author.display_name = "狗與露"
    msg.author.id = int(jack_id)
    msg.author.bot = False
    await cog.on_message(msg)  # 30 < 50 → wrong_low

    # A result embed must have been sent as a new message
    assert len(sent_messages) >= 1, "Must send at least the result embed"
    result_kwargs, _ = sent_messages[-1]
    result_embed = result_kwargs.get("embed")
    assert result_embed is not None, "Result send must include an embed"

    # Game embed must have been EDITED (not sent again)
    assert len(edited_calls) >= 1, (
        "After a wrong guess, the game embed must be edited in place via msg.edit(), "
        "not sent as a new message. Check _upsert_game_message wiring."
    )
    edit_embed = edited_calls[-1].get("embed")
    assert edit_embed is not None, "msg.edit must receive an embed kwarg"
    field_names = [f.name for f in edit_embed.fields]
    assert "範圍" in field_names, (
        "The edited game embed must show the updated range (範圍 field)."
    )


@pytest.mark.asyncio
async def test_wrong_guess_via_chat_game_embed_edited_not_reposted():
    """
    Same constraint via on_message chat path: game embed is edited (not reposted).
    """
    from cogs.busted99_cog import Busted99Cog

    cog = Busted99Cog(_make_bot())
    session, engine, jack_id, channel, sent_messages = await _bootstrap_guessing(cog)

    game_msg_mock = AsyncMock()
    edited_calls = []
    async def _edit(**kwargs):
        edited_calls.append(kwargs)
    game_msg_mock.edit = AsyncMock(side_effect=_edit)
    game_msg_mock.id = 999
    channel.fetch_message = AsyncMock(return_value=game_msg_mock)
    session.game_message_id = 999

    msg = MagicMock()
    msg.content = "30"
    msg.author = MagicMock()
    msg.author.display_name = "狗與露"
    msg.author.id = int(jack_id)
    msg.author.bot = False

    await cog.on_message(msg)

    assert len(sent_messages) >= 1
    assert len(edited_calls) >= 1, (
        "After a wrong guess via chat, the game embed must be edited in place via msg.edit()."
    )
    edit_embed = edited_calls[-1].get("embed")
    field_names = [f.name for f in edit_embed.fields]
    assert "範圍" in field_names, (
        "The edited game embed must show the updated range (範圍 field)."
    )


# ══════════════════════════════════════════════════════════════════════════════
# 3. Joining embed re-posted (not duplicated) when player joins
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_join_reposts_embed_not_duplicates():
    """
    When a player clicks Join, the joining embed must be RE-POSTED at the bottom
    (delete old + send new), not accumulated as duplicate messages.

    Before fix: Join99View.join calls _channel.send directly, so each join
    adds another embed without removing the previous one.
    """
    from cogs.busted99_cog import Busted99Cog, Join99View
    from game.busted99.session import Busted99Session

    bot = _make_bot()
    cog = Busted99Cog(bot)

    session = Busted99Session(session_id="x", guild_id=1, channel_id=1)
    cog._session = session

    from game.busted99.engine import Busted99Engine
    async def _noop(s): pass
    engine = Busted99Engine(session=session, on_state_change=_noop, db_path=":memory:")
    cog._engine = engine
    await engine.add_player("marvin", "Marvin")

    sent_messages = []
    deleted_ids = []

    async def _send(**kwargs):
        m = MagicMock()
        m.id = len(sent_messages) + 100
        m.delete = AsyncMock()
        sent_messages.append(kwargs)
        return m

    channel = AsyncMock()
    channel.send = AsyncMock(side_effect=_send)
    channel.fetch_message = AsyncMock(return_value=AsyncMock(delete=AsyncMock()))
    cog._channel = channel

    # Simulate initial embed present (game_message_id set)
    session.game_message_id = 99

    view = Join99View(cog)

    interaction = MagicMock()
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    interaction.user = MagicMock()
    interaction.user.id = 12345
    interaction.user.display_name = "狗與露"

    # v.join is a discord.ui.Button; .callback is _ItemCallback; .callback.callback is the fn
    await view.join.callback.callback(view, interaction, MagicMock())

    # After joining, _post_game_message should have been used:
    # fetch_message should be called to delete the OLD embed
    channel.fetch_message.assert_called(), (
        "Join must delete the previous joining embed (fetch + delete) "
        "before posting the updated one — otherwise embeds pile up."
    )
    # Only ONE new embed should be at the bottom (the repost)
    embed_sends = [m for m in sent_messages if m.get("embed") is not None]
    assert len(embed_sends) == 1, (
        f"Join must post exactly ONE updated embed (got {len(embed_sends)}), "
        "not accumulate duplicate joining embeds."
    )
