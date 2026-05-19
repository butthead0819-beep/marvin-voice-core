"""Lock decoupling: MusicAgentV2.declare_intents must not read keyword
constants via controller. After refactor, keywords come from
intent_agents.constants — controller can be a bare object without
any _MUSIC_*_KW attribute.
"""
from __future__ import annotations

import pytest

from intent_agents.music_agent_v2 import MusicAgentV2


class _BareCtrl:
    """Controller stub without any _MUSIC_*_KW attribute.

    Accessing any _MUSIC_*_KW on this object raises AttributeError —
    which is exactly what we want to catch any residual coupling.
    """
    pass


def test_declare_intents_does_not_read_ctrl_keywords():
    agent = MusicAgentV2(_BareCtrl())
    intents = agent.declare_intents()
    names = [i.name for i in intents]
    assert "control_skip" in names
    assert "control_pause" in names
    assert "control_resume" in names
    assert "control_stop" in names
    assert "strong_play" in names
    assert "weak_play_with_marker" in names
    assert "weak_play_long_string" in names


def test_constants_module_exports_expected_keywords():
    from intent_agents import constants as c

    # 6 keyword families that v2 needs for declare_intents
    assert "放音樂" in c.STRONG_PLAY_KW
    assert "播放" in c.WEAK_PLAY_KW
    assert "換一首" in c.MUSIC_SKIP_KW
    assert "停止播放" in c.MUSIC_STOP_KW
    assert "暫停音樂" in c.MUSIC_PAUSE_KW
    assert "繼續播" in c.MUSIC_RESUME_KW

    # Combined view used by voice_controller's _extract_music_search_query
    assert set(c.MUSIC_PLAY_KW) == set(c.STRONG_PLAY_KW) | set(c.WEAK_PLAY_KW)

    # IBA-T0 direct keywords (frozensets)
    assert isinstance(c.MUSIC_DIRECT_SKIP_KW, frozenset)
    assert "下一首" in c.MUSIC_DIRECT_SKIP_KW
    assert "停止播放" in c.MUSIC_DIRECT_STOP_KW
    assert "暫停音樂" in c.MUSIC_DIRECT_PAUSE_KW
    assert "繼續播" in c.MUSIC_DIRECT_RESUME_KW


def test_voice_controller_class_attrs_reference_constants_module():
    """voice_controller backward-compat: self._XXX still works and points
    to the constants module values (not local literal copies)."""
    from intent_agents import constants as c
    from cogs.voice_controller import VoiceController

    # Class attrs must be value-equal to constants module
    assert list(VoiceController._STRONG_PLAY_KW) == list(c.STRONG_PLAY_KW)
    assert list(VoiceController._WEAK_PLAY_KW) == list(c.WEAK_PLAY_KW)
    assert list(VoiceController._MUSIC_PLAY_KW) == list(c.MUSIC_PLAY_KW)
    assert list(VoiceController._MUSIC_SKIP_KW) == list(c.MUSIC_SKIP_KW)
    assert list(VoiceController._MUSIC_STOP_KW) == list(c.MUSIC_STOP_KW)
    assert list(VoiceController._MUSIC_PAUSE_KW) == list(c.MUSIC_PAUSE_KW)
    assert list(VoiceController._MUSIC_RESUME_KW) == list(c.MUSIC_RESUME_KW)
