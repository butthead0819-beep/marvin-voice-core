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


@pytest.mark.asyncio
async def test_run_grounded_qa_source_defaults_to_ambient_qa(monkeypatch):
    """source 沒傳時預設 ambient_qa（既有呼叫點如 GroundedQAAgent handler 行為不變）。"""
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

    await gqa.run_grounded_qa(ctrl, "showay", "生命宇宙的答案")
    await __import__("asyncio").sleep(0)
    assert recorded[0]["source"] == "ambient_qa"


@pytest.mark.asyncio
async def test_run_grounded_qa_source_passthrough(monkeypatch):
    """新呼叫點（NowPlaying 追問補充）傳 source="music_followup" 要被記錄下來，
    讓 records/ambient_qa.jsonl 之後分得出來這筆不是 GroundedQAAgent regex 觸發的。"""
    ctrl = MagicMock()
    ctrl._play_ack = AsyncMock()
    ctrl.play_tts = AsyncMock()
    ctrl.active_text_channel.send = AsyncMock()
    ctrl.stt_logger = MagicMock()
    ctrl._ambient_qa_guard = _guard()
    monkeypatch.setattr(gqa, "grounded_answer",
                        AsyncMock(return_value=("2003 年。", ["example.com"])))
    recorded = []
    monkeypatch.setattr(gqa, "record_ambient_qa", lambda r: recorded.append(r))

    await gqa.run_grounded_qa(ctrl, "showay", "歌曲《夜曲》哪一年的",
                              raw="這是哪一年的", source="music_followup")
    await __import__("asyncio").sleep(0)
    assert recorded[0]["source"] == "music_followup"
    assert recorded[0]["answer"] == "2003 年。"


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


# ── 任意問題（廣義問句）與 10 分鐘 STT 背景注入測試 ────────────────────────

@pytest.mark.parametrize("raw", [
    "馬文 請問為什麼今天台北一直下雨",
    "馬文 我想知道黑神話悟空評價好不好",
    "馬文 幫我搜尋南港展覽館附近牛肉麵",
    "馬文 想問台積電今天跌多少",
    "馬文 找一下這附近有哪幾間咖啡廳",
    "馬文 你知不知道大谷翔平今天第幾轟",
])
def test_parse_lookup_verb_questions_hit(raw):
    """明確查詢動詞（請問/想知道/搜尋/找一下…）帶出的問題要命中；
    沒有動詞的寬版疑問詞/句尾嗎呢不觸發（閒聊誤觸，見 docstring 收斂版）。"""
    got = parse_grounded_qa(raw)
    assert got is not None, f"{raw!r} 應命中 Grounded QA"


@pytest.mark.parametrize("raw", [
    "馬文你覺得今天會贏嗎",
    "馬文你覺得這款遊戲好玩嗎",
    "馬文你好帥喔",
    "馬文你在做什麼",
    "馬文你會眨眼嗎",
    "馬文放首告五人的歌",
    "馬文音量大一點",
    "馬文這是哪一首歌",
])
def test_parse_arbitrary_questions_excludes_banter_and_other_agents(raw):
    assert parse_grounded_qa(raw) is None, f"{raw!r} 應被排除（由人格聊天或特化 Agent 接手）"


@pytest.mark.asyncio
async def test_build_recent_transcript_context_success():
    from intent_agents.grounded_qa_agent import build_recent_transcript_context

    ctrl = MagicMock()
    mock_store = MagicMock()
    mock_store.get_recent.return_value = [
        {"speaker": "Jack", "text": "昨天看那款黑神話悟空好帥", "timestamp": 100.0},
        {"speaker": "Suki", "text": "真的假的？", "timestamp": 105.0},
        {"speaker": "Jack", "text": "嗯", "timestamp": 106.0},  # 太短被過濾
        {"speaker": "Suki", "text": "我電腦配備不知道跑不跑得動", "timestamp": 110.0},
    ]
    ctrl._transcript_store = mock_store
    ctrl.active_text_channel = MagicMock()
    ctrl.active_text_channel.guild.id = 12345

    ctx_text = await build_recent_transcript_context(ctrl, minutes=10)
    assert "Jack: 昨天看那款黑神話悟空好帥" in ctx_text
    assert "Suki: 我電腦配備不知道跑不跑得動" in ctx_text
    # 長度小於 2 的「嗯」被過濾
    assert "Jack: 嗯\n" not in ctx_text
    mock_store.get_recent.assert_called_once_with(guild_id=12345, minutes=10)


