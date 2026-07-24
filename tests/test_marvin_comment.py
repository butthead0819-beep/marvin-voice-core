"""
tests/test_marvin_comment.py
TDD：GET /marvin_comment — HUD 點 Marvin 卡片時，現生一句對目前畫面的銳評。

走既有 GeminiRouter bus（vc.bot.router._call_llm，跟 imitate/standup 同款一次性小生成），
不自己開 client；router 缺席或 LLM 呼叫失敗都要退回罐頭台詞，不讓卡片壞掉。
"""
import json

import pytest
from unittest.mock import AsyncMock, MagicMock

from main_satellite import build_marvin_comment_prompt, parse_other_cards_param


def _make_vc(router=None):
    vc = MagicMock()
    vc.handle_stt_result = AsyncMock()
    vc.bot.cogs.get.return_value = None
    vc.bot.router = router
    return vc


def test_prompt_includes_title_and_queue_when_playing():
    system_prompt, user_prompt = build_marvin_comment_prompt(
        playing=True, title="夜曲", by="周杰倫",
        queue=[{"title": "晴天"}, {"title": "七里香"}])
    assert "夜曲" in user_prompt
    assert "周杰倫" in user_prompt
    assert "晴天" in user_prompt and "七里香" in user_prompt
    assert "馬文" in system_prompt


def test_prompt_says_nothing_playing_when_not_playing():
    system_prompt, user_prompt = build_marvin_comment_prompt(playing=False)
    assert "沒歌在播" in user_prompt


def test_prompt_includes_other_cards_when_provided():
    """Marvin 要能對畫面上的其他卡片（非音樂）講評，不是只看得到自己在播的歌。"""
    _, user_prompt = build_marvin_comment_prompt(
        playing=False,
        other_cards=[{"label": "Claude Code", "text": "Discord-voice-bot 等你回應"},
                     {"label": "行事曆", "text": "設計評審 10:30"}])
    assert "Claude Code" in user_prompt
    assert "Discord-voice-bot 等你回應" in user_prompt
    assert "行事曆" in user_prompt
    assert "設計評審 10:30" in user_prompt


def test_prompt_omits_other_cards_section_when_absent():
    """沒傳 other_cards（例如舊版前端）→ 維持原本只講音樂的行為，不炸。"""
    _, user_prompt = build_marvin_comment_prompt(playing=True, title="夜曲")
    assert "同時還顯示著" not in user_prompt


def test_parse_other_cards_param_valid_json_round_trips():
    raw = json.dumps([{"label": "Claude Code", "text": "等你回應"}])
    assert parse_other_cards_param(raw) == [{"label": "Claude Code", "text": "等你回應"}]


def test_parse_other_cards_param_missing_or_invalid_returns_empty():
    assert parse_other_cards_param(None) == []
    assert parse_other_cards_param("") == []
    assert parse_other_cards_param("{not json") == []
    assert parse_other_cards_param('"just a string"') == []
    assert parse_other_cards_param("42") == []


def test_parse_other_cards_param_drops_non_dict_items_and_truncates():
    raw = json.dumps(["oops", {"label": "A", "text": "x"}, 123,
                       {"label": "B", "text": "y"}, {"label": "C", "text": "z"},
                       {"label": "D", "text": "w"}, {"label": "E", "text": "v"},
                       {"label": "F", "text": "u"}])
    out = parse_other_cards_param(raw)
    assert len(out) == 5   # 最多 5 張，防惡意超長 payload
    assert all(isinstance(o, dict) for o in out)


def test_parse_other_cards_param_truncates_long_label_and_text():
    raw = json.dumps([{"label": "L" * 100, "text": "T" * 200}])
    out = parse_other_cards_param(raw)
    assert len(out[0]["label"]) <= 20
    assert len(out[0]["text"]) <= 60


@pytest.mark.asyncio
async def test_marvin_comment_returns_llm_text(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app
    router = MagicMock()
    router._call_llm = AsyncMock(return_value="這首歌播了三次了，你是不是卡帶了？")
    vc = _make_vc(router=router)
    app = build_text_app(vc, token=None)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/marvin_comment")
        assert resp.status == 200
        body = await resp.json()
        assert body["comment"] == "這首歌播了三次了，你是不是卡帶了？"


@pytest.mark.asyncio
async def test_marvin_comment_passes_other_cards_from_query_to_prompt():
    """HUD 帶 ?cards= 快照過來 → 送進 LLM 的 user_prompt 要看得到那些卡片內容。"""
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app
    router = MagicMock()
    router._call_llm = AsyncMock(return_value="欸，你的 CI 又紅了。")
    vc = _make_vc(router=router)
    app = build_text_app(vc, token=None)
    cards = json.dumps([{"label": "Claude Code", "text": "Discord-voice-bot 等你回應"}])
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/marvin_comment?cards=" + cards)
        assert resp.status == 200
    sent_user_prompt = router._call_llm.call_args.args[1]
    assert "Claude Code" in sent_user_prompt
    assert "Discord-voice-bot 等你回應" in sent_user_prompt


@pytest.mark.asyncio
async def test_marvin_comment_falls_back_when_router_missing():
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app
    vc = _make_vc(router=None)
    app = build_text_app(vc, token=None)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/marvin_comment")
        body = await resp.json()
        assert body["comment"]  # 有罐頭台詞，不是空的/500


@pytest.mark.asyncio
async def test_marvin_comment_falls_back_when_llm_call_raises():
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app
    router = MagicMock()
    router._call_llm = AsyncMock(side_effect=RuntimeError("boom"))
    vc = _make_vc(router=router)
    app = build_text_app(vc, token=None)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/marvin_comment")
        assert resp.status == 200
        body = await resp.json()
        assert body["comment"]
