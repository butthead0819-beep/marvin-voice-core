"""TDD — Busted99 SFX → TTS 排序與三段音效改造

需求：
1. 玩家猜題（wrong_low / wrong_high） → 先播 ba_dum_tss 音效，再接 range + narration TTS
2. 玩家猜中爆掉（bust / last_bust / last_wrong） → 先播 sad_horn，再接 narration TTS
3. 出題人完成後（SETTER_PICKING → GUESSING 首次轉移） → 播 air_horn

關鍵不變式：
- SFX 必須在 TTS 之前先完成（透過共用 events list 驗序）
- 不影響舊有 fanfare/buzz 在 first-guess 之後輪次的播放
"""
from __future__ import annotations

import asyncio
import uuid
import pytest
from unittest.mock import AsyncMock, MagicMock


def _make_bot():
    bot = MagicMock()
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    engine = MagicMock()
    engine._full_stt_inflight = 0
    engine._MAX_FULL_STT_INFLIGHT = 3
    bot.engine = engine
    return bot


async def _bootstrap_guessing(cog, *, guesser_name="狗與露", guesser_id="11111", answer=50):
    from game.busted99.engine import Busted99Engine
    from game.busted99.session import Busted99Session, Busted99State

    session = Busted99Session(
        session_id=str(uuid.uuid4()), guild_id=1, channel_id=1,
    )
    channel = AsyncMock()
    channel.send = AsyncMock()
    cog._channel = channel

    async def _noop(s):
        pass

    game_engine = Busted99Engine(
        session=session, on_state_change=_noop, db_path=":memory:",
    )
    cog._engine = game_engine
    cog._session = session

    await game_engine.add_player("marvin", "Marvin")
    await game_engine.add_player(guesser_id, guesser_name)
    cog._name_to_id[guesser_name] = int(guesser_id)

    session.setter_id = "marvin"
    session.current_guesser_id = guesser_id
    session.answer = answer
    session.low_bound = 1
    session.high_bound = 99
    session.guessing_queue = []
    session.state = Busted99State.GUESSING
    cog._guessing_deadline = asyncio.get_event_loop().time() + 60
    return session, game_engine, channel


def _wire_events(cog):
    """讓 _play_sfx 與 _fire_tts 共用同一個 events list，方便驗序。"""
    events: list[str] = []

    async def _capture_sfx(name: str):
        events.append(f"SFX:{name}")

    async def _capture_fire(vc, text):
        events.append(f"TTS:{text}")

    cog._play_sfx = _capture_sfx
    cog._fire_tts = _capture_fire
    return events


# ─── 1. wrong_low → ba_dum_tss 先於 TTS ───────────────────────────────────────

@pytest.mark.asyncio
async def test_wrong_low_plays_ba_dum_tss_before_tts():
    from cogs.busted99_cog import Busted99Cog
    bot = _make_bot()
    vc_mock = AsyncMock()
    vc_mock._tts_protected = False
    bot.cogs.get.side_effect = lambda name: vc_mock if name == "VoiceController" else None

    cog = Busted99Cog(bot)
    await _bootstrap_guessing(cog, answer=50)
    events = _wire_events(cog)

    original_submit = cog._engine.submit_guess

    async def _patched_submit(uid, num):
        result = await original_submit(uid, num)
        if result:
            result["narration"] = "低了喔"
        return result

    cog._engine.submit_guess = _patched_submit

    ok, res = await cog._process_guess("狗與露", "11111", 30)
    # 讓 spawned chain 跑完
    for _ in range(5):
        await asyncio.sleep(0)

    assert ok is True
    assert res == "wrong_low"
    # SFX 必須是第一個事件
    assert events[0] == "SFX:ba_dum_tss", f"首事件應為 ba_dum_tss，實際：{events}"
    # narration 必在最後
    assert events[-1] == "TTS:低了喔", f"末事件應為 narration，實際：{events}"
    # 不應包含舊的 wrong SFX
    assert "SFX:wrong" not in events


# ─── 2. wrong_high → ba_dum_tss 先於 TTS ──────────────────────────────────────

@pytest.mark.asyncio
async def test_wrong_high_plays_ba_dum_tss_before_tts():
    from cogs.busted99_cog import Busted99Cog
    bot = _make_bot()
    vc_mock = AsyncMock()
    vc_mock._tts_protected = False
    bot.cogs.get.side_effect = lambda name: vc_mock if name == "VoiceController" else None

    cog = Busted99Cog(bot)
    await _bootstrap_guessing(cog, answer=50)
    events = _wire_events(cog)

    original_submit = cog._engine.submit_guess

    async def _patched_submit(uid, num):
        result = await original_submit(uid, num)
        if result:
            result["narration"] = "太高了"
        return result

    cog._engine.submit_guess = _patched_submit

    ok, res = await cog._process_guess("狗與露", "11111", 70)
    for _ in range(5):
        await asyncio.sleep(0)

    assert ok is True
    assert res == "wrong_high"
    assert events[0] == "SFX:ba_dum_tss"
    assert events[-1] == "TTS:太高了"


