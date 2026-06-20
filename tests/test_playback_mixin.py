"""
PlaybackMixin — VoiceController 的 TTS 渲染 + mixer 播放抽到獨立檔（減肥），
以 mixin 併入，self 身分不變、零行為改動。外部呼叫者（main_discord / intent_agents
/ 其他 cog）都是實例呼叫 vc.play_tts(...)，方法仍在 VoiceController 上 → 不受影響。
"""
from __future__ import annotations

import pytest

MOD = "cogs.voice_controller_playback"

MOVED = [
    "_ensure_mixer_playing",
    "_mixer_play_music",
    "_ffmpeg_to_f32",
    "_stream_tts_to_mixer",
    "speak",
    "_maybe_try_dual_upgrade",
    "_generate_dual_marvin_lead",
    "play_tts",
    "_play_dual_interject",
    "play_dual_dialogue",
    "tts_flush",
    "play_local_file",
    "_cleanup_fifo",
    "_release_queue_duration",
]


def test_mixin_in_mro():
    from cogs.voice_controller import VoiceController
    from cogs.voice_controller_playback import PlaybackMixin
    assert PlaybackMixin in VoiceController.__mro__


@pytest.mark.parametrize("name", MOVED)
def test_method_moved(name):
    from cogs.voice_controller import VoiceController
    assert getattr(VoiceController, name).__module__ == MOD


def test_max_hotswap_chars_still_importable():
    # test_voice_controller_speak_helper 靠 cogs.voice_controller import 這個
    from cogs.voice_controller import MAX_HOTSWAP_CHARS
    assert MAX_HOTSWAP_CHARS == 12
