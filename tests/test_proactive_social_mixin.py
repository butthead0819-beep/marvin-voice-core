"""
ProactiveSocialMixin — VoiceController 的「主動社交 / SpeakBus tick / 主動表演」
子系統抽到獨立檔（減肥 voice_controller.py），以 mixin 併入，self 身分不變、
行為零改動。

守：mixin 在 MRO、方法與 @tasks.loop 真的搬到新模組、PROACTIVE_TOPIC_COOLDOWN_S
仍可從 cogs.voice_controller import（既有測試與外部相依靠這個）、_compute_speak_mode
的 precedence 不變。
"""
from __future__ import annotations

import pytest


PLAIN_METHODS = [
    "proactive_topic_on_cooldown",
    "mark_proactive_topic_spoken",
    "_compute_speak_mode",
    "_build_speak_context",
    "_post_utterance_speak_tick",
    "_record_speak_outcome_after",
    "trigger_proactive_topic",
    "_proactive_play_manzai",
    "_proactive_play_imitate",
    "_proactive_play_news",
    "_proactive_play_standup",
    "_proactive_play_joke",
]

LOOP_METHODS = ["background_news_loop", "speak_bus_tick_loop", "dynamic_social_loop"]


def test_mixin_in_voice_controller_mro():
    from cogs.voice_controller import VoiceController
    from cogs.voice_controller_social import ProactiveSocialMixin
    assert ProactiveSocialMixin in VoiceController.__mro__


@pytest.mark.parametrize("name", PLAIN_METHODS)
def test_plain_method_moved_to_social_module(name):
    from cogs.voice_controller import VoiceController
    fn = getattr(VoiceController, name)
    assert fn.__module__ == "cogs.voice_controller_social", f"{name} 沒搬到 social 模組"


@pytest.mark.parametrize("name", LOOP_METHODS)
def test_loop_moved_to_social_module(name):
    from discord.ext import tasks
    from cogs.voice_controller import VoiceController
    loop = getattr(VoiceController, name)
    assert isinstance(loop, tasks.Loop), f"{name} 不是 tasks.Loop"
    assert loop.coro.__module__ == "cogs.voice_controller_social"


def test_cooldown_constant_still_importable_from_voice_controller():
    # 既有 test_proactive_topic_shared_cooldown.py + __init__ 接線靠這個 re-export
    from cogs.voice_controller import PROACTIVE_TOPIC_COOLDOWN_S
    assert PROACTIVE_TOPIC_COOLDOWN_S == 600.0


@pytest.mark.parametrize("attrs,expected", [
    ({"game_mode": True, "stream_mode": True, "radio_mode": True}, "game"),
    ({"game_mode": False, "stream_mode": True, "radio_mode": True}, "stream"),
    ({"game_mode": False, "stream_mode": False, "radio_mode": True}, "radio"),
    ({"game_mode": False, "stream_mode": False, "radio_mode": False}, "normal"),
])
def test_compute_speak_mode_precedence(attrs, expected):
    from unittest.mock import MagicMock
    from cogs.voice_controller import VoiceController
    vc = VoiceController.__new__(VoiceController)
    # 關掉 MusicCog 委派，讓 stream_mode/radio_mode property 走 local fallback
    vc.bot = MagicMock()
    vc.bot.cogs.get.return_value = None
    for k, v in attrs.items():
        setattr(vc, k, v)
    assert vc._compute_speak_mode() == expected
