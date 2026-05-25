"""TDD — search_lyrics_grounded：Gemini + Google Search grounding，雙層驗證。

驗證契約（避免「LLM 拿 STT 垃圾片段也自信編一首歌」）：
  L1: response.text 開頭「無」→ 拒（Gemini 自承找不到）
  L2: response.candidates[0].grounding_metadata.grounding_chunks 非空 → 必要
       （chunks 空 = Gemini 根本沒搜到網頁就在編）

兩條都過才回「藝人 - 歌名」。LRC 只負責 timestamp 裝飾，不再守門（搜尋庫太窄）。
"""
from __future__ import annotations

import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from intent_agents.lyrics_grounded_search import search_lyrics_grounded


def _client(
    text: str,
    *,
    grounding_chunks: list | None = None,
):
    """組 mock client。預設給一個非空 chunk → 通過 L2 驗證。

    grounding_chunks=[] 模擬 Gemini 沒實際搜到任何頁。
    grounding_chunks=None 模擬 SDK 完全沒回 grounding_metadata（舊版 / 無 grounding）。
    """
    if grounding_chunks is None:
        candidate = SimpleNamespace(grounding_metadata=None)
    else:
        gm = SimpleNamespace(grounding_chunks=grounding_chunks)
        candidate = SimpleNamespace(grounding_metadata=gm)
    resp = SimpleNamespace(text=text, candidates=[candidate])
    client = MagicMock()
    client.aio = MagicMock()
    client.aio.models = MagicMock()
    client.aio.models.generate_content = AsyncMock(return_value=resp)
    return client


def _ok_chunk(uri: str = "https://mojim.com/qa.htm"):
    return SimpleNamespace(uri=uri, title="example")


def _client_raising(exc: Exception):
    client = MagicMock()
    client.aio = MagicMock()
    client.aio.models = MagicMock()
    client.aio.models.generate_content = AsyncMock(side_effect=exc)
    return client


# ── happy path ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_returns_artist_dash_title_when_grounded_with_chunks():
    client = _client("周杰倫 - 七里香", grounding_chunks=[_ok_chunk()])
    result = await search_lyrics_grounded(client, "雨下整夜")
    assert result == "周杰倫 - 七里香"


@pytest.mark.asyncio
async def test_strips_surrounding_whitespace():
    client = _client("   周杰倫 - 七里香   \n", grounding_chunks=[_ok_chunk()])
    result = await search_lyrics_grounded(client, "雨下整夜")
    assert result == "周杰倫 - 七里香"


@pytest.mark.asyncio
async def test_keeps_only_first_line():
    """response.text 多行時只取第一行（即使 Gemini 多嘴附評論）。"""
    client = _client("周杰倫 - 七里香\n（這首歌很經典...）", grounding_chunks=[_ok_chunk()])
    result = await search_lyrics_grounded(client, "雨下整夜")
    assert result == "周杰倫 - 七里香"


# ── L1: 「無」拒絕 ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_returns_none_when_llm_says_no():
    client = _client("無", grounding_chunks=[_ok_chunk()])
    result = await search_lyrics_grounded(client, "完全不存在的片段")
    assert result is None


@pytest.mark.asyncio
async def test_returns_none_when_response_starts_with_no():
    client = _client("無法識別", grounding_chunks=[_ok_chunk()])
    result = await search_lyrics_grounded(client, "片段")
    assert result is None


# ── L2: grounding_chunks 必須非空 ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rejects_when_grounding_chunks_empty():
    """關鍵：Gemini 給了答案但 grounding_chunks=[] → 它沒真的搜到，是在編 → 拒。

    這正是要擋的 case：STT 給垃圾「升旗白馬過三觀」，Google 搜不到，Gemini 仍胡謅
    「張學友 - 將軍令」。改造後此 case 因 chunks 空被擋下。
    """
    client = _client("張學友 - 將軍令", grounding_chunks=[])
    result = await search_lyrics_grounded(client, "升旗白馬過三觀")
    assert result is None


@pytest.mark.asyncio
async def test_rejects_when_grounding_metadata_missing():
    """SDK 沒回 grounding_metadata（grounding 沒實際運作）→ 也拒。"""
    client = _client("張學友 - 將軍令", grounding_chunks=None)
    result = await search_lyrics_grounded(client, "升旗白馬過三觀")
    assert result is None


