"""TDD — Busted99 語音 UX 改善

覆蓋要求：
  A) 數字解析成功 → 立刻 fire TTS echo "X猜N" (before _process_guess 完成)
  B) parse_number=None + extract_guess 2s timeout → TTS "X，請打字輸入數字"
  C) parse_number=None + extract_guess 回 None（非 timeout）→ 同樣提示鍵盤
  D) non-guesser 語音：should_suppress_for_game=True → receive_voice_answer_by_speaker 回 False，不送 channel 訊息
  E) Web b99_guess from 非當前猜題人 → 靜默捨棄，不送 channel.send（移除打擾訊息）
  F) STT 塞車（full_stt_inflight >= MAX）→ receive_voice_answer_by_speaker 回傳 False 並通知 channel
"""

from __future__ import annotations

import asyncio
import uuid
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_bot():
    bot = MagicMock()
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    # 模擬 bot.engine（DiscordVoiceEngine）
    engine = MagicMock()
    engine._full_stt_inflight = 0
    engine._MAX_FULL_STT_INFLIGHT = 3
    bot.engine = engine
    return bot


def _make_vc_mock():
    vc = AsyncMock()
    vc._tts_protected = False
    return vc


async def _bootstrap_guessing(cog, *, guesser_name="狗與露", guesser_id="11111"):
    from game.busted99.engine import Busted99Engine
    from game.busted99.session import Busted99Session, Busted99State

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
    session.answer = 50
    session.low_bound = 1
    session.high_bound = 99
    session.guessing_queue = []

    from game.busted99.session import Busted99State as S
    session.state = S.GUESSING
    cog._play_sfx = AsyncMock()

    return session, game_engine, channel


# ─── A: 數字解析成功 → TTS echo 立刻觸發 ─────────────────────────────────────

@pytest.mark.asyncio
async def test_voice_guess_fires_tts_echo_on_parsed_number():
    """
    猜題者說 '我猜42' → parse_number 成功 → TTS '狗與露猜42' 在 _process_guess 之前即 spawn。
    驗法：mock _fire_tts，確認它以正確文字被 spawn。
    """
    from cogs.busted99_cog import Busted99Cog
    bot = _make_bot()
    vc_mock = _make_vc_mock()
    bot.cogs.get.side_effect = lambda name: vc_mock if name == "VoiceController" else None

    cog = Busted99Cog(bot)
    session, _, channel = await _bootstrap_guessing(cog, guesser_name="狗與露", guesser_id="11111")

    fired_texts = []
    original_fire = cog._fire_tts

    async def _capture_fire(vc, text):
        fired_texts.append(text)

    cog._fire_tts = _capture_fire

    consumed = await cog.receive_voice_answer_by_speaker("狗與露", "我猜四十二")
    await asyncio.sleep(0)  # 讓 spawned TTS task 有機會執行
    assert consumed is True, "parse 成功應回 True"
    assert any("42" in t for t in fired_texts), (
        f"TTS echo 應含 '42'，實際觸發：{fired_texts}"
    )
    # echo 應出現在 fired_texts[0]（最先呼叫）
    assert fired_texts[0] == "狗與露猜42", (
        f"第一個 TTS 應是 echo '狗與露猜42'，實際：{fired_texts[0]!r}"
    )


# ─── B: extract_guess 2s timeout → TTS 鍵盤提示 ─────────────────────────────

@pytest.mark.asyncio
async def test_voice_guess_parse_timeout_fires_keyboard_hint():
    """
    猜題者說 '嗯我覺得大概是那個吧' → parse_number=None → extract_guess_via_llm 超時 →
    TTS/channel 提示請打字，receive_voice_answer_by_speaker 回 False。
    """
    from cogs.busted99_cog import Busted99Cog
    bot = _make_bot()
    vc_mock = _make_vc_mock()
    bot.cogs.get.side_effect = lambda name: vc_mock if name == "VoiceController" else None

    cog = Busted99Cog(bot)
    session, _, channel = await _bootstrap_guessing(cog, guesser_name="狗與露", guesser_id="11111")

    fired_texts = []
    async def _capture_fire(vc, text): fired_texts.append(text)
    cog._fire_tts = _capture_fire

    async def _slow_llm(text, low, high):
        await asyncio.sleep(10)  # 超時
        return None

    with patch("cogs.busted99_cog.extract_guess_via_llm", _slow_llm):
        consumed = await cog.receive_voice_answer_by_speaker("狗與露", "嗯我覺得大概是那個吧")
    await asyncio.sleep(0)

    assert consumed is False
    # TTS 或 channel 要通知打字
    has_keyboard_hint = any("打字" in t or "鍵盤" in t or "keyboard" in t.lower() for t in fired_texts)
    channel_hinted = any(
        "打字" in str(call) or "鍵盤" in str(call)
        for call in channel.send.call_args_list
    )
    assert has_keyboard_hint or channel_hinted, (
        f"超時後應 TTS 或 channel 提示打字，fired_texts={fired_texts}, "
        f"channel calls={channel.send.call_args_list}"
    )


# ─── C: extract_guess 回 None（不超時）→ 同樣提示 ───────────────────────────

