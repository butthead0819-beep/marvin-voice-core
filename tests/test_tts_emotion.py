"""TTS 情緒微調（2026-08-21）：edge-tts 沒有 SSML style/express-as，zh-TW-YunJheNeural
也沒有 StyleList，真情緒風格是死路——改用 rate/pitch 微調當替代（見 tts_engine.py
_EMOTION_ADJUST 開頭說明）。這裡測純函式 delta 計算 + generate_audio 依 emotion
挑 rate/pitch 傳給 stream_audio，且不同 emotion 不共用快取檔。
"""
from __future__ import annotations

import pytest

from tts_engine import SukiTTS, _apply_pitch_delta, _apply_rate_delta


class TestApplyRateDelta:
    def test_positive_delta_adds_to_base(self):
        assert _apply_rate_delta("-20%", 5) == "-15%"

    def test_negative_delta_subtracts_from_base(self):
        assert _apply_rate_delta("-20%", -5) == "-25%"

    def test_zero_delta_returns_base_unchanged(self):
        assert _apply_rate_delta("-20%", 0) == "-20%"

    def test_unparseable_base_treated_as_zero(self):
        assert _apply_rate_delta("garbage", 5) == "+5%"


class TestApplyPitchDelta:
    def test_positive_delta_adds_to_base(self):
        assert _apply_pitch_delta("-15Hz", 10) == "-5Hz"

    def test_negative_delta_subtracts_from_base(self):
        assert _apply_pitch_delta("-15Hz", -8) == "-23Hz"

    def test_zero_delta_returns_base_unchanged(self):
        assert _apply_pitch_delta("-15Hz", 0) == "-15Hz"


class _RecordingEngine(SukiTTS):
    """generate_audio 內部呼叫 stream_audio 的地方換成記錄呼叫參數的假串流，
    不打真的 edge-tts 網路請求。"""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.calls = []

    async def stream_audio(self, text, voice=None, rate=None, pitch=None, volume=None, force_macos=False):
        self.calls.append({"text": text, "rate": rate, "pitch": pitch})
        yield b"x" * 200  # 超過 generate_audio 的 100-byte 有效檔案門檻


@pytest.mark.asyncio
class TestGenerateAudioEmotionWiring:
    async def test_upbeat_emotion_passes_adjusted_rate_and_pitch(self, tmp_path):
        engine = _RecordingEngine()
        engine.temp_dir = str(tmp_path)
        await engine.generate_audio("測試台詞", emotion="upbeat")
        assert engine.calls[0]["rate"] == "-15%"   # -20% + 5
        assert engine.calls[0]["pitch"] == "-5Hz"  # -15Hz + 10

    async def test_calm_emotion_passes_adjusted_rate_and_pitch(self, tmp_path):
        engine = _RecordingEngine()
        engine.temp_dir = str(tmp_path)
        await engine.generate_audio("測試台詞", emotion="calm")
        assert engine.calls[0]["rate"] == "-25%"   # -20% - 5
        assert engine.calls[0]["pitch"] == "-23Hz"  # -15Hz - 8

    async def test_normal_emotion_passes_no_override(self, tmp_path):
        engine = _RecordingEngine()
        engine.temp_dir = str(tmp_path)
        await engine.generate_audio("測試台詞", emotion="normal")
        assert engine.calls[0]["rate"] is None
        assert engine.calls[0]["pitch"] is None

    async def test_unknown_emotion_defaults_to_no_override(self, tmp_path):
        engine = _RecordingEngine()
        engine.temp_dir = str(tmp_path)
        await engine.generate_audio("測試台詞", emotion="不存在的情緒")
        assert engine.calls[0]["rate"] is None
        assert engine.calls[0]["pitch"] is None

    async def test_same_text_different_emotion_uses_different_cache_file(self, tmp_path):
        """emotion 沒進快取 key 的話，第二次呼叫會誤用第一次的 rate/pitch 音檔。"""
        engine = _RecordingEngine()
        engine.temp_dir = str(tmp_path)
        path_upbeat = await engine.generate_audio("同一句話", emotion="upbeat")
        path_calm = await engine.generate_audio("同一句話", emotion="calm")
        assert path_upbeat != path_calm
        assert len(engine.calls) == 2  # 兩個檔都各自真的合成，不是快取命中
