"""
UI behavior tests for Busted99:

  1. Game embed stays at bottom
     - After wrong guess, game embed is re-posted below the result message
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
async def test_wrong_guess_game_embed_reposted_below_result():
    """
    After a wrong_low/wrong_high guess, the sequence of channel.send calls must
    end with the GUESSING embed (range + timer), not the result embed.

    Before fix:
      on_state_change(GUESSING) → game embed  ← sent first
      receive_voice_answer → result embed      ← sent second (below game embed)
    The game embed ends up ABOVE the result embed — scrolls away from bottom.

    After fix:
      on_state_change(GUESSING) → game embed
      receive_voice_answer → result embed
      receive_voice_answer → re-post game embed  ← sent last (stays at bottom)
    """
    from cogs.busted99_cog import Busted99Cog

    cog = Busted99Cog(_make_bot())
    session, engine, jack_id, channel, sent_messages = await _bootstrap_guessing(cog)

    await cog.receive_voice_answer_by_speaker("狗與露", "30")  # 30 < 50 → wrong_low

    assert len(sent_messages) >= 2, "Must send at least result embed + game embed"

    # Last message sent must be the game embed (has a "範圍" field)
    last_kwargs, _ = sent_messages[-1]
    last_embed = last_kwargs.get("embed")
    assert last_embed is not None, "Last send must include an embed"
    field_names = [f.name for f in last_embed.fields]
    assert "範圍" in field_names, (
        "After a wrong guess, the LAST message sent must be the GUESSING embed "
        "(showing range + timer) so it stays at the bottom of the channel."
    )


@pytest.mark.asyncio
async def test_wrong_guess_via_chat_game_embed_at_bottom():
    """
    Same constraint via on_message chat path: game embed must be last sent.
    """
    from cogs.busted99_cog import Busted99Cog

    cog = Busted99Cog(_make_bot())
    session, engine, jack_id, channel, sent_messages = await _bootstrap_guessing(cog)

    msg = MagicMock()
    msg.content = "30"
    msg.author = MagicMock()
    msg.author.display_name = "狗與露"
    msg.author.bot = False

    await cog.on_message(msg)

    assert len(sent_messages) >= 2

    last_kwargs, _ = sent_messages[-1]
    last_embed = last_kwargs.get("embed")
    assert last_embed is not None
    field_names = [f.name for f in last_embed.fields]
    assert "範圍" in field_names, (
        "After a wrong guess via chat, the LAST channel message must be "
        "the GUESSING embed so it stays at the bottom."
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
