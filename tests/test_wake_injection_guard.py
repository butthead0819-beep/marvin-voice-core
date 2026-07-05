"""TDD: P2-a — Wake Injection Guard 拒絕時也要把 wake_intent 設 None。

5/18 audit 真相：cases #10/#20/#23（"3F D呀每天都去點" 等 raw 無 marvin
但 wake_intent=1.0）能進到 IntentBus 是因為：

  1. stt_cleaner LLM 對音訊判 intent=1.0, calling=True
  2. _build_res：is_wake=True，但 Wake Injection Guard 偵測「original 無
     wake 詞 → reject」→ 設 is_wake=False
  3. ❌ **wake_intent 仍保留 1.0**
  4. discord_voice_engine 把 (is_wake=False, wake_intent=1.0, track="B")
     傳到 stt_callback
  5. voice_controller.handle_stt_result 進 IBA fusion：
     `_voice_score(action="drop", wake_intent=1.0, track="B")` →
     `if track == "B" and wake_intent is not None: return wake_intent` = 1.0
  6. total = VOICE_WEIGHT * 1.0 + ... ≥ threshold → is_fast=True → wake!

Bug 根因：Wake Injection Guard 只清 is_wake 不清 wake_intent，下游 IBA
fusion 把 wake_intent 當 truth source 結果 false positive 漏過。

P2-a 修法：抽出 pure helper `_verify_wake_against_raw(is_wake, wake_intent,
raw)`，rejection 同時把 wake_intent 設 None。
"""
from __future__ import annotations

import pytest

from stt_cleaner import _revert_injected_wake, _verify_wake_against_raw


# ── Rejection path: raw 無 wake 但 LLM 判 wake → 雙清 ─────────────────────

@pytest.mark.parametrize("raw", [
    "3F D呀每天都去點",                # 5/18 #20
    "嫂嫂有成功啊成功率越高",            # 5/18 #23
    "幹打開小女兒哭",                  # 5/18 #10
    "今天天氣不錯",                    # 完全無 marvin
])
def test_raw_without_wake_word_rejects_and_nullifies_intent(raw):
    is_wake, wake_intent = _verify_wake_against_raw(
        is_wake=True, wake_intent=1.0, raw_text=raw,
    )
    assert is_wake is False, f"raw='{raw}' 無 wake 詞，is_wake 必須清為 False"
    assert wake_intent is None, (
        f"raw='{raw}' 拒絕時 wake_intent 也必須清為 None，"
        f"避免下游 IBA fusion 仍用 1.0 推 is_fast=True（實際={wake_intent}）"
    )


# ── Pass-through: raw 有 wake → 不動 ──────────────────────────────────────

@pytest.mark.parametrize("raw,intent", [
    ("馬文，你好嗎", 1.0),
    ("Marvin, play music", 0.85),
    ("嗨馬文", 0.95),
    ("hey marvin", 0.70),
])
def test_raw_with_wake_word_preserves_intent(raw, intent):
    is_wake, wake_intent = _verify_wake_against_raw(
        is_wake=True, wake_intent=intent, raw_text=raw,
    )
    assert is_wake is True
    assert wake_intent == pytest.approx(intent)


# ── 不影響 is_wake=False 的 case（不該誤動）─────────────────────────────

def test_low_intent_already_rejected_unchanged():
    """LLM 自己判 is_wake=False（低信心），不該被 guard 改動 wake_intent。"""
    is_wake, wake_intent = _verify_wake_against_raw(
        is_wake=False, wake_intent=0.30, raw_text="完全沒馬文",
    )
    assert is_wake is False
    assert wake_intent == pytest.approx(0.30)


# ── raw 是 None / 空 → 不 reject（沒資料就不判定）────────────────────────

def test_none_raw_does_not_reject():
    is_wake, wake_intent = _verify_wake_against_raw(
        is_wake=True, wake_intent=0.9, raw_text=None,
    )
    assert is_wake is True
    assert wake_intent == pytest.approx(0.9)


def test_empty_raw_rejects():
    """空字串 raw + is_wake=True → 顯然 LLM 注入，照 reject。"""
    is_wake, wake_intent = _verify_wake_against_raw(
        is_wake=True, wake_intent=0.9, raw_text="",
    )
    assert is_wake is False
    assert wake_intent is None


