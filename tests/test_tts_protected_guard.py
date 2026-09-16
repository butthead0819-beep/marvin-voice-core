"""_protected_tts_window() 回歸測試（Phase A 重構：抽出共用 choke point）。

歷史上「進入 protected TTS 窗口時忘記 save/restore _tts_protected」這類 bug
出過至少 3 次（PR#84/86/87）。這裡鎖住 context manager 本身的最小契約：
進入時設 True、離開時（含例外路徑）還原成先前的值，不論先前是 False 或 True。
"""
from __future__ import annotations

import pytest

from cogs.voice_controller_playback import PlaybackMixin


class _Dummy(PlaybackMixin):
    """只需要 self._tts_protected 屬性；PlaybackMixin 沒有 __init__，裸繼承即可。"""


def test_protected_tts_window_sets_true_on_enter_and_restores_false():
    obj = _Dummy()
    obj._tts_protected = False
    with obj._protected_tts_window():
        assert obj._tts_protected is True
    assert obj._tts_protected is False


def test_protected_tts_window_restores_prior_true():
    obj = _Dummy()
    obj._tts_protected = True
    with obj._protected_tts_window():
        assert obj._tts_protected is True
    assert obj._tts_protected is True


def test_protected_tts_window_restores_on_exception():
    obj = _Dummy()
    obj._tts_protected = False
    with pytest.raises(RuntimeError):
        with obj._protected_tts_window():
            assert obj._tts_protected is True
            raise RuntimeError("boom")
    assert obj._tts_protected is False


def test_protected_tts_window_defaults_missing_attr_to_false():
    """呼叫端沒先設過 _tts_protected 時（getattr 預設 False）也不炸、且正確還原。"""
    obj = _Dummy()
    assert not hasattr(obj, "_tts_protected")
    with obj._protected_tts_window():
        assert obj._tts_protected is True
    assert obj._tts_protected is False
