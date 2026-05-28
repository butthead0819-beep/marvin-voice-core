"""gap_classifier — cheap LLM 把「沒命中 agent 的 query」分類成 gap record。

設計：
- 沿用 TieredLLMRouter.quick（同 chat_classifier_judge 的設施）
- 回傳：intent_type / slots / nearest_agent / nearest_distance / ack_text
- failure：router 回 None → safe default UNKNOWN；JSON 壞 → raise（caller 寫 UNKNOWN gap）
- ack_text 由 LLM 同一次 call 產（使用者拍板「用 LLM 產生」）
"""
import json
from unittest.mock import AsyncMock

import pytest

from intent_gap import make_groq_gap_classifier


def _manifest():
    return {
        "version": "2026-05-27",
        "agents": [
            {
                "name": "music_v2",
                "intents": [
                    {"name": "play_song", "required_slots": ["song_choice"], "reason_template": "play_song:{song_choice}"},
                    {"name": "skip", "required_slots": [], "reason_template": "skip"},
                ],
            },
        ],
    }


def _router_returning(payload: dict | None):
    router = AsyncMock()
    router.quick = AsyncMock(return_value=None if payload is None else json.dumps(payload))
    return router


@pytest.mark.asyncio
async def test_gap_classifier_returns_parsed_payload_on_happy_path():
    expected = {
        "intent_type": "replay_user_history",
        "slots": {"target_user": "showay"},
        "nearest_agent": "music_v2",
        "nearest_distance": 0.45,
        "ack_text": "想播 showay 點過的歌，這個我還沒會。",
    }
    router = _router_returning(expected)
    classify = make_groq_gap_classifier(router)

    result = await classify("播 showay 點過的歌", _manifest())

    assert result == expected


@pytest.mark.asyncio
async def test_gap_classifier_returns_safe_default_when_router_pool_exhausted():
    """router.quick 回 None（pool 全冷）→ safe default UNKNOWN，caller 不會炸。"""
    router = _router_returning(None)
    classify = make_groq_gap_classifier(router)

    result = await classify("播 showay 點過的歌", _manifest())

    assert result["intent_type"] == "UNKNOWN"
    assert result["slots"] == {}
    assert result["nearest_agent"] is None
    assert result["nearest_distance"] is None
    assert result["ack_text"] is None


@pytest.mark.asyncio
async def test_gap_classifier_raises_on_malformed_json():
    """LLM 回非 JSON → raise，讓 caller 寫 UNKNOWN gap（拍板 #5）。"""
    router = AsyncMock()
    router.quick = AsyncMock(return_value="this is not json")
    classify = make_groq_gap_classifier(router)

    with pytest.raises(json.JSONDecodeError):
        await classify("播 showay 點過的歌", _manifest())


@pytest.mark.asyncio
async def test_gap_classifier_passes_manifest_into_prompt():
    """prompt 必須帶入 manifest，否則 LLM 看不到 agent 能力地圖。"""
    router = _router_returning({
        "intent_type": "UNKNOWN", "slots": {}, "nearest_agent": None,
        "nearest_distance": None, "ack_text": None,
    })
    classify = make_groq_gap_classifier(router)

    await classify("播 showay 點過的歌", _manifest())

    router.quick.assert_called_once()
    kwargs = router.quick.call_args.kwargs
    assert "music_v2" in kwargs["prompt"]
    assert "play_song" in kwargs["prompt"]
    assert "播 showay 點過的歌" in kwargs["prompt"]


@pytest.mark.asyncio
async def test_gap_classifier_uses_json_mode_and_temperature_zero():
    """對齊 chat_classifier 慣例：json=True, temperature=0.0, caller 標籤可識別。"""
    router = _router_returning({
        "intent_type": "UNKNOWN", "slots": {}, "nearest_agent": None,
        "nearest_distance": None, "ack_text": None,
    })
    classify = make_groq_gap_classifier(router)

    await classify("hi", _manifest())

    kwargs = router.quick.call_args.kwargs
    assert kwargs["json"] is True
    assert kwargs["temperature"] == 0.0
    assert kwargs["caller"] == "gap_classifier"


@pytest.mark.asyncio
async def test_gap_classifier_unknown_intent_returned_by_llm_is_passed_through():
    """LLM 自己判定 UNKNOWN（雜訊 / 反問）→ 透傳，不強行 ack。"""
    payload = {
        "intent_type": "UNKNOWN",
        "slots": {},
        "nearest_agent": None,
        "nearest_distance": None,
        "ack_text": None,
    }
    router = _router_returning(payload)
    classify = make_groq_gap_classifier(router)

    result = await classify("嗯啊", _manifest())

    assert result["intent_type"] == "UNKNOWN"
    assert result["ack_text"] is None
