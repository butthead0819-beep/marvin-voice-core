"""etd_clean_reuse.clean_for_worker（Commit 1：搬移，行為不變）"""
from __future__ import annotations

import asyncio
import types
from unittest.mock import AsyncMock

import pytest

from etd_clean_reuse import clean_for_worker


def _make_ctrl(clean_stt_text=None, has_router=True):
    router = types.SimpleNamespace(clean_stt_text=clean_stt_text) if clean_stt_text else None
    if has_router:
        bot = types.SimpleNamespace(router=router or types.SimpleNamespace(clean_stt_text=AsyncMock()))
    else:
        bot = types.SimpleNamespace()
    return types.SimpleNamespace(bot=bot, _CONFIRM_CLEAN_TIMEOUT=2.5)


@pytest.mark.asyncio
async def test_router_returns_cleaned_text():
    mock = AsyncMock(return_value={"text": "清過"})
    ctrl = _make_ctrl(clean_stt_text=mock)
    result = await clean_for_worker(ctrl, "原始文字")
    assert result == "清過"
    mock.assert_called_once_with("原始文字")


@pytest.mark.asyncio
async def test_router_raises_falls_back_to_raw():
    mock = AsyncMock(side_effect=RuntimeError("boom"))
    ctrl = _make_ctrl(clean_stt_text=mock)
    result = await clean_for_worker(ctrl, "原始文字")
    assert result == "原始文字"


@pytest.mark.asyncio
async def test_router_timeout_falls_back_to_raw():
    mock = AsyncMock(side_effect=asyncio.TimeoutError())
    ctrl = _make_ctrl(clean_stt_text=mock)
    result = await clean_for_worker(ctrl, "原始文字")
    assert result == "原始文字"


@pytest.mark.asyncio
async def test_router_returns_non_dict_falls_back_to_raw():
    mock = AsyncMock(return_value="x")
    ctrl = _make_ctrl(clean_stt_text=mock)
    result = await clean_for_worker(ctrl, "原始文字")
    assert result == "原始文字"


@pytest.mark.asyncio
async def test_no_router_falls_back_to_raw():
    ctrl = _make_ctrl(has_router=False)
    result = await clean_for_worker(ctrl, "原始文字")
    assert result == "原始文字"


@pytest.mark.asyncio
async def test_router_returns_empty_text_falls_back_to_raw():
    mock = AsyncMock(return_value={"text": ""})
    ctrl = _make_ctrl(clean_stt_text=mock)
    result = await clean_for_worker(ctrl, "原始文字")
    assert result == "原始文字"
