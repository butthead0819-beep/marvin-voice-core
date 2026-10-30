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
    # take_etd 取一次就刪（比對不符也刪），所以只用最新那句取一次：命中＝舊的被覆蓋掉了
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
        # B-2 經 WakeSessionMixin._etd_semantic_check（對談極速通道 F）；這裡模擬對談窗外 → 照舊問 Groq
        _etd_semantic_check=lambda spk, text: clean_stt_text(text),
    )

    result = await VoiceController._apply_semantic_etd(
        ctrl, "alice", "馬文，播放音樂吧。", 0.0, None, b"", None,
    )

    assert result is not None
    assert take_etd(ctrl, "alice", "播放音樂吧。") == "播放音樂吧"


# ── J3 Cleaner 結果重用 ────────────────────────────────────────────────────────

def test_remember_and_take_j3():
    from etd_clean_reuse import remember_j3, take_j3
    ctrl = _make_ctrl()
    remember_j3(ctrl, "alice", "馬文今天天氣真好", "今天天氣真好", cleaned="今天天氣真好", now=100.0)
    # raw_text 命中
    assert take_j3(ctrl, "alice", "馬文今天天氣真好", now=102.0) == "今天天氣真好"

    remember_j3(ctrl, "bob", "馬文今天天氣真好", "今天天氣真好", cleaned="今天天氣真好", now=100.0)
    # query / stripped 命中
    assert take_j3(ctrl, "bob", "今天天氣真好", now=102.0) == "今天天氣真好"

    # 超過 TTL (10s) 過期
    remember_j3(ctrl, "charlie", "馬文今天天氣真好", cleaned="今天天氣真好", now=100.0)
    assert take_j3(ctrl, "charlie", "馬文今天天氣真好", now=115.0) is None


@pytest.mark.asyncio
async def test_clean_for_worker_populates_j3_cache_for_both_keys():
    from etd_clean_reuse import take_j3
    mock = AsyncMock(return_value={"text": "今天天氣很好"})
    ctrl = _make_ctrl(clean_stt_text=mock)

    # worker 跑 clean_for_worker
    cleaned = await clean_for_worker(ctrl, "今天天氣很好吧", speaker="alice")
    assert cleaned == "今天天氣很好"
    mock.assert_called_once()

    # 隨後的 J3 take_j3 用 stripped 命中，不再需要問 cleaner
    reused = take_j3(ctrl, "alice", "今天天氣很好吧")
    assert reused == "今天天氣很好"


@pytest.mark.asyncio
async def test_real_cleaner_call_reuses_j3_cache():
    from etd_clean_reuse import remember_j3
    from intent_bus import IntentContext

    clean_mock = AsyncMock(return_value={"text": "SHOULD_NOT_BE_CALLED"})
    ctrl = _make_ctrl(clean_stt_text=clean_mock)
    remember_j3(ctrl, "alice", "馬文今天天氣如何", "今天天氣如何", cleaned="今天天氣如何")

    from cogs.voice_controller import VoiceController
    ctx = IntentContext(
        speaker="alice", raw_text="馬文今天天氣如何", query="今天天氣如何",
        original_raw="馬文今天天氣如何", wake_intent=0.9, stream_active=False,
        game_mode=False, is_owner=False, now=0.0, mode="normal",
    )

    # 模擬 real_cleaner_call 邏輯（透過 controller 實作）
    res = await VoiceController._run_real_cleaner_call(ctrl, ctx)
    assert res == "今天天氣如何"
    clean_mock.assert_not_called()

