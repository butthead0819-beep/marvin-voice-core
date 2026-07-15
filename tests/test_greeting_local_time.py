"""TDD: 進場招呼的時間必須是真實當地時間，不准 LLM 幻覺編造。

Bug（2026-07-15 使用者）：Marvin 打招呼台詞裡的時間每次都是幻覺——
greeting_ambient 的 prompt 明確邀請 LLM 用「時間」拋話題（gemini_router_content.py
user_prompt + marvin_prompts.py:201 system instruction），卻從沒餵真實時間 →
8b 模型自己編。修法：注入 local_time_phrase(now) 並禁止自編。
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

import gemini_router_content as grc
from gemini_router_content import local_time_phrase


# ── local_time_phrase：純函式，週幾 + 時段 + HH:MM ──────────────────────────

def test_phrase_format_wednesday_afternoon():
    ts = datetime(2026, 7, 15, 12, 20).timestamp()  # 2026-07-15 = 週三
    assert local_time_phrase(ts) == "週三 下午 12:20"


def test_phrase_format_late_night():
    ts = datetime(2026, 7, 15, 2, 5).timestamp()
    assert local_time_phrase(ts) == "週三 凌晨 02:05"


def test_phrase_weekday_mapping():
    # 2026-07-13 是週一
    ts_mon = datetime(2026, 7, 13, 10, 0).timestamp()
    assert local_time_phrase(ts_mon).startswith("週一")
    # 2026-07-19 是週日
    ts_sun = datetime(2026, 7, 19, 10, 0).timestamp()
    assert local_time_phrase(ts_sun).startswith("週日")


@pytest.mark.parametrize("hour,slot", [
    (4, "凌晨"), (0, "凌晨"),
    (5, "早晨"), (8, "早晨"),
    (9, "上午"), (11, "上午"),
    (12, "下午"), (17, "下午"),
    (18, "傍晚"), (20, "傍晚"),
    (21, "深夜"), (23, "深夜"),
])
def test_phrase_slot_boundaries(hour, slot):
    ts = datetime(2026, 7, 15, hour, 0).timestamp()
    assert slot in local_time_phrase(ts)


# ── generate_greeting：ambient 必須注入真實時間 ─────────────────────────────

def _fake_router():
    fake = MagicMock()
    fake._call_llm = AsyncMock(return_value="嗨")
    fake.prompt_manager.get_instruction = MagicMock(return_value="sys")
    fake.vision_enabled = True
    fake.dna = {}
    fake.temp_toxicity_override = None
    return fake


@pytest.mark.asyncio
async def test_ambient_greeting_injects_real_local_time(monkeypatch):
    """冷場長招呼 → user_prompt 帶真實當地時間 + 禁止自編指示。"""
    fixed = datetime(2026, 7, 15, 2, 30).timestamp()  # 週三 凌晨 02:30
    monkeypatch.setattr(grc.time, "time", lambda: fixed)

    fake = _fake_router()
    await grc.GeminiRouterContentMixin.generate_greeting(fake, players=["大肚"], active=False)

    user_prompt = fake._call_llm.call_args.args[1]
    assert local_time_phrase(fixed) in user_prompt, f"應注入真實時間: {user_prompt!r}"
    assert "02:30" in user_prompt
    # 必須明確禁止 LLM 自己編時間
    assert "編" in user_prompt or "真實" in user_prompt


@pytest.mark.asyncio
async def test_brief_greeting_stays_terse_without_time(monkeypatch):
    """熱絡短招呼（快速報到）不注入時間，維持極簡、避免無謂提時間。"""
    fixed = datetime(2026, 7, 15, 2, 30).timestamp()
    monkeypatch.setattr(grc.time, "time", lambda: fixed)

    fake = _fake_router()
    await grc.GeminiRouterContentMixin.generate_greeting(fake, players=["大肚", "showay", "狗與露"], active=True)

    user_prompt = fake._call_llm.call_args.args[1]
    assert "現在時間" not in user_prompt, f"短招呼不該注入時間: {user_prompt!r}"