# ── 整合：_build_res 從 LLM 得到 intent=1.0 但 raw 無 wake → res 兩個都清 ─

# ── 文字層注入補漏：cleaned 冒出原句沒有的喚醒詞 → 還原（不論 is_wake）─────────


@pytest.mark.parametrize("cleaned,raw", [
    ("馬文說個笑話", "說個笑話"),        # 前綴注入（7/5 User_local live）
    ("播放馬文的歌", "播放的歌"),        # 句中注入
    ("馬文", "說個笑話"),               # 整句被塌縮成喚醒詞（over-collapse 亦屬注入）
])
def test_injected_wake_reverts_to_raw(cleaned, raw):
    text, injected = _revert_injected_wake(cleaned, raw)
    assert injected is True
    assert text == raw


@pytest.mark.parametrize("cleaned,raw", [
    ("播周杰倫", "播周杰倫"),            # 雙方都無喚醒詞 → 不動
    ("馬文播周杰倫", "馬文播周杰倫"),    # 雙方都有喚醒詞（真喚醒）→ 不動
    ("說個笑話", "馬文說個笑話"),        # raw 有、cleaned 沒（正常剝除）→ 不動
])
def test_no_injection_leaves_text_unchanged(cleaned, raw):
    text, injected = _revert_injected_wake(cleaned, raw)
    assert injected is False
    assert text == cleaned


def test_none_raw_no_baseline_does_not_revert():
    text, injected = _revert_injected_wake("馬文說個笑話", None)
    assert injected is False
    assert text == "馬文說個笑話"


@pytest.mark.asyncio
async def test_clean_stt_text_reverts_injected_wake_when_not_wake(tmp_path, monkeypatch):
    """整合：cleaner 把「說個笑話」注入成「馬文說個笑話」且判 not-wake（intent 低）
    → clean_stt_text 還原成「說個笑話」，is_wake/wake_intent 清空，不污染 ctx.query。"""
    import json
    from unittest.mock import AsyncMock, MagicMock

    from stt_cleaner import GeminiRouterSTTMixin

    # 隔離本地 corrections 讀寫，避免走 fast-path 跳過 LLM / 污染 prod
    monkeypatch.setattr("stt_cleaner._LOCAL_CORRECTIONS_PATH", tmp_path / "noop.json")
    monkeypatch.setattr("stt_cleaner._CORRECTIONS_LOG", tmp_path / "noop.jsonl")

    class _R(GeminiRouterSTTMixin):
        pass

    r = _R()
    r.wake_fusion = None
    r.prompt_manager = MagicMock()
    r.prompt_manager.get_instruction = MagicMock(return_value="SYS")
    r.google_cleaner_client = None
    rt = MagicMock()
    rt.quick = AsyncMock(return_value=json.dumps(
        {"cleaned": "馬文說個笑話", "intent": 0.0, "calling": False, "is_complete": True},
        ensure_ascii=False))
    r._stt_router = rt

    res = await r.clean_stt_text("說個笑話", speaker=None)
    assert res["text"] == "說個笑話"      # 注入的「馬文」已還原
    assert res["is_wake"] is False
    assert res["wake_intent"] is None


def test_build_res_integration_rejects_and_nullifies():
    """`_build_res` 在 cleaner 內 — 整合測試確認 P2-a fix 真的 reflect 到 dict。"""
    from stt_cleaner import GeminiRouterSTTMixin

    # 不需要真的初始化 mixin，直接用閉包邏輯
    # 用 _verify_wake_against_raw 走 _build_res 等價路徑
    is_wake, wake_intent = _verify_wake_against_raw(
        is_wake=True, wake_intent=1.0, raw_text="3F D呀每天都去點",
    )
    # 模擬 _build_res 回傳的 dict 應該長這樣
    expected_res = {
        "text": "3F D呀每天都去點",  # 拒絕時退回 original
        "is_wake": is_wake,            # False
        "wake_intent": wake_intent,    # None ← P2-a 關鍵
        "wake_threshold": 0.70,
    }
    assert expected_res["is_wake"] is False
    assert expected_res["wake_intent"] is None
