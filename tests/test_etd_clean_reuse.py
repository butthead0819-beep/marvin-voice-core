"""etd_clean_reuse：worker 尾段 cleaner（Commit 1：搬移，行為不變）+ Semantic ETD 結果重用（Commit 2）"""
from __future__ import annotations

import asyncio
import types
from unittest.mock import AsyncMock

import pytest

from etd_clean_reuse import clean_for_worker, remember_etd, take_etd


def _strip_wake(text: str) -> str:
    """假 _strip_wake_word：去掉開頭「馬文，」或「馬文」。"""
    t = text.strip()
    for w in ("馬文，", "馬文"):
        if t.startswith(w):
            return t[len(w):].strip()
    return t


def _make_ctrl(clean_stt_text=None, has_router=True):
    router = types.SimpleNamespace(clean_stt_text=clean_stt_text) if clean_stt_text else None
    if has_router:
        bot = types.SimpleNamespace(router=router or types.SimpleNamespace(clean_stt_text=AsyncMock()))
    else:
        bot = types.SimpleNamespace()
    return types.SimpleNamespace(bot=bot, _CONFIRM_CLEAN_TIMEOUT=2.5, _strip_wake_word=_strip_wake)


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


# ── Commit 2：remember_etd / take_etd ───────────────────────────────────────

def test_remember_then_take_hit():
    ctrl = _make_ctrl()
    raw = "馬文，今天天氣如何"
    res = {"text": "馬文，今天天氣如何？"}
    remember_etd(ctrl, "alice", raw, res)
    assert take_etd(ctrl, "alice", "今天天氣如何") == "今天天氣如何？"


def test_take_etd_only_once():
    ctrl = _make_ctrl()
    raw = "馬文，今天天氣如何"
    remember_etd(ctrl, "alice", raw, {"text": "馬文，今天天氣如何？"})
    assert take_etd(ctrl, "alice", "今天天氣如何") == "今天天氣如何？"
    assert take_etd(ctrl, "alice", "今天天氣如何") is None


def test_take_etd_expired_after_ttl():
    ctrl = _make_ctrl()
    raw = "馬文，今天天氣如何"
    remember_etd(ctrl, "alice", raw, {"text": "馬文，今天天氣如何？"}, now=1000.0)
    assert take_etd(ctrl, "alice", "今天天氣如何", now=1031.0) is None


def test_take_etd_mismatch_returns_none():
    ctrl = _make_ctrl()
    remember_etd(ctrl, "alice", "馬文，今天天氣如何", {"text": "馬文，今天天氣如何？"})
    assert take_etd(ctrl, "alice", "明天天氣如何") is None


def test_remember_etd_skips_non_dict_res():
    ctrl = _make_ctrl()
    remember_etd(ctrl, "alice", "馬文，今天天氣如何", "not a dict")
    assert take_etd(ctrl, "alice", "今天天氣如何") is None


def test_remember_etd_skips_empty_text():
    ctrl = _make_ctrl()
    remember_etd(ctrl, "alice", "馬文，今天天氣如何", {"text": ""})
    assert take_etd(ctrl, "alice", "今天天氣如何") is None


def test_remember_etd_skips_no_speaker():
    ctrl = _make_ctrl()
    remember_etd(ctrl, None, "馬文，今天天氣如何", {"text": "馬文，今天天氣如何？"})
    assert take_etd(ctrl, None, "今天天氣如何") is None


def test_remember_etd_same_speaker_keeps_latest_only():
    ctrl = _make_ctrl()
    remember_etd(ctrl, "alice", "馬文，今天天氣如何", {"text": "馬文，今天天氣如何？"})
    remember_etd(ctrl, "alice", "馬文，播放音樂", {"text": "馬文，播放音樂吧"})
    assert take_etd(ctrl, "alice", "今天天氣如何") is None
    assert take_etd(ctrl, "alice", "播放音樂") == "播放音樂吧"


@pytest.mark.asyncio
async def test_clean_for_worker_reuses_etd_cache_without_calling_router():
    mock = AsyncMock()
    ctrl = _make_ctrl(clean_stt_text=mock)
    remember_etd(ctrl, "alice", "馬文，播放音樂", {"text": "馬文，播放音樂吧"})
    result = await clean_for_worker(ctrl, "播放音樂", speaker="alice")
    assert result == "播放音樂吧"
    mock.assert_not_called()


@pytest.mark.asyncio
async def test_clean_for_worker_calls_router_on_cache_miss():
    mock = AsyncMock(return_value={"text": "清過"})
    ctrl = _make_ctrl(clean_stt_text=mock)
    result = await clean_for_worker(ctrl, "播放音樂", speaker="alice")
    assert result == "清過"
    mock.assert_called_once_with("播放音樂")


@pytest.mark.asyncio
async def test_clean_for_worker_reuse_disabled_by_env(monkeypatch):
    monkeypatch.setenv("MARVIN_ETD_CLEAN_REUSE", "0")
    mock = AsyncMock(return_value={"text": "清過"})
    ctrl = _make_ctrl(clean_stt_text=mock)
    remember_etd(ctrl, "alice", "馬文，播放音樂", {"text": "馬文，播放音樂吧"})
    result = await clean_for_worker(ctrl, "播放音樂", speaker="alice")
    assert result == "清過"
    mock.assert_called_once_with("播放音樂")


@pytest.mark.asyncio
async def test_clean_for_worker_no_speaker_calls_router():
    mock = AsyncMock(return_value={"text": "清過"})
    ctrl = _make_ctrl(clean_stt_text=mock)
    remember_etd(ctrl, None, "馬文，播放音樂", {"text": "馬文，播放音樂吧"})
    result = await clean_for_worker(ctrl, "播放音樂")
    assert result == "清過"
    mock.assert_called_once_with("播放音樂")


@pytest.mark.asyncio
async def test_apply_semantic_etd_populates_reusable_cache():
    from cogs.voice_controller import VoiceController

    clean_stt_text = AsyncMock(return_value={"text": "馬文，播放音樂吧", "is_complete": True})
    router = types.SimpleNamespace(clean_stt_text=clean_stt_text)
    bot = types.SimpleNamespace(router=router)
    ctrl = types.SimpleNamespace(
        bot=bot,
        user_sentence_buffer={},
        _strip_wake_word=_strip_wake,
    )

    result = await VoiceController._apply_semantic_etd(
        ctrl, "alice", "馬文，播放音樂吧。", 0.0, None, b"", None,
    )

    assert result is not None
    assert take_etd(ctrl, "alice", "播放音樂吧。") == "播放音樂吧"
