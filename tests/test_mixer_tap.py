"""LocalMixingAudioSource.set_tap：單一 mixer 第 1 刀的輸出旁聽。

驗：
(1) instrument=False：set_tap 後每次 read() 的輸出原樣 tap.write(frame)
(2) instrument=True 同上（計時路徑也要一樣 tap）
(3) tap.write raise → read() 照常回 3840 bytes、不 raise（Discord 送幀不能被旁聽者拖垮）
(4) 未 set_tap → read() 正常
(5) set_tap(None) 後 tap 不再收到
"""
from __future__ import annotations

from local_mixing_source import LocalMixingAudioSource


class _RecordingTap:
    def __init__(self):
        self.frames = []

    def write(self, frame):
        self.frames.append(frame)


class _RaisingTap:
    def write(self, frame):
        raise RuntimeError("tap boom")


def _check_tap_mirrors_read(instrument: bool):
    mix = LocalMixingAudioSource(instrument=instrument)
    tap = _RecordingTap()
    mix.set_tap(tap)
    outs = [mix.read() for _ in range(3)]
    assert len(tap.frames) == 3
    assert tap.frames == outs
    assert all(len(f) == 3840 for f in tap.frames)


def test_tap_mirrors_read_output_without_instrument():
    _check_tap_mirrors_read(instrument=False)


def test_tap_mirrors_read_output_with_instrument():
    _check_tap_mirrors_read(instrument=True)


def test_raising_tap_does_not_break_read():
    for instrument in (False, True):
        mix = LocalMixingAudioSource(instrument=instrument)
        mix.set_tap(_RaisingTap())
        out = mix.read()  # 不 raise
        assert len(out) == 3840


def test_read_works_without_tap():
    mix = LocalMixingAudioSource()
    assert mix._tap is None
    assert len(mix.read()) == 3840


def test_set_tap_none_detaches():
    mix = LocalMixingAudioSource()
    tap = _RecordingTap()
    mix.set_tap(tap)
    mix.read()
    assert len(tap.frames) == 1
    mix.set_tap(None)
    mix.read()
    assert len(tap.frames) == 1