@pytest.mark.asyncio
async def test_accepts_when_multiple_chunks_present():
    chunks = [_ok_chunk("https://mojim.com/a"), _ok_chunk("https://kkbox.com/b")]
    client = _client("周杰倫 - 青花瓷", grounding_chunks=chunks)
    result = await search_lyrics_grounded(client, "天青色等煙雨")
    assert result == "周杰倫 - 青花瓷"


# ── 空輸入 / 空 response ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_returns_none_on_empty_response():
    client = _client("", grounding_chunks=[_ok_chunk()])
    result = await search_lyrics_grounded(client, "片段")
    assert result is None


@pytest.mark.asyncio
async def test_returns_none_on_whitespace_only_response():
    client = _client("   \n\n  ", grounding_chunks=[_ok_chunk()])
    result = await search_lyrics_grounded(client, "片段")
    assert result is None


# ── 輸入端防呆 ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_returns_none_for_empty_fragment():
    client = _client("不該被呼叫 - 不該被呼叫", grounding_chunks=[_ok_chunk()])
    result = await search_lyrics_grounded(client, "")
    assert result is None
    client.aio.models.generate_content.assert_not_awaited()


@pytest.mark.asyncio
async def test_returns_none_for_whitespace_fragment():
    client = _client("不該被呼叫 - 不該被呼叫", grounding_chunks=[_ok_chunk()])
    result = await search_lyrics_grounded(client, "   ")
    assert result is None
    client.aio.models.generate_content.assert_not_awaited()


@pytest.mark.asyncio
async def test_returns_none_when_client_is_none():
    result = await search_lyrics_grounded(None, "雨下整夜")
    assert result is None


# ── 例外容錯 ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_returns_none_on_exception():
    client = _client_raising(RuntimeError("503 service unavailable"))
    result = await search_lyrics_grounded(client, "雨下整夜")
    assert result is None


@pytest.mark.asyncio
async def test_returns_none_on_timeout():
    import asyncio
    client = _client_raising(asyncio.TimeoutError())
    result = await search_lyrics_grounded(client, "雨下整夜")
    assert result is None


# ── Search grounding 真的有被啟用（regression）──────────────────────────────

@pytest.mark.asyncio
async def test_passes_google_search_tool_in_config():
    client = _client("周杰倫 - 七里香", grounding_chunks=[_ok_chunk()])
    await search_lyrics_grounded(client, "雨下整夜")

    call = client.aio.models.generate_content.await_args
    assert call is not None
    config = call.kwargs.get("config")
    assert config is not None
    tools = getattr(config, "tools", None) or []
    assert tools
    assert any(getattr(t, "google_search", None) is not None for t in tools)


@pytest.mark.asyncio
async def test_passes_fragment_into_prompt():
    client = _client("周杰倫 - 七里香", grounding_chunks=[_ok_chunk()])
    await search_lyrics_grounded(client, "雨下整夜我的愛溢出")

    call = client.aio.models.generate_content.await_args
    contents = call.kwargs.get("contents") or (call.args[1] if len(call.args) > 1 else "")
    assert "雨下整夜我的愛溢出" in str(contents)


@pytest.mark.asyncio
async def test_prompt_acknowledges_stt_phonetic_errors():
    """關鍵 regression：prompt 必須允許 Gemini 做同音字校正。

    沒這條 → STT 把「身騎白馬過三關」聽成「升旗白馬過三觀」會永遠卡住。
    要點：prompt 要明說「STT 可能錯誤」+「依拼音相近猜原句」。
    """
    client = _client("徐佳瑩 - 身騎白馬", grounding_chunks=[_ok_chunk()])
    await search_lyrics_grounded(client, "升旗白馬過三觀")

    call = client.aio.models.generate_content.await_args
    contents = str(call.kwargs.get("contents") or "")
    # 必須提到 STT 跟拼音/同音/近音任一概念
    assert "STT" in contents or "語音" in contents, "prompt 應提到輸入來自 STT"
    assert any(k in contents for k in ["拼音", "同音", "近音", "phonetic", "homophone"]), \
        "prompt 應提到拼音/同音字校正"