@pytest.mark.asyncio
async def test_voice_guess_parse_none_fires_keyboard_hint():
    """
    extract_guess 快速回 None（LLM 確認沒有數字）→ 也要提示打字。
    """
    from cogs.busted99_cog import Busted99Cog
    bot = _make_bot()
    vc_mock = _make_vc_mock()
    bot.cogs.get.side_effect = lambda name: vc_mock if name == "VoiceController" else None

    cog = Busted99Cog(bot)
    session, _, channel = await _bootstrap_guessing(cog, guesser_name="狗與露", guesser_id="11111")

    fired_texts = []
    async def _capture_fire(vc, text): fired_texts.append(text)
    cog._fire_tts = _capture_fire

    async def _null_llm(text, low, high): return None

    with patch("cogs.busted99_cog.extract_guess_via_llm", _null_llm):
        consumed = await cog.receive_voice_answer_by_speaker("狗與露", "嗯嗯嗯嗯嗯")
    await asyncio.sleep(0)

    assert consumed is False
    has_hint = (
        any("打字" in t or "鍵盤" in t for t in fired_texts)
        or any("打字" in str(c) or "鍵盤" in str(c) for c in channel.send.call_args_list)
    )
    assert has_hint, "無法解析數字時應提示打字輸入"


# ─── D: 非猜題者語音 → receive_voice_answer_by_speaker 靜默回 False ──────────

@pytest.mark.asyncio
async def test_non_guesser_voice_silently_returns_false():
    """
    非猜題玩家 Showay 說 '我猜42' → receive_voice_answer_by_speaker 回 False，
    不觸發 TTS echo，不送 channel.send。
    """
    from cogs.busted99_cog import Busted99Cog
    bot = _make_bot()
    vc_mock = _make_vc_mock()
    bot.cogs.get.side_effect = lambda name: vc_mock if name == "VoiceController" else None

    cog = Busted99Cog(bot)
    session, _, channel = await _bootstrap_guessing(cog, guesser_name="狗與露", guesser_id="11111")

    # Showay 不是猜題者
    await cog._engine.add_player("22222", "Showay")

    fired_texts = []
    async def _capture_fire(vc, text): fired_texts.append(text)
    cog._fire_tts = _capture_fire

    consumed = await cog.receive_voice_answer_by_speaker("Showay", "我猜42")
    assert consumed is False, "非猜題者語音應回 False"
    assert len(fired_texts) == 0, "非猜題者語音不應觸發 TTS"
    assert channel.send.call_count == 0, "非猜題者語音不應送 channel 訊息"


# ─── E: Web b99_guess 非當前猜題人 → 靜默捨棄，不送 channel.send ─────────────

@pytest.mark.asyncio
async def test_web_action_wrong_guesser_silently_dropped():
    """
    Web UI 送出 b99_guess，但 resolved_user_id 不是當前猜題人。
    不應送 Discord channel 訊息（移除 '⏸ X 從 Web 送出 N' 的干擾）。
    """
    from cogs.busted99_cog import Busted99Cog
    bot = _make_bot()
    cog = Busted99Cog(bot)
    session, _, channel = await _bootstrap_guessing(cog, guesser_name="狗與露", guesser_id="11111")
    # 加入第二個玩家但不是猜題者
    await cog._engine.add_player("22222", "Showay")

    action = {
        "type": "b99_guess",
        "resolved_user_id": "22222",  # Showay，不是猜題者
        "number": 42,
    }
    await cog._handle_web_action(action)

    # 不應有任何 channel.send
    assert channel.send.call_count == 0, (
        "Web 非猜題者猜題應靜默捨棄，不送 channel 訊息。"
        f"實際 calls: {channel.send.call_args_list}"
    )


# ─── F: STT 塞車 → channel 警告 ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stt_congestion_warns_channel_when_maxed():
    """
    bot.engine._full_stt_inflight >= _MAX_FULL_STT_INFLIGHT 且在 GUESSING 狀態 →
    receive_voice_answer_by_speaker 應在 channel 發警告（或 TTS），並回 False（不繼續處理）。
    """
    from cogs.busted99_cog import Busted99Cog
    bot = _make_bot()
    bot.engine._full_stt_inflight = 3   # 已滿
    bot.engine._MAX_FULL_STT_INFLIGHT = 3

    vc_mock = _make_vc_mock()
    bot.cogs.get.side_effect = lambda name: vc_mock if name == "VoiceController" else None

    cog = Busted99Cog(bot)
    session, _, channel = await _bootstrap_guessing(cog, guesser_name="狗與露", guesser_id="11111")

    fired_texts = []
    async def _capture_fire(vc, text): fired_texts.append(text)
    cog._fire_tts = _capture_fire

    consumed = await cog.receive_voice_answer_by_speaker("狗與露", "我猜四十二")

    # 塞車時應通知並回 False
    assert consumed is False, "STT 塞車時應回 False（數字無法可靠解析）"
    warned = (
        any("塞" in t or "congestion" in t.lower() or "排隊" in t for t in fired_texts)
        or any("塞" in str(c) or "排隊" in str(c) for c in channel.send.call_args_list)
    )
    assert warned, (
        f"STT 塞車應通知玩家，fired={fired_texts}, channel={channel.send.call_args_list}"
    )
