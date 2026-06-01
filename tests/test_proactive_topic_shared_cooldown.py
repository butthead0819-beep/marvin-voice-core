"""
共用 proactive-topic cooldown：冷場 TopicGenerator 與 SpeakBus ProactiveTopicAgent
兩套主動拋話題系統共用同一個 cooldown 時間戳（last_proactive_time），避免使用者
連續聽到兩套話題。

功能重疊 OK（使用者明示），但 cooldown 必須同源：任一系統發話 → 另一系統靜默
PROACTIVE_TOPIC_COOLDOWN_S 秒。
"""
from __future__ import annotations

from unittest.mock import patch

from cogs.voice_controller import VoiceController, PROACTIVE_TOPIC_COOLDOWN_S


def _make_cog():
    cog = VoiceController.__new__(VoiceController)
    cog.last_proactive_time = 0.0
    return cog


# ── on_cooldown 判斷 ──────────────────────────────────────────────────────────

def test_not_on_cooldown_when_never_spoke():
    cog = _make_cog()
    assert cog.proactive_topic_on_cooldown(now=1000.0) is False


def test_on_cooldown_right_after_speaking():
    cog = _make_cog()
    cog.last_proactive_time = 1000.0
    # 同一時刻 / cooldown 窗內 → True
    assert cog.proactive_topic_on_cooldown(now=1000.0) is True
    assert cog.proactive_topic_on_cooldown(now=1000.0 + PROACTIVE_TOPIC_COOLDOWN_S - 1) is True


def test_off_cooldown_after_window():
    cog = _make_cog()
    cog.last_proactive_time = 1000.0
    assert cog.proactive_topic_on_cooldown(now=1000.0 + PROACTIVE_TOPIC_COOLDOWN_S) is False
    assert cog.proactive_topic_on_cooldown(now=1000.0 + PROACTIVE_TOPIC_COOLDOWN_S + 100) is False


# ── mark 更新時間戳 ──────────────────────────────────────────────────────────

def test_mark_updates_timestamp():
    cog = _make_cog()
    cog.mark_proactive_topic_spoken(now=2000.0)
    assert cog.last_proactive_time == 2000.0


def test_mark_then_on_cooldown():
    """mark 後立刻 on_cooldown 為 True（兩套系統共用 → 互相擋）。"""
    cog = _make_cog()
    cog.mark_proactive_topic_spoken(now=3000.0)
    assert cog.proactive_topic_on_cooldown(now=3000.0 + 10) is True


def test_mark_defaults_to_now():
    cog = _make_cog()
    with patch("cogs.voice_controller.time.time", return_value=5555.0):
        cog.mark_proactive_topic_spoken()
    assert cog.last_proactive_time == 5555.0


# ── 跨系統互擋情境 ────────────────────────────────────────────────────────────

def test_cold_trigger_blocked_after_proactive_agent_spoke():
    """情境：ProactiveTopicAgent 剛講（stamp last_proactive_time）→ 冷場系統
    讀同一時間戳 → on_cooldown=True → 不會緊接著再拋一次。"""
    cog = _make_cog()
    # ProactiveTopicAgent handler 發話後 stamp（模擬 trigger_proactive_topic）
    cog.mark_proactive_topic_spoken(now=1000.0)
    # 30 秒後冷場系統想發話 → 被共用 cooldown 擋
    assert cog.proactive_topic_on_cooldown(now=1030.0) is True
