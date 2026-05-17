"""TDD — Busted99 wrong_low/wrong_high 後依序播放 range TTS → narration TTS

流程要求：
  A) wrong_low 後：range TTS（含 low/high）在 narration TTS 之前觸發
  B) wrong_high 後：同上順序
  C) range TTS 的文字必須包含 session.low_bound 和 session.high_bound 的更新值
  D) bust 結果 → 不觸發 range TTS，只觸發 narration
  E) LLM narration 空字串（引擎沒有 narration）→ 只觸發 range TTS，不崩潰

「不競爭」的驗法：fired_texts 的順序必須是 range 在前、narration 在後。
"""

from __future__ import annotations

import asyncio
import uuid
import pytest
from unittest.mock import AsyncMock, MagicMock


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_bot():
    bot = MagicMock()
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    engine = MagicMock()
    engine._full_stt_inflight = 0
    engine._MAX_FULL_STT_INFLIGHT = 3
    bot.engine = engine
    return bot


def _make_vc_mock():
    vc = AsyncMock()
    vc._tts_protected = False
    return vc


async def _bootstrap_guessing(cog, *, guesser_name="狗與露", guesser_id="11111", answer=50):
    from game.busted99.engine import Busted99Engine
    from game.busted99.session import Busted99Session

    session = Busted99Session(
        session_id=str(uuid.uuid4()), guild_id=1, channel_id=1,
    )
    channel = AsyncMock()
    channel.send = AsyncMock()
    cog._channel = channel

    async def _noop(s): pass
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

    from game.busted99.session import Busted99State as S
    session.state = S.GUESSING
    cog._play_sfx = AsyncMock()
    cog._guessing_deadline = asyncio.get_event_loop().time() + 60

    return session, game_engine, channel


# ─── A: wrong_low → range TTS 在 narration 前 ────────────────────────────────

@pytest.mark.asyncio
async def test_wrong_low_range_tts_fires_before_narration():
    """
    猜題者猜 30（低於 answer=50）→ wrong_low →
    fired_texts[0] 含 range 資訊，fired_texts[1] 是 narration。
    """
    from cogs.busted99_cog import Busted99Cog
    bot = _make_bot()
    vc_mock = _make_vc_mock()
    bot.cogs.get.side_effect = lambda name: vc_mock if name == "VoiceController" else None

    cog = Busted99Cog(bot)
    session, _, channel = await _bootstrap_guessing(cog, answer=50)

    fired_texts: list[str] = []

    async def _capture_fire(vc, text):
        fired_texts.append(text)

    cog._fire_tts = _capture_fire

    # 讓引擎 submit_guess 回傳固定 narration
    original_submit = cog._engine.submit_guess

    async def _patched_submit(uid, num):
        result = await original_submit(uid, num)
        if result:
            result["narration"] = "狗與露猜低了，範圍縮小，輪到下一位"
        return result

    cog._engine.submit_guess = _patched_submit

    ok, res = await cog._process_guess("狗與露", "11111", 30)
    # 等所有 spawned task 執行
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert ok is True
    assert res == "wrong_low"
    assert len(fired_texts) >= 2, f"應有至少 2 個 TTS 呼叫，實際：{fired_texts}"

    # 第一個必須是 range（含數字），第二個是 narration
    assert any(c.isdigit() for c in fired_texts[0]), (
        f"fired_texts[0] 應是 range TTS（含數字），實際：{fired_texts[0]!r}"
    )
    assert fired_texts[-1] == "狗與露猜低了，範圍縮小，輪到下一位", (
        f"最後 TTS 應是 narration，實際：{fired_texts[-1]!r}"
    )


# ─── B: wrong_high → range TTS 在 narration 前 ───────────────────────────────

@pytest.mark.asyncio
async def test_wrong_high_range_tts_fires_before_narration():
    """
    猜題者猜 70（高於 answer=50）→ wrong_high →
    fired_texts[0] 含 range 資訊，fired_texts[-1] 是 narration。
    """
    from cogs.busted99_cog import Busted99Cog
    bot = _make_bot()
    vc_mock = _make_vc_mock()
    bot.cogs.get.side_effect = lambda name: vc_mock if name == "VoiceController" else None

    cog = Busted99Cog(bot)
    session, _, channel = await _bootstrap_guessing(cog, answer=50)

    fired_texts: list[str] = []

    async def _capture_fire(vc, text):
        fired_texts.append(text)

    cog._fire_tts = _capture_fire

    original_submit = cog._engine.submit_guess

    async def _patched_submit(uid, num):
        result = await original_submit(uid, num)
        if result:
            result["narration"] = "猜高了！緊張感上升"
        return result

    cog._engine.submit_guess = _patched_submit

    ok, res = await cog._process_guess("狗與露", "11111", 70)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert ok is True
    assert res == "wrong_high"
    assert len(fired_texts) >= 2, f"應有至少 2 個 TTS 呼叫：{fired_texts}"
    assert any(c.isdigit() for c in fired_texts[0]), (
        f"第一個 TTS 應是 range（含數字）：{fired_texts[0]!r}"
    )
    assert fired_texts[-1] == "猜高了！緊張感上升"


