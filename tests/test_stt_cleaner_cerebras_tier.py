"""stt_cleaner.py cleaner → 算力池（TieredLLMRouter）整合測試。

2026-05-21：cleaner 從硬編 Groq8b→Cerebras→Groq70b tier chain 遷移到 CooldownAwarePool
（多家 free-tier 自動分流 + 429 cooldown）。底層 failover/cooldown/TPM headroom 由
tests/test_llm_pool.py 覆蓋；本檔測 cleaner 與 router 的接合：

  router.quick(8b 池) → 升 router.analyze(70b 池) → Gemini/raw 兜底

以及契約（text / is_wake / wake_intent）與 validate 行為保持不變。
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from stt_cleaner import GeminiRouterSTTMixin


@pytest.fixture(autouse=True)
def _isolate_stt_corrections_files(tmp_path, monkeypatch):
    """patch 掉本地 corrections 讀寫路徑：避免走 fast-path 跳過 LLM、避免污染 prod。"""
    monkeypatch.setattr("stt_cleaner._LOCAL_CORRECTIONS_PATH", tmp_path / "noop_local.json")
    monkeypatch.setattr("stt_cleaner._CORRECTIONS_LOG", tmp_path / "noop_jsonl.jsonl")
    yield


def _clean_json(cleaned="馬文，播放音樂", intent=1.0, calling=True, is_complete=True):
    return json.dumps({"cleaned": cleaned, "intent": intent,
                       "calling": calling, "is_complete": is_complete}, ensure_ascii=False)


def _make_router(quick_ret=None, analyze_ret=None):
    """帶 Mixin 的假 router + 注入 fake TieredLLMRouter（quick/analyze 回固定值）。"""
    class _R(GeminiRouterSTTMixin):
        pass
    r = _R()
    r.wake_fusion = None
    r.prompt_manager = MagicMock()
    r.prompt_manager.get_instruction = MagicMock(return_value="SYS_PROMPT")
    r.google_cleaner_client = None   # 跳過 Gemini tier
    rt = MagicMock()
    rt.quick = AsyncMock(return_value=quick_ret)
    rt.analyze = AsyncMock(return_value=analyze_ret)
    r._stt_router = rt               # 預先注入 → _ensure_stt_router 跳過 build
    return r, rt


@pytest.mark.asyncio
async def test_quick_success_returns_cleaned_skips_analyze():
    """quick 池回有效 JSON → 用它，不升 analyze（happy path）。"""
    r, rt = _make_router(quick_ret=_clean_json(cleaned="馬文，播放音樂"))
    res = await r.clean_stt_text("馬文播放音樂", speaker="大肚")
    assert res["text"] == "馬文，播放音樂"
    rt.quick.assert_awaited_once()
    rt.analyze.assert_not_awaited()


@pytest.mark.asyncio
async def test_quick_exhausted_escalates_to_analyze():
    """quick 池全冷卻回 None → 升 analyze(70b) 並用其結果。"""
    r, rt = _make_router(quick_ret=None, analyze_ret=_clean_json(cleaned="馬文，播放周杰倫"))
    res = await r.clean_stt_text("馬文播放周杰倫", speaker="大肚")
    assert res["text"] == "馬文，播放周杰倫"
    rt.quick.assert_awaited_once()
    rt.analyze.assert_awaited_once()


@pytest.mark.asyncio
async def test_both_exhausted_falls_to_raw():
    """quick + analyze 都 None、無 Gemini → 降級 raw（不 crash）。"""
    r, rt = _make_router(quick_ret=None, analyze_ret=None)
    res = await r.clean_stt_text("馬文播放音樂", speaker="大肚")
    assert res["text"] == "馬文播放音樂"
    assert res["wake_intent"] is None


@pytest.mark.asyncio
async def test_validate_fail_returns_raw_no_escalate():
    """quick 回多行（吐脈絡）→ _validate_cleaned None → 直接降 raw，不升 analyze（語意同舊）。"""
    r, rt = _make_router(quick_ret="第一行\n第二行")
    res = await r.clean_stt_text("馬文播放音樂", speaker="大肚")
    assert res["text"] == "馬文播放音樂"
    rt.analyze.assert_not_awaited()


@pytest.mark.asyncio
async def test_quick_called_with_json_temp0_caller():
    """cleaner 呼叫 router.quick 必帶 json/temperature=0/max_tokens=200/caller/system。"""
    r, rt = _make_router(quick_ret=_clean_json())
    await r.clean_stt_text("馬文播放音樂", speaker="大肚")
    kw = rt.quick.await_args.kwargs
    assert kw["json"] is True
    assert kw["temperature"] == 0.0
    assert kw["max_tokens"] == 200
    assert kw["caller"] == "stt_cleaner"
    assert kw["system"] == "SYS_PROMPT"


@pytest.mark.asyncio
async def test_wake_intent_preserved_from_pool():
    """pool 回 intent=0.95 + raw 真含喚醒詞 → wake_intent 保留、不被 injection guard 清掉。"""
    r, rt = _make_router(quick_ret=_clean_json(cleaned="馬文", intent=0.95))
    res = await r.clean_stt_text("我我我我馬文", speaker="showay")
    assert res["wake_intent"] == 0.95
    assert res["text"] == "馬文"
    assert res["is_wake"] is True