# ─── 3. bust → sad_horn 先於 narration TTS ────────────────────────────────────

@pytest.mark.asyncio
async def test_bust_plays_sad_horn_before_narration():
    from cogs.busted99_cog import Busted99Cog
    bot = _make_bot()
    vc_mock = AsyncMock()
    vc_mock._tts_protected = False
    bot.cogs.get.side_effect = lambda name: vc_mock if name == "VoiceController" else None

    cog = Busted99Cog(bot)
    session, _, _ = await _bootstrap_guessing(cog, answer=50)
    await cog._engine.add_player("22222", "Showay")
    session.guessing_queue = []
    events = _wire_events(cog)

    original_submit = cog._engine.submit_guess

    async def _patched_submit(uid, num):
        result = await original_submit(uid, num)
        if result:
            result["narration"] = "你爆了"
        return result

    cog._engine.submit_guess = _patched_submit

    ok, res = await cog._process_guess("狗與露", "11111", 50)
    for _ in range(5):
        await asyncio.sleep(0)

    assert res in ("bust", "last_bust", "last_wrong"), f"應為 bust 類，實際：{res}"
    # bust 系列的 SFX 必須是 sad_horn（不是 ba_dum_tss）
    assert events[0] == "SFX:sad_horn", f"bust 首事件應為 sad_horn，實際：{events}"
    assert "SFX:correct" not in events
    assert "TTS:你爆了" in events
    # sad_horn 必須在 narration 前
    assert events.index("SFX:sad_horn") < events.index("TTS:你爆了")


# ─── 4. setter 完成（SETTER_PICKING → GUESSING）→ air_horn ────────────────────

@pytest.mark.asyncio
async def test_setter_complete_plays_air_horn():
    """
    模擬 setter 設定完成的狀態轉移：
    _prev_state = SETTER_PICKING → on_state_change(GUESSING) 應播 air_horn。
    """
    from cogs.busted99_cog import Busted99Cog
    from game.busted99.session import Busted99Session, Busted99State

    bot = _make_bot()
    cog = Busted99Cog(bot)

    session = Busted99Session(
        session_id=str(uuid.uuid4()), guild_id=1, channel_id=1,
    )
    await cog._engine_init_stub(session) if hasattr(cog, "_engine_init_stub") else None
    cog._session = session
    cog._channel = AsyncMock()
    cog._channel.send = AsyncMock()
    cog._post_game_message = AsyncMock()
    cog._upsert_game_message = AsyncMock()
    cog._edit_game_message = AsyncMock()
    cog._emit_phase = AsyncMock()
    cog._emit_ws_state = AsyncMock()
    cog._send_player_links = AsyncMock()
    cog._spawn = lambda coro: coro.close() or None  # 不執行 background coro

    events = _wire_events(cog)

    # 標記前一狀態為 SETTER_PICKING（模擬 setter 剛完成）
    cog._prev_state = Busted99State.SETTER_PICKING

    # 設定 session 為 GUESSING（首回合）
    session.state = Busted99State.GUESSING
    session.setter_id = "marvin"
    session.current_guesser_id = "11111"
    session.round_num = 1
    session.low_bound = 1
    session.high_bound = 99
    session.players = []

    await cog.on_state_change(session)
    for _ in range(3):
        await asyncio.sleep(0)

    sfx_calls = [e for e in events if e.startswith("SFX:")]
    assert "SFX:air_horn" in sfx_calls, f"應有 air_horn，實際：{sfx_calls}"


# ─── 5. 後續輪次 GUESSING 不重播 air_horn ─────────────────────────────────────

@pytest.mark.asyncio
async def test_subsequent_guessing_no_air_horn():
    """
    SETTER_PICKING → GUESSING 已過，後續每次 advance_guesser 重進 GUESSING
    （prev_state 已是 GUESSING）不應再播 air_horn。
    """
    from cogs.busted99_cog import Busted99Cog
    from game.busted99.session import Busted99Session, Busted99State

    bot = _make_bot()
    cog = Busted99Cog(bot)

    session = Busted99Session(
        session_id=str(uuid.uuid4()), guild_id=1, channel_id=1,
    )
    cog._session = session
    cog._channel = AsyncMock()
    cog._channel.send = AsyncMock()
    cog._post_game_message = AsyncMock()
    cog._upsert_game_message = AsyncMock()
    cog._edit_game_message = AsyncMock()
    cog._emit_phase = AsyncMock()
    cog._emit_ws_state = AsyncMock()
    cog._send_player_links = AsyncMock()
    cog._spawn = lambda coro: coro.close() or None

    events = _wire_events(cog)

    # 前一狀態已是 GUESSING（換人輪流，非首輪）
    cog._prev_state = Busted99State.GUESSING

    session.state = Busted99State.GUESSING
    session.setter_id = "marvin"
    session.current_guesser_id = "11111"
    session.round_num = 2
    session.low_bound = 20
    session.high_bound = 80
    session.players = []

    await cog.on_state_change(session)
    for _ in range(3):
        await asyncio.sleep(0)

    sfx_calls = [e for e in events if e.startswith("SFX:")]
    assert "SFX:air_horn" not in sfx_calls, f"非首輪不應有 air_horn，實際：{sfx_calls}"