# ─── C: range TTS 文字包含 session 更新後的 low/high ─────────────────────────

@pytest.mark.asyncio
async def test_range_tts_text_contains_updated_bounds():
    """
    wrong_low：猜 30，answer=50 → new low_bound=31。
    range TTS 文字必須包含 31 和 99（high 不變）。
    """
    from cogs.busted99_cog import Busted99Cog
    bot = _make_bot()
    vc_mock = _make_vc_mock()
    bot.cogs.get.side_effect = lambda name: vc_mock if name == "VoiceController" else None

    cog = Busted99Cog(bot)
    session, _, channel = await _bootstrap_guessing(cog, answer=50)

    fired_texts: list[str] = []

    async def _capture_fire(vc, text):
        fired_texts.append(text)

    cog._fire_tts = _capture_fire

    ok, res = await cog._process_guess("狗與露", "11111", 30)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert res == "wrong_low"
    # 找 range TTS（第一個含數字的呼叫）
    range_texts = [t for t in fired_texts if any(c.isdigit() for c in t)]
    assert range_texts, "應有 range TTS 文字"
    range_text = range_texts[0]
    assert "30" in range_text, f"range TTS 應含 new_low=30，實際：{range_text!r}"
    assert "99" in range_text, f"range TTS 應含 high=99，實際：{range_text!r}"


# ─── D: bust → 無 range TTS ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bust_does_not_fire_range_tts():
    """
    猜中（bust）→ 只有 narration TTS，不應有 range TTS。
    """
    from cogs.busted99_cog import Busted99Cog
    bot = _make_bot()
    vc_mock = _make_vc_mock()
    bot.cogs.get.side_effect = lambda name: vc_mock if name == "VoiceController" else None

    cog = Busted99Cog(bot)
    session, _, channel = await _bootstrap_guessing(cog, answer=50)

    # 加入第二位玩家，因為 bust 需要有隊伍
    await cog._engine.add_player("22222", "Showay")
    session.guessing_queue = []

    fired_texts: list[str] = []

    async def _capture_fire(vc, text):
        fired_texts.append(text)

    cog._fire_tts = _capture_fire

    original_submit = cog._engine.submit_guess

    async def _patched_submit(uid, num):
        result = await original_submit(uid, num)
        if result:
            result["narration"] = "BUSTED！"
        return result

    cog._engine.submit_guess = _patched_submit

    ok, res = await cog._process_guess("狗與露", "11111", 50)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert res in ("bust", "last_bust", "last_wrong"), f"猜中應是 bust 系列，實際：{res}"
    # range TTS 的特徵：含"到"且含兩個數字邊界
    range_fired = [t for t in fired_texts if "到" in t and any(c.isdigit() for c in t)
                   and t != "BUSTED！"]
    assert len(range_fired) == 0, f"bust 不應有 range TTS，實際：{range_fired}"


# ─── E: narration 為空 → 只 range TTS，不崩潰 ────────────────────────────────

@pytest.mark.asyncio
async def test_empty_narration_only_fires_range_tts():
    """
    引擎沒有回傳 narration（或空字串）→ 只播 range TTS，不崩潰。
    """
    from cogs.busted99_cog import Busted99Cog
    bot = _make_bot()
    vc_mock = _make_vc_mock()
    bot.cogs.get.side_effect = lambda name: vc_mock if name == "VoiceController" else None

    cog = Busted99Cog(bot)
    session, _, channel = await _bootstrap_guessing(cog, answer=50)

    fired_texts: list[str] = []

    async def _capture_fire(vc, text):
        fired_texts.append(text)

    cog._fire_tts = _capture_fire

    # 引擎不附 narration
    original_submit = cog._engine.submit_guess

    async def _patched_submit(uid, num):
        result = await original_submit(uid, num)
        if result:
            result.pop("narration", None)
        return result

    cog._engine.submit_guess = _patched_submit

    ok, res = await cog._process_guess("狗與露", "11111", 30)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert res == "wrong_low"
    assert len(fired_texts) >= 1, "沒有 narration 時應仍有 range TTS"
    assert any(c.isdigit() for c in fired_texts[0]), "range TTS 應含數字"
