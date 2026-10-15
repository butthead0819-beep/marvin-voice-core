"""
get_estimated_duration 長停頓計分（docs/tts_breath_plan.md Phase 2）。

句中 。！？ 代表 edge 的長停頓（≈1.5s），句尾那個不算（edge 本來就會補靜音）。
"""
from __future__ import annotations

import pytest


def _make_tts(monkeypatch, key="fake-key", region="eastasia"):
    if key is None:
        monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)
    else:
        monkeypatch.setenv("AZURE_SPEECH_KEY", key)
    if region is None:
        monkeypatch.delenv("AZURE_SPEECH_REGION", raising=False)
    else:
        monkeypatch.setenv("AZURE_SPEECH_REGION", region)
    from tts_engine import SukiTTS
    return SukiTTS()


def test_no_pause(monkeypatch):
    tts = _make_tts(monkeypatch)
    assert tts.get_estimated_duration("好吧") == pytest.approx(2 * 0.25 * 1.2)


def test_trailing_pause_not_counted(monkeypatch):
    tts = _make_tts(monkeypatch)
    assert tts.get_estimated_duration("好吧。") == pytest.approx(2 * 0.25 * 1.2)


def test_mid_sentence_pause_counted(monkeypatch):
    tts = _make_tts(monkeypatch)
    assert tts.get_estimated_duration("好。吧") == pytest.approx((2 * 0.25 + 1.25) * 1.2)


def test_ellipsis_becomes_mid_sentence_pause(monkeypatch):
    tts = _make_tts(monkeypatch)
    assert tts.get_estimated_duration("好⋯⋯吧") == pytest.approx((2 * 0.25 + 1.25) * 1.2)
