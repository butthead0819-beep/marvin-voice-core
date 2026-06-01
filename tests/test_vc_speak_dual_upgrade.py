"""VoiceController.speak() Marmo Case B 機率升級為 dual 的 gate 邏輯。

驗證 _maybe_try_dual_upgrade gate（每次呼叫現讀 env）：
  - MARMO_DUAL_SPEAK 未設 → False
  - MARMO_DUAL_SPEAK=true 但 random >= chance → False
  - 全部 OK 但 bot.router 是 None → False
  - 全部 OK → True

speak() 整合：
  - proactive=False → 永遠走 play_tts、不試 dual
  - proactive=True + gate False → play_tts
  - proactive=True + gate True + 雙段成功 → play_dual_dialogue（play_tts 不被呼叫）
  - proactive=True + gate True + 雙段 None（LLM 失敗）→ fallback play_tts
  - proactive=True + gate True + 雙段拋例外 → fallback play_tts
"""
from __future__ import annotations

import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cogs.voice_controller import VoiceController


def _fake_vc():
    fake = types.SimpleNamespace()
    fake.play_tts = AsyncMock(return_value=None)
    fake.play_dual_dialogue = AsyncMock(return_value=None)
    bot = MagicMock()
    bot.router = MagicMock()  # 預設有 router
    fake.bot = bot
    # speak() 內部呼叫 self._maybe_try_dual_upgrade() / self._generate_dual_marvin_lead()
    # — fake self 必須掛這兩個方法，使用真實 unbound class method
    fake._maybe_try_dual_upgrade = lambda: VoiceController._maybe_try_dual_upgrade(fake)
    fake._generate_dual_marvin_lead = lambda text: VoiceController._generate_dual_marvin_lead(fake, text)
    return fake


# ── _maybe_try_dual_upgrade gate ──────────────────────────────────────────────

def test_gate_flag_off(monkeypatch):
    monkeypatch.delenv("MARMO_DUAL_SPEAK", raising=False)
    fake = _fake_vc()
    assert VoiceController._maybe_try_dual_upgrade(fake) is False


def test_gate_flag_false(monkeypatch):
    monkeypatch.setenv("MARMO_DUAL_SPEAK", "false")
    fake = _fake_vc()
    assert VoiceController._maybe_try_dual_upgrade(fake) is False


def test_gate_chance_zero(monkeypatch):
    """chance=0 → 永遠 False（不試 dual）。"""
    monkeypatch.setenv("MARMO_DUAL_SPEAK", "true")
    monkeypatch.setenv("MARMO_DUAL_CHANCE", "0")
    fake = _fake_vc()
    # 跑 50 次都不該回 True
    assert all(VoiceController._maybe_try_dual_upgrade(fake) is False for _ in range(50))


def test_gate_chance_one(monkeypatch):
    """chance=1 → 永遠 True（必定試 dual）。"""
    monkeypatch.setenv("MARMO_DUAL_SPEAK", "true")
    monkeypatch.setenv("MARMO_DUAL_CHANCE", "1.0")
    fake = _fake_vc()
    assert all(VoiceController._maybe_try_dual_upgrade(fake) is True for _ in range(50))


def test_gate_router_missing(monkeypatch):
    """router=None → False，避免叫到不存在的 router。"""
    monkeypatch.setenv("MARMO_DUAL_SPEAK", "true")
    monkeypatch.setenv("MARMO_DUAL_CHANCE", "1.0")
    fake = _fake_vc()
    fake.bot.router = None
    assert VoiceController._maybe_try_dual_upgrade(fake) is False


def test_gate_invalid_chance_defaults_to_half(monkeypatch):
    """chance 非數字 → 預設 0.5（不爆例外）。"""
    monkeypatch.setenv("MARMO_DUAL_SPEAK", "true")
    monkeypatch.setenv("MARMO_DUAL_CHANCE", "not_a_number")
    fake = _fake_vc()
    # 應該不爆，回 bool 即可
    result = VoiceController._maybe_try_dual_upgrade(fake)
    assert isinstance(result, bool)


# ── speak() 整合：proactive=False 永遠單聲 ────────────────────────────────────

@pytest.mark.asyncio
async def test_speak_non_proactive_never_dual(monkeypatch):
    """proactive=False → 不論 env / chance，永遠走 play_tts。"""
    monkeypatch.setenv("MARMO_DUAL_SPEAK", "true")
    monkeypatch.setenv("MARMO_DUAL_CHANCE", "1.0")  # 必中
    fake = _fake_vc()
    await VoiceController.speak(fake, "你好", proactive=False)
    fake.play_tts.assert_awaited_once()
    fake.play_dual_dialogue.assert_not_called()


# ── speak() 整合：proactive=True ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_speak_proactive_gate_off_uses_play_tts(monkeypatch):
    monkeypatch.delenv("MARMO_DUAL_SPEAK", raising=False)
    fake = _fake_vc()
    await VoiceController.speak(fake, "你好", proactive=True)
    fake.play_tts.assert_awaited_once()
    fake.play_dual_dialogue.assert_not_called()


@pytest.mark.asyncio
async def test_speak_proactive_dual_success_skips_play_tts(monkeypatch):
    monkeypatch.setenv("MARMO_DUAL_SPEAK", "true")
    monkeypatch.setenv("MARMO_DUAL_CHANCE", "1.0")
    fake = _fake_vc()
    segments = [
        {"voice": "marvin", "text": "存在仍是虛無"},
        {"voice": "marmo", "text": "閉嘴他在問正事"},
    ]
    with patch(
        "cogs.voice_controller.VoiceController._generate_dual_marvin_lead",
        new=AsyncMock(return_value=segments),
    ):
        await VoiceController.speak(fake, "今天怎樣", proactive=True)
    fake.play_dual_dialogue.assert_awaited_once_with(segments)
    fake.play_tts.assert_not_called()


@pytest.mark.asyncio
async def test_speak_proactive_dual_returns_none_falls_back(monkeypatch):
    """LLM 失敗 / 紅線命中 → generate 回 None → fallback 走 play_tts。"""
    monkeypatch.setenv("MARMO_DUAL_SPEAK", "true")
    monkeypatch.setenv("MARMO_DUAL_CHANCE", "1.0")
    fake = _fake_vc()
    with patch(
        "cogs.voice_controller.VoiceController._generate_dual_marvin_lead",
        new=AsyncMock(return_value=None),
    ):
        await VoiceController.speak(fake, "今天怎樣", proactive=True)
    fake.play_dual_dialogue.assert_not_called()
    fake.play_tts.assert_awaited_once()


@pytest.mark.asyncio
async def test_speak_proactive_dual_raises_falls_back(monkeypatch):
    """generate 拋例外 → fallback 走 play_tts，整段不爆。"""
    monkeypatch.setenv("MARMO_DUAL_SPEAK", "true")
    monkeypatch.setenv("MARMO_DUAL_CHANCE", "1.0")
    fake = _fake_vc()
    with patch(
        "cogs.voice_controller.VoiceController._generate_dual_marvin_lead",
        new=AsyncMock(side_effect=RuntimeError("LLM boom")),
    ):
        await VoiceController.speak(fake, "今天怎樣", proactive=True)
    fake.play_dual_dialogue.assert_not_called()
    fake.play_tts.assert_awaited_once()
