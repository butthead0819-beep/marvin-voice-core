"""[Intent Gate] silent 分支落地 records/intent_gate_silenced.jsonl（8/18 修正）.

背景：has_intent_signal()=False 之前只會 silent return，連 agent_gaps.jsonl 都不會
記錄——短指令（如裸字「暫停」）因此完全看不見（bot_main.log 顯示 IntentBus
winner=none，但 agent_gaps.jsonl 從未收到這筆）。這道 gate 決定「要不要理」，
不該連帶決定「要不要測量」，所以補一筆零成本（無 LLM）落地供之後人工/批次審查。

第二輪修正（同日）：gap classifier 現在對所有 winner=none 一律先跑（不再被
has_intent_signal 擋在前面）——has_intent_signal 只在 classifier 判 UNKNOWN 之後才
決定要不要繼續让 Marvin 兜底閒聊。intent_gate_silenced.jsonl 因此只會在「classifier
也判不出來（或不可用）+ 無實質指令訊號」時才落地，不再是唯一的測量管道。
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest


pytestmark = pytest.mark.asyncio


async def _agen(items):
    for it in items:
        yield it


def _make_vc(monkeypatch, *, query_intent_signal: bool, query_text: str):
    from cogs.voice_controller import VoiceController
    import cogs.voice_controller as vcmod

    monkeypatch.setattr(vcmod, "is_helper_wake", lambda *a, **k: False)
    monkeypatch.setattr(vcmod, "has_intent_signal", lambda q: query_intent_signal)
    monkeypatch.setattr(vcmod, "is_personal_assistant_query", lambda q: False)
    monkeypatch.setattr(vcmod, "detect_imitation_target", lambda q, players: None)
    monkeypatch.setattr(vcmod, "is_manual_add_query", lambda q: False)
    monkeypatch.setattr(vcmod, "is_task_update_query", lambda q: False)
    monkeypatch.setattr(vcmod, "is_mark_done_query", lambda q: False)
    monkeypatch.setattr(vcmod, "is_recall_query", lambda q: False)

    captured = {}
    def _fake_append(path, record):
        captured["path"] = path
        captured["record"] = record
    monkeypatch.setattr(vcmod, "gap_append_record", _fake_append)

    vc = VoiceController.__new__(VoiceController)
    vc.bot = MagicMock()
    vc.bot.cogs.get.return_value = None
    vc.bot.vision_enabled = False
    vc.bot.router._background_intent_enrich = AsyncMock()
    vc.bot.router._pending_prefetch = {}
    vc.bot.router.memory.list_players.return_value = []
    vc.bot.router.wake_fusion = MagicMock()
    vc.bot.engine.conv_buffer.get_harvest.return_value = query_text
    vc.bot.engine.conv_buffer.get_last_n_utterances.return_value = []
    vc.bot.router.stream_fast_response = MagicMock(return_value=object())

    vc._stream_mode_local = False
    vc._radio_mode_local = False
    vc.game_mode = False
    vc._tts_interrupted = False
    vc._wake_response_pending = True
    vc._awaiting_confirmation = False
    vc._recall_handler = None
    vc._gap_classifier_cached = None
    vc._shared_tier_router = None
    vc.speech_buffers = {}
    vc.user_emotion_cache = {}
    vc.marvin_self_emotion = {}
    vc._last_speech_time = 0.0

    vc.stt_logger = MagicMock()
    vc._ducking_agent = MagicMock()
    vc._ducking_agent.wake_threshold_boost.return_value = 0.0
    vc._room_mood_store = MagicMock()
    vc._room_mood_store.get.return_value.hot_chat = False
    vc._latency_marks = MagicMock()
    vc._latency_marks.mark_first_sentence.return_value = None
    vc._intent_bus = MagicMock()
    vc._intent_bus.dispatch = AsyncMock(return_value=None)

    placeholder = MagicMock()
    placeholder.edit = AsyncMock()
    placeholder.delete = AsyncMock()
    channel = MagicMock()
    channel.send = AsyncMock(return_value=placeholder)
    channel.guild.id = 1
    channel.id = 2
    vc.active_text_channel = channel

    vc._query_quality_gate = MagicMock(return_value=(True, "ok"))
    vc._is_owner_speaker = MagicMock(return_value=False)
    vc._detect_music_command = MagicMock(return_value=None)
    vc._cancel_stale_prefetch = MagicMock()
    vc.get_online_members = MagicMock(return_value=[])
    vc.play_tts = AsyncMock()
    vc.speak = AsyncMock()
    vc._schedule_reaction_check = AsyncMock()
    vc._send_mood_sticker = AsyncMock()
    vc._classify_marvin_self_emotion = AsyncMock()
    vc._llm_wait_ack_watcher = AsyncMock()
    vc._is_low_confidence_answer = MagicMock(return_value=False)
    vc._cot_filter_stream = lambda s: s
    vc._stream_sentence_splitter = lambda _stream: _agen([])

    return vc, captured


async def test_silenced_short_query_gets_logged(monkeypatch):
    """has_intent_signal=False → 不理使用者，但要落地 intent_gate_silenced.jsonl。"""
    vc, captured = _make_vc(monkeypatch, query_intent_signal=False, query_text="暫停")
    await vc._process_queued_query("狗與露", time.time(), wake_intent=None)

    assert captured.get("path") == "records/intent_gate_silenced.jsonl"
    assert captured["record"]["speaker"] == "狗與露"
    assert captured["record"]["query"] == "暫停"
    assert "ts" in captured["record"]
    vc._cancel_stale_prefetch.assert_called_once()


async def test_real_query_does_not_get_silenced_log(monkeypatch):
    """has_intent_signal=True → 不該走 silenced log 分支。"""
    vc, captured = _make_vc(monkeypatch, query_intent_signal=True, query_text="現在幾點呢")
    await vc._process_queued_query("狗與露", time.time(), wake_intent=None)

    assert captured == {}


async def test_gap_classifier_runs_even_when_has_intent_signal_false(monkeypatch):
    """核心修正：classifier 現在對 winner=none 一律先跑，不再被 has_intent_signal
    擋在前面。分類出非 UNKNOWN → ack + skip Marvin，且完全不該碰
    intent_gate_silenced.jsonl（那是 classifier 也判不出來時才用的後備管道）。"""
    import cogs.voice_controller as vcmod
    from intent_gap import IntentGapRecord

    vc, captured = _make_vc(monkeypatch, query_intent_signal=False, query_text="暫停播放")
    vc._shared_tier_router = MagicMock()  # 讓 classifier 不再是 None
    vc._gap_logger = MagicMock()

    monkeypatch.setattr(vcmod, "make_groq_gap_classifier", lambda router: MagicMock())

    fake_rec = IntentGapRecord(
        utterance_id="u1", ts=time.time(), speaker="狗與露", mode="normal",
        raw_query="暫停播放", cleaned_query="暫停播放", intent_type="playback_control_pause",
        slots={}, nearest_agent="playback_control", nearest_distance=0.0,
        ack_text="收到！", acknowledged=True,
    )
    handle_gap_mock = AsyncMock(return_value=fake_rec)
    monkeypatch.setattr(vcmod, "handle_intent_gap", handle_gap_mock)

    await vc._process_queued_query("狗與露", time.time(), wake_intent=None)

    handle_gap_mock.assert_awaited_once()  # classifier 真的被叫到，即便 has_intent_signal=False
    assert captured == {}  # 分類成功 → 不落 intent_gate_silenced.jsonl
    vc._cancel_stale_prefetch.assert_called_once()


async def test_gap_record_dms_owner(monkeypatch):
    """8/18 第三輪：每筆 gap classifier 記錄（不論 UNKNOWN 與否）都該即時 DM owner，
    取代人工翻 log 的一次性流程。DM 走 fire-and-forget create_task，不能拖慢 pipeline。"""
    import asyncio
    import cogs.voice_controller as vcmod
    from intent_gap import IntentGapRecord

    vc, captured = _make_vc(monkeypatch, query_intent_signal=False, query_text="暫停播放")
    vc._shared_tier_router = MagicMock()
    vc._gap_logger = MagicMock()
    monkeypatch.setattr(vcmod, "_NEMOCLAW_OWNER_ID", 999)

    owner = MagicMock()
    owner.send = AsyncMock()
    vc.bot.get_user = MagicMock(return_value=owner)
    vc.bot.fetch_user = AsyncMock(return_value=owner)

    monkeypatch.setattr(vcmod, "make_groq_gap_classifier", lambda router: MagicMock())
    fake_rec = IntentGapRecord(
        utterance_id="u3", ts=time.time(), speaker="狗與露", mode="normal",
        raw_query="暫停播放", cleaned_query="暫停播放", intent_type="playback_control_pause",
        slots={}, nearest_agent="playback_control", nearest_distance=0.0,
        ack_text="收到！", acknowledged=True,
    )
    monkeypatch.setattr(vcmod, "handle_intent_gap", AsyncMock(return_value=fake_rec))

    await vc._process_queued_query("狗與露", time.time(), wake_intent=None)
    for _ in range(5):
        await asyncio.sleep(0)  # 排空 fire-and-forget DM task

    owner.send.assert_awaited_once()
    sent_text = owner.send.await_args.args[0]
    assert "狗與露" in sent_text
    assert "playback_control_pause" in sent_text


async def test_gap_classifier_unknown_then_falls_to_silenced_log(monkeypatch):
    """classifier 有跑但判 UNKNOWN，且 has_intent_signal=False → 才落 silenced log，
    且記錄要帶上 classifier 的判讀結果（gap_intent_type）方便事後比對。"""
    import cogs.voice_controller as vcmod
    from intent_gap import IntentGapRecord

    vc, captured = _make_vc(monkeypatch, query_intent_signal=False, query_text="呃")
    vc._shared_tier_router = MagicMock()
    vc._gap_logger = MagicMock()

    monkeypatch.setattr(vcmod, "make_groq_gap_classifier", lambda router: MagicMock())

    fake_rec = IntentGapRecord(
        utterance_id="u2", ts=time.time(), speaker="狗與露", mode="normal",
        raw_query="呃", cleaned_query="呃", intent_type="UNKNOWN",
        slots={}, nearest_agent=None, nearest_distance=None,
        ack_text=None, acknowledged=False,
    )
    monkeypatch.setattr(vcmod, "handle_intent_gap", AsyncMock(return_value=fake_rec))

    await vc._process_queued_query("狗與露", time.time(), wake_intent=None)

    assert captured.get("path") == "records/intent_gate_silenced.jsonl"
    assert captured["record"]["gap_intent_type"] == "UNKNOWN"