@pytest.mark.asyncio
async def test_build_recent_transcript_context_store_missing_or_error():
    from intent_agents.grounded_qa_agent import build_recent_transcript_context

    ctrl = MagicMock()
    ctrl._transcript_store = None
    assert await build_recent_transcript_context(ctrl, minutes=10) == ""

    ctrl._transcript_store = MagicMock()
    ctrl._transcript_store.get_recent.side_effect = RuntimeError("DB locked")
    assert await build_recent_transcript_context(ctrl, minutes=10) == ""


@pytest.mark.asyncio
async def test_grounded_answer_with_recent_context_injects_prompt():
    free = _client(_resp("黑神話在 Steam 售價是 1280 元。"))
    guard = _guard()
    context = "Jack: 昨天看那個黑神話悟空好帥\nSuki: 不知道多少錢"
    out = await grounded_answer(
        free, None, guard, "那款遊戲多少錢？", recent_context=context
    )
    assert out is not None
    ans, sources = out
    assert "1280" in ans

    # 驗證傳遞給模型的 contents 包含背景與當前提問
    call_kwargs = free.aio.models.generate_content.call_args.kwargs
    contents = call_kwargs["contents"]
    assert "【過去 10 分鐘語音對話背景" in contents
    assert "黑神話悟空好帥" in contents
    assert "【使用者當前提問】" in contents
    assert "那款遊戲多少錢？" in contents


@pytest.mark.asyncio
async def test_run_grounded_qa_fetches_and_passes_stt_context(monkeypatch):
    ctrl = MagicMock()
    ctrl._play_ack = AsyncMock()
    ctrl.play_tts = AsyncMock()
    ctrl.active_text_channel.send = AsyncMock()
    ctrl.stt_logger = MagicMock()
    ctrl._ambient_qa_guard = _guard()
    ctrl.active_text_channel.guild.id = 999

    mock_store = MagicMock()
    mock_store.get_recent.return_value = [
        {"speaker": "Jack", "text": "台北今天天氣如何", "timestamp": 100.0}
    ]
    ctrl._transcript_store = mock_store

    called_context = {}

    async def _mock_grounded_answer(free, paid, guard, query, *, recent_context="", **kwargs):
        called_context["query"] = query
        called_context["recent_context"] = recent_context
        return ("今天台北晴天降雨機率 10%。", ["cwb.gov.tw"])

    monkeypatch.setattr(gqa, "grounded_answer", _mock_grounded_answer)
    recorded = []
    monkeypatch.setattr(gqa, "record_ambient_qa", lambda r: recorded.append(r))

    await gqa.run_grounded_qa(ctrl, "showay", "台北明天天氣怎樣", raw="馬文 台北明天天氣怎樣")
    await __import__("asyncio").sleep(0)

    assert called_context["query"] == "台北明天天氣怎樣"
    assert "Jack: 台北今天天氣如何" in called_context["recent_context"]
    assert recorded and recorded[0]["answer"] == "今天台北晴天降雨機率 10%。"



# ── code review 回歸：寬版疑問詞搶閒聊 / 你字開頭查詢被 _SELF_RE 吃掉 ──────────

@pytest.mark.parametrize("raw", [
    "馬文我比較喜歡周杰倫",
    "馬文今天晚餐吃什麼",
    "馬文我帥嗎",
    "馬文閉嘴好嗎",
    "馬文好無聊喔有沒有人要打遊戲",
])
def test_parse_chat_with_question_words_not_grounded(raw):
    assert parse_grounded_qa(raw) is None, f"閒聊 {raw!r} 不該被搶去 Google 查"


@pytest.mark.parametrize("raw", [
    "馬文你幫我查一下台積電股價",
    "馬文你查一下明天天氣",
    "馬文妳幫我查一下颱風動態",
])
def test_parse_lookup_starting_with_ni_hits(raw):
    assert parse_grounded_qa(raw) is not None, f"{raw!r} 是明確查詢，不該被當成問 Marvin 自身"


@pytest.mark.asyncio
async def test_grounded_default_system_prompt_unchanged_and_overridable():
    """grounded_answer 加 system_prompt/caller kwarg 給 audiophile_fetcher 重用；
    不傳時 AmbientQA 行為不變。"""
    from intent_agents.grounded_qa_agent import _SYSTEM_PROMPT

    free = _client(_resp("答案。"))
    await grounded_answer(free, None, _guard(), "某個問題")
    assert free.aio.models.generate_content.await_args.kwargs["config"].system_instruction == _SYSTEM_PROMPT

    free2 = _client(_resp("答案。"))
    await grounded_answer(free2, None, _guard(), "某個問題", system_prompt="自訂")
    assert free2.aio.models.generate_content.await_args.kwargs["config"].system_instruction == "自訂"