# ─── 6. LLM engine 路徑也要走相同 SFX 序列 ────────────────────────────────────

@pytest.mark.asyncio
async def test_llm_engine_wrong_low_plays_ba_dum_tss():
    """
    使用 Busted99LLMEngine（不是 Busted99Engine）→ wrong_low →
    SFX 序列 ba_dum_tss → range → narration 仍然成立。
    """
    from cogs.busted99_cog import Busted99Cog
    from game.busted99.llm_engine import Busted99LLMEngine
    from game.busted99.session import Busted99Session, Busted99State

    bot = _make_bot()
    vc_mock = AsyncMock()
    vc_mock._tts_protected = False
    bot.cogs.get.side_effect = lambda name: vc_mock if name == "VoiceController" else None

    cog = Busted99Cog(bot)
    session = Busted99Session(
        session_id=str(uuid.uuid4()), guild_id=1, channel_id=1,
    )
    channel = AsyncMock()
    channel.send = AsyncMock()
    cog._channel = channel

    async def _noop(s):
        pass

    llm_engine = Busted99LLMEngine(
        session=session, on_state_change=_noop, db_path=":memory:",
    )

    # mock LLM call → 回傳固定 wrong_low + narration
    async def _fake_llm(*args, **kwargs):
        return {"outcome": "wrong_low", "narration": "LLM說：低了"}

    llm_engine._call_llm = _fake_llm

    cog._engine = llm_engine
    cog._session = session

    await llm_engine.add_player("marvin", "Marvin")
    await llm_engine.add_player("11111", "狗與露")
    cog._name_to_id["狗與露"] = 11111

    session.setter_id = "marvin"
    session.current_guesser_id = "11111"
    session.answer = 50
    session.low_bound = 1
    session.high_bound = 99
    session.guessing_queue = []
    session.state = Busted99State.GUESSING
    cog._guessing_deadline = asyncio.get_event_loop().time() + 60

    events = _wire_events(cog)

    ok, res = await cog._process_guess("狗與露", "11111", 30)
    for _ in range(5):
        await asyncio.sleep(0)

    assert ok is True
    assert res == "wrong_low"
    assert events[0] == "SFX:ba_dum_tss", f"LLM 路徑首事件應為 ba_dum_tss，實際：{events}"
    assert events[-1] == "TTS:LLM說：低了", f"LLM narration 應為末事件，實際：{events}"


@pytest.mark.asyncio
async def test_llm_engine_bust_plays_sad_horn():
    """
    LLM engine 回傳 bust → sad_horn → narration TTS。
    """
    from cogs.busted99_cog import Busted99Cog
    from game.busted99.llm_engine import Busted99LLMEngine
    from game.busted99.session import Busted99Session, Busted99State

    bot = _make_bot()
    vc_mock = AsyncMock()
    vc_mock._tts_protected = False
    bot.cogs.get.side_effect = lambda name: vc_mock if name == "VoiceController" else None

    cog = Busted99Cog(bot)
    session = Busted99Session(
        session_id=str(uuid.uuid4()), guild_id=1, channel_id=1,
    )
    cog._channel = AsyncMock()
    cog._channel.send = AsyncMock()

    async def _noop(s):
        pass

    llm_engine = Busted99LLMEngine(
        session=session, on_state_change=_noop, db_path=":memory:",
    )

    async def _fake_llm(*args, **kwargs):
        return {"outcome": "bust", "narration": "💥 LLM播報：爆了"}

    llm_engine._call_llm = _fake_llm

    cog._engine = llm_engine
    cog._session = session

    await llm_engine.add_player("marvin", "Marvin")
    await llm_engine.add_player("11111", "狗與露")
    await llm_engine.add_player("22222", "Showay")
    cog._name_to_id["狗與露"] = 11111

    session.setter_id = "marvin"
    session.current_guesser_id = "11111"
    session.answer = 50
    session.low_bound = 1
    session.high_bound = 99
    session.guessing_queue = []
    session.state = Busted99State.GUESSING
    cog._guessing_deadline = asyncio.get_event_loop().time() + 60

    events = _wire_events(cog)

    ok, res = await cog._process_guess("狗與露", "11111", 50)
    for _ in range(5):
        await asyncio.sleep(0)

    assert res in ("bust", "last_bust", "last_wrong"), f"應為 bust 類，實際：{res}"
    assert events[0] == "SFX:sad_horn", f"LLM bust 首事件應為 sad_horn，實際：{events}"
    assert "TTS:💥 LLM播報：爆了" in events
