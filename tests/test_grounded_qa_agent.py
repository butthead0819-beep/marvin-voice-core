"""GroundedQAAgent — AmbientQA：喚醒 + 明確事實問句 → grounded 回答（design AmbientQA-20260830）。

驗證（對齊 CLAUDE.md IntentBus 測試骨架）：
  - parse_grounded_qa 純規則：查動詞 / 事實問句尾命中；點歌 / 找歌 / 音量 / 問 Marvin 自身 → None
  - bid：命中 0.75 + handler；未命中 dense 0.0；輸真點歌 / find-song 的 CRITICAL
  - gate：low_confidence_wake → dense 0.0
  - mode gate：game → mode_mismatch
  - grounded_answer（mock client）：free 先用 / 429→paid + guard.record / guard 到頂→None
    / L1 拒（空、「無」、拒答前綴）/ L2 拒（grounding_chunks 空）/ model chain fallback
  - handler：呼叫 ctrl._handle_grounded_qa(speaker, topic, raw=...)
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import intent_agents.grounded_qa_agent as gqa
from intent_agents.grounded_qa_agent import (
    GroundedQAAgent, grounded_answer, parse_grounded_qa,
)
from intent_bus import IntentContext


def _ctx(raw, speaker="showay", mode="normal", wake_intent=0.9, low_conf=False,
         dispatch_source="regex"):
    return IntentContext(
        speaker=speaker, raw_text=raw, query=raw, original_raw=raw,
        wake_intent=wake_intent, stream_active=(mode == "stream"),
        game_mode=(mode == "game"), is_owner=False, now=0.0, mode=mode,
        low_confidence_wake=low_conf, dispatch_source=dispatch_source,
    )


def _agent():
    ctrl = MagicMock()
    return GroundedQAAgent(ctrl), ctrl


# ── parse_grounded_qa 純規則 ─────────────────────────────────────────────────

@pytest.mark.parametrize("raw,topic", [
    ("馬文 0722 的酒是什麼", "0722 的酒"),
    ("馬文幫我查什麼叫生綠塔", "什麼叫生綠塔"),
    ("馬文賣快的鑽石機怎麼做", "賣快的鑽石機"),
    ("馬文查一下臺北101幾樓", "臺北101幾樓"),
    ("馬文珠穆朗瑪峰有多高", "珠穆朗瑪峰有多高"),
])
def test_parse_hits(raw, topic):
    got = parse_grounded_qa(raw)
    assert got is not None, f"{raw!r} 應命中"


@pytest.mark.parametrize("raw", [
    "馬文放五月天",              # 點歌
    "馬文播放張惠妹的歌",         # 點歌
    "馬文搜尋後來我終於學會了如何去愛",  # 點歌（裸搜尋）
    "馬文這首歌是誰唱的",         # find-song / lyrics（搶 search_lyrics_grounded）
    "馬文這是什麼歌",            # find-song
    "馬文大聲一點",             # 音量
    "馬文音量調小",             # 音量
    "馬文你好嗎",               # 問 Marvin 自身
    "馬文你在播歌嗎",           # 問 Marvin 自身狀態
    "馬文你會眨眼嗎",           # 問 Marvin 自身
    "馬文今天天氣不錯",         # 沒有問句標記
    "馬文嗎",                   # 太短
])
def test_parse_misses(raw):
    assert parse_grounded_qa(raw) is None, f"{raw!r} 不該命中"


# ── bid ─────────────────────────────────────────────────────────────────────

def test_bid_hits_confidence():
    agent, _ = _agent()
    bid = agent.bid(_ctx("馬文 0722 的酒是什麼"))
    assert bid.confidence == 0.75
    assert bid.handler is not None
    assert bid.reason == "ambient_qa"


def test_bid_no_match_dense_zero():
    agent, _ = _agent()
    bid = agent.bid(_ctx("馬文放五月天"))
    assert bid.confidence == 0.0
    assert bid.reason == "no_match"


def test_bid_low_confidence_wake_gated():
    agent, _ = _agent()
    bid = agent.bid(_ctx("馬文 0722 的酒是什麼", low_conf=True))
    assert bid.confidence == 0.0
    assert bid.reason == "low_confidence_wake"


def test_bid_game_mode_mismatch():
    agent, _ = _agent()
    bid = agent.bid(_ctx("馬文 0722 的酒是什麼", mode="game"))
    assert bid.confidence == 0.0
    assert bid.reason == "mode_mismatch:game"


def test_bid_loses_to_real_music_request():
    """CRITICAL：真點歌（MusicAgentV2 0.80–0.95）必須贏過 grounded_qa（0.75）。"""
    agent, _ = _agent()
    # grounded_qa 對真點歌句根本不出價（parse 排除）→ 自然輸
    assert agent.bid(_ctx("馬文播放告五人的愛人錯過")).confidence == 0.0
    # 即使勉強帶問句標記，confidence 0.75 < MusicAgentV2 marker 0.80
    assert GroundedQAAgent.declare_intents(agent)[0].confidence < 0.80


def test_factual_question_declares_topic_slot():
    """audio-rescue manifest 靠 required_slots 把 topic 曝成 Gemini function 參數。"""
    agent, _ = _agent()
    schema = agent.declare_intents()[0]
    assert schema.name == "factual_question"
    assert schema.required_slots == ["topic"]


def test_regex_bid_fills_topic_no_missing_slots():
    agent, _ = _agent()
    bid = agent.bid(_ctx("馬文 0722 的酒是什麼"))
    assert bid.confidence == 0.75
    assert bid.missing_slots == []


# ── audio-rescue 路徑（LLM 聽糊字音訊 → 填 topic slot）──────────────────────

def test_resolve_intent_audio_rescue_uses_llm_topic():
    """糊掉的 ctx.query + low_confidence_wake，但 LLM 從音訊給了乾淨 topic → 出價。"""
    agent, _ = _agent()
    ctx = _ctx("馬文 淩淩期 九 三 觀", low_conf=True, dispatch_source="llm_rescue_audio")
    bid = agent.resolve_intent("factual_question", {"topic": "0722 的酒是什麼"}, ctx)
    assert bid is not None
    assert bid.confidence == 0.75
    assert "audio_rescue" in bid.reason


def test_resolve_intent_audio_rescue_rejects_empty_topic():
    agent, _ = _agent()
    ctx = _ctx("糊掉的東西", dispatch_source="llm_rescue_audio")
    assert agent.resolve_intent("factual_question", {}, ctx) is None
    assert agent.resolve_intent("factual_question", {"topic": "  "}, ctx) is None


@pytest.mark.asyncio
async def test_resolve_intent_audio_rescue_handler_uses_llm_topic(monkeypatch):
    agent, _ = _agent()
    seen = {}

    async def _fake(c, speaker, topic, *, raw=""):
        seen.update(speaker=speaker, topic=topic, raw=raw)

    monkeypatch.setattr(gqa, "run_grounded_qa", _fake)
    ctx = _ctx("馬文 亂碼 亂碼", speaker="大肚", low_conf=True,
               dispatch_source="llm_rescue_audio")
    bid = agent.resolve_intent("factual_question", {"topic": "珠穆朗瑪峰多高"}, ctx)
    await bid.handler()
    assert seen["topic"] == "珠穆朗瑪峰多高"   # 用 LLM 的 topic，不是糊掉的 ctx.query
    assert seen["speaker"] == "大肚"


@pytest.mark.asyncio
async def test_handler_runs_grounded_qa(monkeypatch):
    agent, ctrl = _agent()
    seen = {}

    async def _fake(c, speaker, topic, *, raw=""):
        seen.update(ctrl=c, speaker=speaker, topic=topic, raw=raw)

    monkeypatch.setattr(gqa, "run_grounded_qa", _fake)
    bid = agent.bid(_ctx("馬文 0722 的酒是什麼", speaker="狗與露"))
    await bid.handler()
    assert seen["speaker"] == "狗與露"
    assert "0722" in seen["topic"]
    assert seen["raw"] == "馬文 0722 的酒是什麼"


@pytest.mark.asyncio
async def test_run_grounded_qa_speaks_answer_and_posts(monkeypatch):
    ctrl = MagicMock()
    ctrl._play_ack = AsyncMock()
    ctrl.play_tts = AsyncMock()
    ctrl.active_text_channel.send = AsyncMock()
    ctrl.stt_logger = MagicMock()
    ctrl._ambient_qa_guard = _guard()
    monkeypatch.setattr(gqa, "grounded_answer",
                        AsyncMock(return_value=("答案是 42。", ["example.com"])))
    recorded = []
    monkeypatch.setattr(gqa, "record_ambient_qa", lambda r: recorded.append(r))

    await gqa.run_grounded_qa(ctrl, "showay", "生命宇宙的答案", raw="馬文查生命宇宙的答案")
    await __import__("asyncio").sleep(0)
    ctrl._play_ack.assert_awaited_once()
    ctrl.play_tts.assert_awaited_once()
    assert "答案是 42" in ctrl.play_tts.call_args.args[0]
    assert recorded and recorded[0]["answer"] == "答案是 42。"


@pytest.mark.asyncio
async def test_run_grounded_qa_no_answer_fallback(monkeypatch):
    ctrl = MagicMock()
    ctrl._play_ack = AsyncMock()
    ctrl.play_tts = AsyncMock()
    ctrl.stt_logger = MagicMock()
    ctrl._ambient_qa_guard = _guard()
    monkeypatch.setattr(gqa, "grounded_answer", AsyncMock(return_value=None))
    recorded = []
    monkeypatch.setattr(gqa, "record_ambient_qa", lambda r: recorded.append(r))

    await gqa.run_grounded_qa(ctrl, "showay", "查不到的東西")
    await __import__("asyncio").sleep(0)
    assert "查不到" in ctrl.play_tts.call_args.args[0]
    assert recorded[0]["answer"] is None


# ── grounded_answer ─────────────────────────────────────────────────────────

def _resp(text, *, chunks=1, finish="STOP"):
    chunk_objs = [SimpleNamespace(web=SimpleNamespace(uri="https://example.com/a"))
                  for _ in range(chunks)]
    gm = SimpleNamespace(grounding_chunks=chunk_objs)
    cand = SimpleNamespace(grounding_metadata=gm, finish_reason=finish)
    return SimpleNamespace(
        text=text, candidates=[cand],
        usage_metadata=SimpleNamespace(prompt_token_count=100, candidates_token_count=50),
    )


def _client(resp=None, exc=None):
    cli = MagicMock()
    if exc is not None:
        cli.aio.models.generate_content = AsyncMock(side_effect=exc)
    else:
        cli.aio.models.generate_content = AsyncMock(return_value=resp)
    return cli


def _guard(allow=True):
    g = MagicMock()
    g.allow.return_value = allow
    g.record = MagicMock()
    return g


@pytest.mark.asyncio
async def test_grounded_free_client_used_first():
    free = _client(_resp("珠穆朗瑪峰高 8848 公尺。"))
    paid = _client(_resp("不該被呼叫"))
    guard = _guard()
    out = await grounded_answer(free, paid, guard, "珠穆朗瑪峰多高")
    assert out is not None
    ans, sources = out
    assert "8848" in ans
    assert sources == ["example.com"]
    free.aio.models.generate_content.assert_awaited_once()
    paid.aio.models.generate_content.assert_not_awaited()
    guard.record.assert_not_called()  # 免費不記帳


@pytest.mark.asyncio
async def test_grounded_falls_back_to_paid_and_records():
    free = _client(exc=RuntimeError("RESOURCE_EXHAUSTED"))
    paid = _client(_resp("答案在這。"))
    guard = _guard(allow=True)
    out = await grounded_answer(free, paid, guard, "某個問題")
    assert out is not None
    guard.record.assert_called_once()
    assert guard.record.call_args.kwargs["caller"] == "ambient_qa"


@pytest.mark.asyncio
async def test_grounded_guard_capped_no_paid():
    free = _client(exc=RuntimeError("boom"))
    paid = _client(_resp("不該被呼叫"))
    guard = _guard(allow=False)
    out = await grounded_answer(free, paid, guard, "某個問題")
    assert out is None
    paid.aio.models.generate_content.assert_not_awaited()


@pytest.mark.asyncio
async def test_grounded_l1_refusal_rejected():
    for txt in ("", "無", "抱歉，我查不到這個資訊。"):
        free = _client(_resp(txt))
        out = await grounded_answer(free, None, _guard(), "某個問題")
        assert out is None, f"{txt!r} 應被 L1 擋"


@pytest.mark.asyncio
async def test_grounded_l2_empty_chunks_rejected():
    free = _client(_resp("看起來很有自信的答案", chunks=0))
    out = await grounded_answer(free, None, _guard(), "某個問題")
    assert out is None


@pytest.mark.asyncio
async def test_grounded_model_chain_fallback():
    calls = []

    async def _gen(*, model, contents, config):
        calls.append(model)
        if len(calls) < 2:
            raise RuntimeError("first model 404")
        return _resp("第二顆 model 答的。")

    paid = MagicMock()
    paid.aio.models.generate_content = _gen
    out = await grounded_answer(None, paid, _guard(), "某個問題")
    assert out is not None
    assert len(calls) == 2
