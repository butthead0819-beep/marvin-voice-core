"""依據近期聊天室內容動態產生押韻招呼語測試：

1. 有近期 transcripts 或話題時：優先呼叫 LLM 動態生成押韻對句（使用 player_greeting_rhyme 指令）。
2. 命中後寫入 1 小時快取，短時間重複進場不重複消耗 LLM。
3. 保證叫到名字：若 LLM 輸出漏了名字，自動補上前綴。
4. 無近期紀錄時：熟面孔優雅降級回退到 PERSONAL_GREETINGS 經典台詞。
5. LLM 例外/失敗時：優雅降級回退到 PERSONAL_GREETINGS 或預設台詞。
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gemini_router_content import PERSONAL_GREETINGS, GeminiRouterContentMixin


def _make_mixin():
    inst = GeminiRouterContentMixin.__new__(GeminiRouterContentMixin)
    inst._greeting_cache = {}
    inst._farewell_cache = {}
    inst.dna = {}
    inst.memory = MagicMock()
    inst.memory.get_player_memory.return_value = {}
    inst.temp_toxicity_override = None
    inst.prompt_manager = MagicMock()
    inst.prompt_manager.get_instruction.return_value = "[rhyme system prompt]"
    inst._call_llm = AsyncMock(return_value="天靈靈，地靈靈，拜請 showay 來通靈！")
    return inst


@pytest.mark.asyncio
async def test_player_with_recent_transcripts_triggers_rhyme_greeting_llm():
    mixin = _make_mixin()
    mock_store = MagicMock()
    mock_store.get_recent.return_value = [
        {"speaker": "showay", "text": "我一向是用通靈在修東西的", "timestamp": time.time() - 100},
        {"speaker": "showay", "text": "以後我要叫通靈王", "timestamp": time.time() - 50},
    ]

    with patch("transcript_store.TranscriptStore", return_value=mock_store):
        msg = await mixin.generate_player_greeting("showay", guild_id=123)

    assert "showay" in msg
    assert "通靈" in msg
    # 驗證呼叫了 player_greeting_rhyme 指令
    mixin.prompt_manager.get_instruction.assert_called_with(
        "player_greeting_rhyme",
        dna=mixin.dna,
        speaker="showay",
        memory_manager=mixin.memory,
        temp_toxicity_override=mixin.temp_toxicity_override,
    )
    # 驗證 user_prompt 包含近期通靈對話
    call_args = mixin._call_llm.call_args[0]
    user_prompt = call_args[1]
    assert "通靈" in user_prompt
    assert mixin._greeting_cache["showay"][1] == msg


@pytest.mark.asyncio
async def test_rhyme_greeting_cache_hit():
    mixin = _make_mixin()
    now = time.time()
    mixin._greeting_cache["showay"] = (now, "快取招呼句：拜請 showay！")

    msg = await mixin.generate_player_greeting("showay")
    assert msg == "快取招呼句：拜請 showay！"
    mixin._call_llm.assert_not_awaited()


@pytest.mark.asyncio
async def test_player_without_recent_transcripts_falls_back_to_personal_greetings():
    mixin = _make_mixin()
    mock_store = MagicMock()
    mock_store.get_recent.return_value = []  # 無近期紀錄

    with patch("transcript_store.TranscriptStore", return_value=mock_store):
        msg = await mixin.generate_player_greeting("showay")

    # 無近期紀錄時，熟面孔回退到 PERSONAL_GREETINGS
    assert msg == PERSONAL_GREETINGS["showay"]
    mixin._call_llm.assert_not_awaited()


@pytest.mark.asyncio
async def test_llm_failure_falls_back_to_personal_greetings():
    mixin = _make_mixin()
    mock_store = MagicMock()
    mock_store.get_recent.return_value = [
        {"speaker": "showay", "text": "通靈中", "timestamp": time.time() - 100},
    ]
    mixin._call_llm = AsyncMock(side_effect=RuntimeError("LLM 爆炸"))

    with patch("transcript_store.TranscriptStore", return_value=mock_store):
        msg = await mixin.generate_player_greeting("showay")

    # LLM 失敗時，熟面孔回退到 PERSONAL_GREETINGS
    assert msg == PERSONAL_GREETINGS["showay"]


@pytest.mark.asyncio
async def test_rhyme_greeting_ensures_player_name():
    mixin = _make_mixin()
    mock_store = MagicMock()
    mock_store.get_recent.return_value = [
        {"speaker": "阿明", "text": "今天修車修好久", "timestamp": time.time() - 100},
    ]
    # LLM 回傳了一句押韻但忘了放名字的句子
    mixin._call_llm = AsyncMock(return_value="一指點下天下平，通靈修車鬼神驚！")

    with patch("transcript_store.TranscriptStore", return_value=mock_store):
        msg = await mixin.generate_player_greeting("阿明")

    # 必須確保名字出現在句子中
    assert "阿明" in msg


@pytest.mark.asyncio
async def test_rhyme_greeting_multiline_and_note_cleanup():
    mixin = _make_mixin()
    mock_store = MagicMock()
    mock_store.get_recent.return_value = [
        {"speaker": "Showay", "text": "通靈抓短路", "timestamp": time.time() - 100},
    ]
    # LLM 回傳了換行多句，且文末有 (Note: ...) 備註
    raw_llm_output = (
        "Showay 登場真有一套\n"
        "拆電路板通靈技術高\n"
        "今天又要去哪裡鬧？\n"
        "(Note: Each sentence ends with the same rhyme.)"
    )
    mixin._call_llm = AsyncMock(return_value=raw_llm_output)

    with patch("transcript_store.TranscriptStore", return_value=mock_store):
        msg = await mixin.generate_player_greeting("Showay")

    # 驗證 Note 被清理，多行被正確拼接
    assert "(Note" not in msg
    assert "Showay 登場真有一套" in msg
    assert "拆電路板通靈技術高" in msg
    assert "今天又要去哪裡鬧" in msg
    assert len(msg) <= 35

