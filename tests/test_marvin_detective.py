"""TDD 測試：game/detective/marvin_detective.py"""
from __future__ import annotations

import json
import os
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch


# ─── Fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def set_env(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setenv("GROQ_SIMPLE_MODEL", "llama-3.1-8b-instant")


@pytest.fixture
def detective():
    from game.detective.marvin_detective import MarvinDetective
    return MarvinDetective()


# ─── Import 測試 ─────────────────────────────────────────────────────────────

def test_import_succeeds():
    from game.detective.marvin_detective import MarvinDetective
    assert MarvinDetective is not None


def test_no_discord_dependency():
    """marvin_detective 不能有任何 discord 相關 import"""
    import ast, pathlib
    src = pathlib.Path(
        "game/detective/marvin_detective.py"
    ).read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = (
                [n.name for n in node.names]
                if isinstance(node, ast.Import)
                else ([node.module] if node.module else [])
            )
            for name in names:
                assert name is None or "discord" not in name, (
                    f"marvin_detective.py 不應 import discord 相關模組，但發現: {name}"
                )


# ─── generate_vote ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_generate_vote_returns_tuple(detective):
    statements = {"a": "我喜歡貓", "b": "我每天跑步", "c": "我從沒睡過覺"}
    mock_resp = MagicMock()
    mock_resp.choices[0].message.content = "B。這句話最不可信，因為沒人不睡覺。"
    with patch.object(detective._client.chat.completions, "create", new=AsyncMock(return_value=mock_resp)):
        vote_index, quip = await detective.generate_vote(statements, "Alice")
    assert isinstance(vote_index, int)
    assert vote_index in (0, 1, 2)
    assert isinstance(quip, str)
    assert len(quip) > 0


@pytest.mark.asyncio
async def test_generate_vote_parses_A_as_0(detective):
    statements = {"a": "我喜歡貓", "b": "我每天跑步", "c": "我從沒睡過覺"}
    mock_resp = MagicMock()
    mock_resp.choices[0].message.content = "A。感覺很假。"
    with patch.object(detective._client.chat.completions, "create", new=AsyncMock(return_value=mock_resp)):
        vote_index, quip = await detective.generate_vote(statements, "Alice")
    assert vote_index == 0


@pytest.mark.asyncio
async def test_generate_vote_parses_C_as_2(detective):
    statements = {"a": "我喜歡貓", "b": "我每天跑步", "c": "我從沒睡過覺"}
    mock_resp = MagicMock()
    mock_resp.choices[0].message.content = "C，明顯是謊言。"
    with patch.object(detective._client.chat.completions, "create", new=AsyncMock(return_value=mock_resp)):
        vote_index, quip = await detective.generate_vote(statements, "Alice")
    assert vote_index == 2


@pytest.mark.asyncio
async def test_generate_vote_fallback_on_llm_error(detective):
    """LLM 拋例外時使用 fallback"""
    statements = {"a": "真的", "b": "也真的", "c": "假的"}
    with patch.object(detective._client.chat.completions, "create", new=AsyncMock(side_effect=Exception("boom"))):
        vote_index, quip = await detective.generate_vote(statements, "Bob")
    assert vote_index in (0, 1, 2)
    assert isinstance(quip, str)


@pytest.mark.asyncio
async def test_generate_vote_fallback_on_unparseable_response(detective):
    """LLM 回應無法解析出 A/B/C 時走 fallback"""
    statements = {"a": "真的", "b": "也真的", "c": "假的"}
    mock_resp = MagicMock()
    mock_resp.choices[0].message.content = "我不知道，隨便啦。"
    with patch.object(detective._client.chat.completions, "create", new=AsyncMock(return_value=mock_resp)):
        vote_index, quip = await detective.generate_vote(statements, "Bob")
    assert vote_index in (0, 1, 2)


# ─── generate_statements ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_generate_statements_returns_dict_with_required_keys(detective):
    player_names = ["Alice", "Bob", "Marvin"]
    llm_json = json.dumps({"a": "Alice 最愛說話", "b": "Bob 從不唱歌", "c": "Marvin 最喜歡被嘲諷", "lie": "C"})
    mock_resp = MagicMock()
    mock_resp.choices[0].message.content = llm_json
    with patch.object(detective._client.chat.completions, "create", new=AsyncMock(return_value=mock_resp)):
        result = await detective.generate_statements(player_names)
    assert set(result.keys()) == {"a", "b", "c", "lie_index"}
    assert isinstance(result["a"], str)
    assert isinstance(result["b"], str)
    assert isinstance(result["c"], str)
    assert result["lie_index"] in (0, 1, 2)


@pytest.mark.asyncio
async def test_generate_statements_parses_lie_A_as_0(detective):
    player_names = ["Alice", "Bob"]
    llm_json = json.dumps({"a": "說話多", "b": "愛唱歌", "c": "從不遲到", "lie": "A"})
    mock_resp = MagicMock()
    mock_resp.choices[0].message.content = llm_json
    with patch.object(detective._client.chat.completions, "create", new=AsyncMock(return_value=mock_resp)):
        result = await detective.generate_statements(player_names)
    assert result["lie_index"] == 0


@pytest.mark.asyncio
async def test_generate_statements_fallback_on_llm_error(detective):
    """LLM 失敗時使用 FALLBACK_STATEMENTS"""
    with patch.object(detective._client.chat.completions, "create", new=AsyncMock(side_effect=Exception("timeout"))):
        result = await detective.generate_statements(["Alice"])
    assert set(result.keys()) == {"a", "b", "c", "lie_index"}
    assert result["lie_index"] in (0, 1, 2)


@pytest.mark.asyncio
async def test_generate_statements_fallback_on_invalid_json(detective):
    """LLM 回應不是合法 JSON 時走 fallback"""
    mock_resp = MagicMock()
    mock_resp.choices[0].message.content = "這不是 JSON"
    with patch.object(detective._client.chat.completions, "create", new=AsyncMock(return_value=mock_resp)):
        result = await detective.generate_statements(["Alice"])
    assert set(result.keys()) == {"a", "b", "c", "lie_index"}


# ─── generate_reveal_quip ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_generate_reveal_quip_returns_str(detective):
    mock_resp = MagicMock()
    mock_resp.choices[0].message.content = "哈哈，你被騙了。"
    with patch.object(detective._client.chat.completions, "create", new=AsyncMock(return_value=mock_resp)):
        quip = await detective.generate_reveal_quip(correct=True, fooled_count=3, declarer_name="Alice")
    assert isinstance(quip, str)
    assert len(quip) > 0


@pytest.mark.asyncio
async def test_generate_reveal_quip_fallback_on_error(detective):
    """LLM 失敗時使用 FALLBACK_REVEAL_QUIPS"""
    from game.detective.marvin_detective import FALLBACK_REVEAL_QUIPS
    with patch.object(detective._client.chat.completions, "create", new=AsyncMock(side_effect=Exception("err"))):
        quip = await detective.generate_reveal_quip(correct=False, fooled_count=0, declarer_name="Bob")
    assert quip in FALLBACK_REVEAL_QUIPS


# ─── 無 Discord 依賴可直接初始化 ─────────────────────────────────────────────

def test_init_without_valid_api_key(monkeypatch):
    """GROQ_API_KEY 未設定時也能靜默初始化（不 raise）"""
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    from game.detective import marvin_detective
    import importlib
    importlib.reload(marvin_detective)
    md = marvin_detective.MarvinDetective()
    assert md is not None
