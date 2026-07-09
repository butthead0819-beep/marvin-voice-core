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


# ── _mixer_play_music 退出原因日誌（device 「~3s 中斷、無錯誤日誌」的觀測性補丁）──
#
# _mixer_play_music 是所有「音樂停了」路徑的唯一出口，過去退出時不 log→無從得知是音源
# 耗盡、still_active 被關、還是斷線。補的 log 讓下次 live 一眼看出，這裡驗三種原因都印對。

class _FakeMixer:
    """精確控制 has_music() 序列（不需真音訊）。"""

    def __init__(self, has_music_seq):
        self._seq = list(has_music_seq)
        self.cleared = False

    def set_music_source(self, s):
        self._src = s          # BufferedF32MusicSource：內部 bg thread 讀 s16 源

    def has_music(self):
        return self._seq.pop(0) if self._seq else False

    def clear_music(self):
        self.cleared = True

    def set_volume(self, v):
        pass


class _FakeDevice:
    def __init__(self, connected=True):
        self._connected = connected

    def is_connected(self):
        return self._connected


class _ExhaustedS16:
    """s16 源立即耗盡（BufferedF32 bg thread 讀到 b"" 即 eof、乾淨退出）。"""

    def read(self):
        return b""

    def cleanup(self):
        pass


def _fake_self(mixer):
    from types import SimpleNamespace
    return SimpleNamespace(
        _mixer=mixer,
        _ensure_mixer_playing=lambda device: None,  # noop，不真 arm
        _stream_norm_gain={},
        _current_stream_url="",
    )


@pytest.mark.asyncio
async def test_mixer_play_music_logs_still_active_false(caplog):
    from cogs.voice_controller_playback import PlaybackMixin
    mixer = _FakeMixer([True])   # 有音樂，但 still_active 立刻 False
    with caplog.at_level("INFO"):
        await PlaybackMixin._mixer_play_music(
            _fake_self(mixer), _FakeDevice(), _ExhaustedS16(),
            still_active=lambda: False,
        )
    assert mixer.cleared is True
    assert any("reason=still_active_false" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_mixer_play_music_logs_source_exhausted(caplog):
    from cogs.voice_controller_playback import PlaybackMixin
    mixer = _FakeMixer([True, False])  # 一圈後 has_music 變 False＝音源耗盡
    with caplog.at_level("INFO"):
        await PlaybackMixin._mixer_play_music(
            _fake_self(mixer), _FakeDevice(), _ExhaustedS16(),
            still_active=lambda: True,
        )
    assert mixer.cleared is False   # 音源自然耗盡不呼叫 clear_music
    assert any("reason=source_exhausted" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_mixer_play_music_logs_disconnected(caplog):
    from cogs.voice_controller_playback import PlaybackMixin
    mixer = _FakeMixer([True])
    with caplog.at_level("INFO"):
        await PlaybackMixin._mixer_play_music(
            _fake_self(mixer), _FakeDevice(connected=False), _ExhaustedS16(),
            still_active=lambda: True,
        )
    assert mixer.cleared is True
    assert any("reason=disconnected" in r.message for r in caplog.records)
