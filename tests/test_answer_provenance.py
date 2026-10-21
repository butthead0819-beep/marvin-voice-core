"""[BOT→] 回答來源標籤（10/21 使用者：文字頻道要看到回答用了哪個 LLM / 付不付費 / 品質怎麼來的）。"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import gemini_router_llm as grl
import intent_agents.grounded_qa_agent as gqa
from intent_agents.grounded_qa_agent import format_provenance, grounded_answer


# ── format_provenance ────────────────────────────────────────────────────────

def test_format_provenance_success():
    meta = {"model": "gemini-3.5-flash", "paid": True, "searches": 2}
    out = format_provenance(meta, 4200, 1, 3, [])
    assert "gemini-3.5-flash付費" in out
    assert "搜尋2次" in out
    assert "4.2s" in out
    assert "問答1輪/背景3句" in out
    assert ")" not in out


def test_format_provenance_shows_tried_chain():
    meta = {"tried": ["gemini-2.5-flash免費✗"], "model": "gemini-2.5-flash", "paid": True, "searches": 1}
    out = format_provenance(meta, 1000, 0, 0, [])
    assert out.startswith("gemini-2.5-flash免費✗ →")


def test_format_provenance_all_failed():
    out = format_provenance({}, 500, 0, 0, [])
    assert "全部失敗" in out


def test_format_provenance_includes_games():
    meta = {"model": "gemini-2.5-flash", "paid": False, "searches": 0}
    out = format_provenance(meta, 100, 0, 0, ["Grounded 2"])
    assert "遊戲Grounded 2" in out


# ── grounded_answer 填 meta ──────────────────────────────────────────────────

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
async def test_grounded_answer_meta_records_free_failure_then_paid_success():
    free = _client(exc=RuntimeError("RESOURCE_EXHAUSTED"))
    paid = _client(_resp("答案在這。"))
    meta: dict = {}
    out = await grounded_answer(free, paid, _guard(), "某個問題", meta=meta)
    assert out is not None
    assert meta["tried"] == ["gemini-2.5-flash免費✗"]
    assert meta["model"] == gqa.MODEL_CHAIN[0]
    assert meta["paid"] is True


@pytest.mark.asyncio
async def test_grounded_answer_meta_records_free_success():
    free = _client(_resp("珠穆朗瑪峰高 8848 公尺。"))
    meta: dict = {}
    out = await grounded_answer(free, None, _guard(), "珠穆朗瑪峰多高", meta=meta)
    assert out is not None
    assert meta["paid"] is False
    assert meta["model"] == "gemini-2.5-flash"


# ── run_grounded_qa 把 provenance 寫進 [BOT→] 行 ─────────────────────────────

@pytest.mark.asyncio
async def test_run_grounded_qa_bot_line_includes_model_provenance(monkeypatch):
    ctrl = MagicMock()
    ctrl._play_ack = AsyncMock()
    ctrl.play_tts = AsyncMock()
    ctrl.active_text_channel.send = AsyncMock()
    ctrl.stt_logger = MagicMock()
    ctrl._ambient_qa_guard = _guard()

    async def _fake_grounded_answer(free_client, paid_client, guard, query, *, meta=None, **kwargs):
        if meta is not None:
            meta.update(model="gemini-3.5-flash", paid=True, searches=1)
        return "答案是 42。", ["example.com"]

    monkeypatch.setattr(gqa, "grounded_answer", _fake_grounded_answer)
    monkeypatch.setattr(gqa, "record_ambient_qa", lambda r: None)

    await gqa.run_grounded_qa(ctrl, "A", "生命宇宙的答案", raw="馬文查生命宇宙的答案")
    import asyncio
    await asyncio.sleep(0)

    bot_lines = [c.args[0] for c in ctrl.stt_logger.info.call_args_list if c.args[0].startswith("[BOT→A]")]
    assert bot_lines, "應有一行 [BOT→A] log"
    assert "gemini-3.5-flash" in bot_lines[0]
    assert "付費" in bot_lines[0]


# ── GeminiRouterLLMMixin stream_model_label ─────────────────────────────────

def _make_fake_router(stream_cloud=None, stream_llm=None):
    fake = object.__new__(grl.GeminiRouterLLMMixin)
    fake.dna = {}
    fake._session_call_count = 0
    fake.prompt_manager = MagicMock()
    fake.prompt_manager.get_instruction.return_value = "system prompt"
    fake.memory = MagicMock()
    fake.current_game = None
    fake._intent_search_cache = {}
    fake.provider = "gemini"
    fake.is_exhausted = False
    fake.budget = MagicMock()
    fake.budget.is_circuit_open.return_value = False
    fake.cleaner_model = "flash-lite"
    fake.short_term_dialogue = []
    fake.last_stream_error_time = 0.0
    fake._maybe_advance_relationship = MagicMock()
    if stream_cloud is not None:
        fake._stream_cloud = stream_cloud
    if stream_llm is not None:
        fake.stream_llm = stream_llm
    return fake


async def _collect(fake, speaker="A", query="測試訊息"):
    out = []
    async for chunk in fake.stream_fast_response(speaker, query):
        out.append(chunk)
    return "".join(out)


def test_stream_model_label_defaults_to_unknown():
    fake = _make_fake_router()
    assert fake.stream_model_label("A") == "?"


@pytest.mark.asyncio
async def test_stream_fast_response_cloud_success_records_label():
    async def _cloud_ok(*a, **kw):
        yield "答案"

    fake = _make_fake_router(stream_cloud=_cloud_ok)
    await _collect(fake)
    assert fake.stream_model_label("A") == "gemini/" + fake.cleaner_model


@pytest.mark.asyncio
async def test_stream_fast_response_fallback_records_composite_label():
    async def _cloud_fail(*a, **kw):
        raise RuntimeError("503")
        yield  # pragma: no cover

    async def _fallback_llm(*a, speaker=None, **kw):
        fake._note_stream_model(speaker, "groq/m")
        yield "備援"

    fake = _make_fake_router(stream_cloud=_cloud_fail, stream_llm=_fallback_llm)
    await _collect(fake)
    assert fake.stream_model_label("A") == "雲端失敗→備援 groq/m"
