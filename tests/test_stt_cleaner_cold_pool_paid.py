"""stt_cleaner.py 冷池直連付費（Plan A）— 免費 quick 池全冷卻時跳過免費 cascade，
直接打付費 Gemini flash-lite，砍掉壞日子 429 級聯的等待。

觸發條件：quick_pool.next_available() is None（全冷卻/TPM/daily 皆滿）+ 有付費 client
+ env MARVIN_CLEANER_COLD_PAID != "0"。付費路徑強制 guard.allow() 前檢查 + record() 記帳
（付費鐵則 feedback_paid_calls_must_record）。付費不可用（超 cap / RPM 滿 / 無 client）→
落回免費 cascade 盡力，最終 raw。
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from stt_cleaner import GeminiRouterSTTMixin


@pytest.fixture(autouse=True)
def _isolate_and_default_flag(tmp_path, monkeypatch):
    monkeypatch.setattr("stt_cleaner._LOCAL_CORRECTIONS_PATH", tmp_path / "noop_local.json")
    monkeypatch.setattr("stt_cleaner._CORRECTIONS_LOG", tmp_path / "noop_jsonl.jsonl")
    monkeypatch.delenv("MARVIN_CLEANER_COLD_PAID", raising=False)  # 預設 ON
    yield


def _clean_json(cleaned="馬文，播放音樂", intent=1.0, calling=True, is_complete=True):
    return json.dumps({"cleaned": cleaned, "intent": intent,
                       "calling": calling, "is_complete": is_complete}, ensure_ascii=False)


def _fake_paid_response(text=None):
    resp = MagicMock()
    resp.text = text if text is not None else _clean_json()
    resp.usage_metadata = MagicMock(prompt_token_count=120, candidates_token_count=40)
    return resp


def _make_router(*, quick_cold: bool, quick_ret=None, analyze_ret=None,
                 paid_text=None, has_paid_client=True, guard_allow=True):
    """帶 Mixin 的假 router。quick_cold=True → quick_pool.next_available() 回 None。"""
    class _R(GeminiRouterSTTMixin):
        pass
    r = _R()
    r.wake_fusion = None
    r.prompt_manager = MagicMock()
    r.prompt_manager.get_instruction = MagicMock(return_value="SYS_PROMPT")
    r.cleaner_model = "gemini-3.1-flash-lite-preview"

    # 付費 client
    if has_paid_client:
        client = MagicMock()
        client.aio.models.generate_content = AsyncMock(return_value=_fake_paid_response(paid_text))
        r.google_cleaner_client = client
    else:
        r.google_cleaner_client = None
        client = None
    r._try_acquire_cleaner_rpm_slot = MagicMock(return_value=True)

    # 付費 guard（allow/record 可觀測）
    guard = MagicMock()
    guard.allow = MagicMock(return_value=guard_allow)
    guard.record = MagicMock()
    r._get_paid_guard = MagicMock(return_value=guard)

    # 免費池 router：quick_pool.next_available() 決定冷/熱
    rt = MagicMock()
    rt.quick = AsyncMock(return_value=quick_ret)
    rt.analyze = AsyncMock(return_value=analyze_ret)
    qp = MagicMock()
    qp.next_available = MagicMock(return_value=None if quick_cold else MagicMock())
    rt.quick_pool = qp
    r._stt_router = rt
    return r, rt, client, guard


@pytest.mark.asyncio
async def test_cold_quick_pool_skips_free_and_uses_paid():
    """quick 池全冷卻 → 不打免費 quick/analyze，直接付費 Gemini，用其結果。"""
    r, rt, client, _ = _make_router(quick_cold=True, paid_text=_clean_json("馬文，播放周杰倫"))
    res = await r.clean_stt_text("馬文播放周杰倫", speaker="大肚")
    assert res["text"] == "馬文，播放周杰倫"
    rt.quick.assert_not_awaited()
    rt.analyze.assert_not_awaited()
    client.aio.models.generate_content.assert_awaited_once()


@pytest.mark.asyncio
async def test_warm_quick_pool_keeps_free_first():
    """quick 池有可用 endpoint → 照舊免費優先，不先打付費。"""
    r, rt, client, _ = _make_router(quick_cold=False, quick_ret=_clean_json("馬文，播放音樂"))
    res = await r.clean_stt_text("馬文播放音樂", speaker="大肚")
    assert res["text"] == "馬文，播放音樂"
    rt.quick.assert_awaited_once()
    client.aio.models.generate_content.assert_not_awaited()


@pytest.mark.asyncio
async def test_cold_pool_records_paid_usage():
    """冷池付費成功 → guard.record 記帳（caller=stt_cleaner + in/out 分開）。"""
    r, rt, client, guard = _make_router(quick_cold=True)
    await r.clean_stt_text("馬文播放音樂", speaker="大肚")
    guard.allow.assert_called()          # 呼叫前有檢查
    guard.record.assert_called_once()
    kw = guard.record.call_args.kwargs
    assert kw["caller"] == "stt_cleaner"
    assert kw["in_tokens"] == 120 and kw["out_tokens"] == 40


@pytest.mark.asyncio
async def test_cold_pool_guard_denies_no_paid_call():
    """冷池但 guard 超 cap → 不打付費，落回免費 cascade（本例 quick/analyze None）→ raw。"""
    r, rt, client, guard = _make_router(quick_cold=True, guard_allow=False,
                                        quick_ret=None, analyze_ret=None)
    res = await r.clean_stt_text("馬文播放音樂", speaker="大肚")
    client.aio.models.generate_content.assert_not_awaited()
    guard.record.assert_not_called()
    assert res["text"] == "馬文播放音樂"   # 降級 raw


@pytest.mark.asyncio
async def test_flag_off_keeps_old_free_first(monkeypatch):
    """MARVIN_CLEANER_COLD_PAID=0 → 即使冷池也照舊免費優先。"""
    monkeypatch.setenv("MARVIN_CLEANER_COLD_PAID", "0")
    r, rt, client, _ = _make_router(quick_cold=True, quick_ret=_clean_json("馬文，播放音樂"))
    await r.clean_stt_text("馬文播放音樂", speaker="大肚")
    rt.quick.assert_awaited_once()
    client.aio.models.generate_content.assert_not_awaited()


@pytest.mark.asyncio
async def test_cold_pool_no_paid_client_falls_to_free():
    """冷池但無付費 client → 不能直連，落回免費 cascade。"""
    r, rt, client, _ = _make_router(quick_cold=True, has_paid_client=False,
                                    quick_ret=_clean_json("馬文，播放音樂"))
    res = await r.clean_stt_text("馬文播放音樂", speaker="大肚")
    rt.quick.assert_awaited_once()
    assert res["text"] == "馬文，播放音樂"
