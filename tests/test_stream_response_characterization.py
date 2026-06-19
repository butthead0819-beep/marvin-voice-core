"""
特徵測試（characterization）：鎖定 _process_queued_query 渲染後半（句子串流 →
TTS/貼文/各種閘 → 收尾）的當前行為。

目的：在把渲染後半抽成 _stream_response 之前，先用這組測試固定「行為現狀」。
抽方法（純搬、不改邏輯）後，這組測試必須維持全綠 —— 這就是「行為不變」的硬標準。

手法：把 routing 前半的 13 個分流閘全部 stub 成 fall-through，再用替換掉的
_stream_sentence_splitter 精確餵入受控句流，斷言渲染後半的可觀察輸出
（play_tts / active_text_channel.send / placeholder.edit）。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


async def _agen(items):
    for it in items:
        if isinstance(it, Exception):
            raise it
        yield it


def _make_vc(monkeypatch, sentences, *, wake_intent=None, is_helper=False,
             stream_mode=False, low_conf_sentences=()):
    """建一個只跑得動渲染後半的 VoiceController：routing 全 fall-through。"""
    from cogs.voice_controller import VoiceController
    import cogs.voice_controller as vcmod

    # ── routing 前半：模組級閘函式全部中性化 → fall-through 到渲染 ──
    monkeypatch.setattr(vcmod, "is_helper_wake", lambda *a, **k: is_helper)
    monkeypatch.setattr(vcmod, "has_intent_signal", lambda q: True)
    monkeypatch.setattr(vcmod, "is_personal_assistant_query", lambda q: False)
    monkeypatch.setattr(vcmod, "detect_imitation_target", lambda q, players: None)
    monkeypatch.setattr(vcmod, "is_manual_add_query", lambda q: False)
    monkeypatch.setattr(vcmod, "is_task_update_query", lambda q: False)
    monkeypatch.setattr(vcmod, "is_mark_done_query", lambda q: False)
    monkeypatch.setattr(vcmod, "is_recall_query", lambda q: False)

    vc = VoiceController.__new__(VoiceController)

    # bot / router
    vc.bot = MagicMock()
    vc.bot.cogs.get.return_value = None          # 關掉 MusicCog 委派 → stream_mode 走 local
    vc.bot.vision_enabled = False
    vc.bot.router._background_intent_enrich = AsyncMock()
    vc.bot.router._pending_prefetch = {}
    vc.bot.router.memory.list_players.return_value = []
    vc.bot.router.wake_fusion = MagicMock()
    vc.bot.engine.conv_buffer.get_harvest.return_value = "今天天氣如何呢"
    vc.bot.engine.conv_buffer.get_last_n_utterances.return_value = []
    vc.bot.router.stream_fast_response = MagicMock(return_value=object())

    # 狀態
    vc._stream_mode_local = stream_mode
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

    # mocks
    vc.stt_logger = MagicMock()
    vc._ducking_agent = MagicMock()
    vc._ducking_agent.wake_threshold_boost.return_value = 0.0
    vc._room_mood_store = MagicMock()
    vc._room_mood_store.get.return_value.hot_chat = False
    vc._latency_marks = MagicMock()
    vc._latency_marks.mark_first_sentence.return_value = None
    vc._intent_bus = MagicMock()
    vc._intent_bus.dispatch = AsyncMock(return_value=None)

    # active text channel + placeholder
    placeholder = MagicMock()
    placeholder.edit = AsyncMock()
    placeholder.delete = AsyncMock()
    channel = MagicMock()
    channel.send = AsyncMock(return_value=placeholder)
    channel.guild.id = 1
    channel.id = 2
    vc.active_text_channel = channel
    vc._placeholder = placeholder  # 方便測試讀

    # self 方法 stub
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
    vc._is_low_confidence_answer = MagicMock(
        side_effect=lambda s: s in low_conf_sentences)

    # 串流接縫：bypass stream_fast_response，直接餵受控句流
    vc._cot_filter_stream = lambda s: s
    vc._stream_sentence_splitter = lambda _stream: _agen(list(sentences))

    return vc, placeholder, channel


async def _run(vc, *, wake_intent=None):
    import asyncio
    import time
    # wake_time 用近期值，避免觸發 Late Skip（_elapsed > 25s 會放棄回應）
    await vc._process_queued_query("陳進文", time.time(), wake_intent=wake_intent)
    # 排空 fire-and-forget 任務（play_tts / speak 走 create_task）
    for _ in range(5):
        await asyncio.sleep(0)


# ── 1. 正常多句 → 每句一次 play_tts + placeholder 編輯成全文 ──────────────────
@pytest.mark.asyncio
async def test_normal_multi_sentence_plays_each_and_edits_placeholder(monkeypatch):
    vc, ph, ch = _make_vc(monkeypatch, ["你好。", "今天很糟。"])
    await _run(vc)
    spoken = [c.args[0] for c in vc.play_tts.call_args_list]
    assert spoken == ["你好。", "今天很糟。"]
    # placeholder 最終為全文
    final = ph.edit.call_args_list[-1].kwargs.get("content", "")
    assert "你好。今天很糟。" in final


# ── 2. 首句含 [SKIP] → 只貼文字、零 TTS ──────────────────────────────────────
@pytest.mark.asyncio
async def test_skip_signal_first_posts_text_no_tts(monkeypatch):
    vc, ph, ch = _make_vc(monkeypatch, ["[SKIP]"])
    await _run(vc)
    vc.play_tts.assert_not_called()
    final = ph.edit.call_args_list[-1].kwargs.get("content", "")
    assert "聽不懂" in final


# ── 3. 首句低信心 → 只貼文字、零 TTS ─────────────────────────────────────────
@pytest.mark.asyncio
async def test_low_confidence_first_sentence_posts_text_no_tts(monkeypatch):
    vc, ph, ch = _make_vc(monkeypatch, ["我不確定耶"], low_conf_sentences=("我不確定耶",))
    await _run(vc)
    vc.play_tts.assert_not_called()
    final = ph.edit.call_args_list[-1].kwargs.get("content", "")
    assert "聽不懂" in final


# ── 4. tts_suppressed（wake_intent<0.80）→ 不播、但貼字 ──────────────────────
@pytest.mark.asyncio
async def test_tts_suppressed_posts_text_but_no_audio(monkeypatch):
    vc, ph, ch = _make_vc(monkeypatch, ["你好。", "再見。"], wake_intent=0.5)
    await _run(vc, wake_intent=0.5)
    vc.play_tts.assert_not_called()
    final = ph.edit.call_args_list[-1].kwargs.get("content", "")
    assert "你好。再見。" in final


# ── 5. helper query → 串流期不逐句念，整段後走 play_tts(protected) ────────────
@pytest.mark.asyncio
async def test_helper_query_defers_then_speaks_once(monkeypatch):
    vc, ph, ch = _make_vc(monkeypatch, ["這是", "答案。"], is_helper=True)
    await _run(vc)
    # 串流期間不逐句念，收完整段後只有一次發聲
    assert vc.play_tts.call_count == 1
    assert vc.play_tts.call_args.kwargs.get("protected") is True


# ── 6. stream_mode 短答 → 收完整段走 speak() ─────────────────────────────────
@pytest.mark.asyncio
async def test_stream_mode_speaks_full_text_via_speak(monkeypatch):
    vc, ph, ch = _make_vc(monkeypatch, ["短答。"], stream_mode=True)
    await _run(vc)
    vc.speak.assert_awaited_once()
    assert vc.speak.call_args.args[0] == "短答。"


# ── 7. 整段弱答 → 替換成 in-character 台詞 ────────────────────────────────────
@pytest.mark.asyncio
async def test_weak_full_text_replaced_with_in_character(monkeypatch):
    # 短且含弱答 pattern，但首句不被低信心 gate 攔（low_conf 空）→ 進 weak filter
    vc, ph, ch = _make_vc(monkeypatch, ["不知道"])
    await _run(vc)
    # 最終貼文不應只是原弱答
    final = ph.edit.call_args_list[-1].kwargs.get("content", "")
    assert "不知道" not in final or "馬文" in final


# ── 8. 串流拋例外 → 錯誤分類訊息 + finally 收手 ──────────────────────────────
@pytest.mark.asyncio
async def test_quota_error_classified_and_spoken(monkeypatch):
    vc, ph, ch = _make_vc(monkeypatch, [Exception("quota exceeded 429")])
    await _run(vc)
    # 首句都還沒到就炸 → 走錯誤 fallback play_tts
    vc.play_tts.assert_awaited_once()
    assert "配額" in vc.play_tts.call_args.args[0]


@pytest.mark.asyncio
async def test_generic_error_classified_and_spoken(monkeypatch):
    vc, ph, ch = _make_vc(monkeypatch, [Exception("boom")])
    await _run(vc)
    vc.play_tts.assert_awaited_once()
    assert "大腦" in vc.play_tts.call_args.args[0]
