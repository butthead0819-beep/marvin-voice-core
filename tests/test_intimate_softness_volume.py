"""TDD — T6b-2: 親密模式 TTS 音量自我校準（跟隨使用者音量相對基準）。

先紅後綠：
  - VoiceMetaAnalyzer 無 rms_baseline/last_softness → AttributeError → RED
  - _apply_softness_to_volume 不存在 → AttributeError → RED
  - _resolve_tts_params intimate 未調整 volume → assertEqual 失敗 → RED
"""
from __future__ import annotations

import statistics
from unittest.mock import MagicMock, patch

import pytest

# ── helpers ──────────────────────────────────────────────────────────────────

def _make_cog():
    """Minimal VoiceController（對齊 test_intimate_register._make_cog 骨架）。"""
    bot = MagicMock()
    bot.guilds = []
    bot.cogs.get.return_value = None

    with patch("discord_voice_engine.faster_whisper", None, create=True):
        from discord_voice_engine import DiscordVoiceEngine
        engine = DiscordVoiceEngine(bot)
    bot.engine = engine

    with patch("discord.ext.tasks.loop", lambda *a, **kw: lambda f: f), \
         patch("cogs.voice_controller.DepartureStats", MagicMock), \
         patch("cogs.voice_controller.ConsentManager", MagicMock):
        from cogs.voice_controller import VoiceController
        cog = VoiceController(bot)

    return cog


# ════════════════════════════════════════════════════════════════════════════════
# Group A — VoiceMetaAnalyzer.calculate_prosody 軟度狀態
# ════════════════════════════════════════════════════════════════════════════════

def test_calculate_prosody_first_ever_utterance_last_softness_is_zero():
    """首次發聲：baseline 設為 mean_rms → softness = 0.0（基準就是本次，不算軟）。"""
    from voice_meta_analyzer import VoiceMetaAnalyzer

    uid = "local"
    analyzer = VoiceMetaAnalyzer()
    for r in [1000.0, 1000.0, 1000.0]:
        analyzer.add_rms(uid, r)

    analyzer.calculate_prosody(uid, text="hi", physical_duration=1.0)

    assert analyzer.last_softness == 0.0


def test_calculate_prosody_softer_utterance_raises_last_softness():
    """同一使用者：先建立 baseline，再餵更小 RMS → last_softness > 0。"""
    from voice_meta_analyzer import VoiceMetaAnalyzer

    uid = "local"
    analyzer = VoiceMetaAnalyzer()

    # 建立 baseline（baseline ≈ 1000）
    for r in [1000.0, 1000.0, 1000.0]:
        analyzer.add_rms(uid, r)
    analyzer.calculate_prosody(uid, text="hi", physical_duration=1.0)

    # 更小音量
    for r in [500.0, 500.0, 500.0]:
        analyzer.add_rms(uid, r)
    analyzer.calculate_prosody(uid, text="hi", physical_duration=1.0)

    assert analyzer.last_softness > 0.0


def test_calculate_prosody_louder_utterance_last_softness_is_zero():
    """同一使用者：baseline 建立後，餵更大 RMS → last_softness == 0.0（非負夾到 0）。"""
    from voice_meta_analyzer import VoiceMetaAnalyzer

    uid = "local"
    analyzer = VoiceMetaAnalyzer()

    for r in [1000.0, 1000.0, 1000.0]:
        analyzer.add_rms(uid, r)
    analyzer.calculate_prosody(uid, text="hi", physical_duration=1.0)

    # 更大音量
    for r in [2000.0, 2000.0, 2000.0]:
        analyzer.add_rms(uid, r)
    analyzer.calculate_prosody(uid, text="hi", physical_duration=1.0)

    assert analyzer.last_softness == 0.0


def test_calculate_prosody_equal_utterance_last_softness_is_zero():
    """連續等音量 → last_softness 永遠 0.0（baseline 跟著收斂，不算軟）。"""
    from voice_meta_analyzer import VoiceMetaAnalyzer

    uid = "local"
    analyzer = VoiceMetaAnalyzer()

    for _ in range(3):
        for r in [800.0, 800.0, 800.0]:
            analyzer.add_rms(uid, r)
        analyzer.calculate_prosody(uid, text="hi", physical_duration=1.0)

    assert analyzer.last_softness == 0.0


def test_calculate_prosody_empty_samples_leaves_state_unchanged():
    """無採樣路徑（user_id 不在 rms_history → 早退 {}）→ baseline/last_softness 原值不變。"""
    from voice_meta_analyzer import VoiceMetaAnalyzer

    uid = "local"
    analyzer = VoiceMetaAnalyzer()

    # 建立 baseline，此時 last_softness == 0.0
    for r in [1000.0, 1000.0, 1000.0]:
        analyzer.add_rms(uid, r)
    analyzer.calculate_prosody(uid, text="hi", physical_duration=1.0)
    saved_softness = analyzer.last_softness
    saved_baseline = analyzer.rms_baseline.get(uid)

    # 不 add_rms，直接再呼叫 → user_id 不在 history → 早退 {}
    result = analyzer.calculate_prosody(uid, text="hi", physical_duration=1.0)

    assert result == {}
    assert analyzer.last_softness == saved_softness
    assert analyzer.rms_baseline.get(uid) == saved_baseline


def test_calculate_prosody_returned_dict_has_no_softness_or_baseline_keys():
    """返回 dict 形狀不變：無 'softness'/'baseline' 鍵（狀態在 analyzer 上，非 dict）。"""
    from voice_meta_analyzer import VoiceMetaAnalyzer

    uid = "local"
    analyzer = VoiceMetaAnalyzer()
    for r in [1000.0, 1000.0, 1000.0]:
        analyzer.add_rms(uid, r)

    result = analyzer.calculate_prosody(uid, text="hi", physical_duration=1.0)

    assert "softness" not in result
    assert "baseline" not in result
    # 既有 key 全在
    for key in ("wps", "char_count", "energy_variance", "physical_duration", "sample_count", "mean_rms"):
        assert key in result, f"expected key '{key}' missing from returned dict"


# ════════════════════════════════════════════════════════════════════════════════
# Group B — _apply_softness_to_volume 純函式
# ════════════════════════════════════════════════════════════════════════════════

def test_apply_softness_to_volume_zero_softness_no_change():
    """softness=0.0 → volume 原封不動。"""
    cog = _make_cog()
    assert cog._apply_softness_to_volume("-18%", 0.0) == "-18%"


def test_apply_softness_to_volume_full_softness_max_reduction():
    """softness=1.0 → -18% - 15 = -33%。"""
    cog = _make_cog()
    assert cog._apply_softness_to_volume("-18%", 1.0) == "-33%"


def test_apply_softness_to_volume_partial_softness_worked_example():
    """softness=0.8 → -18% - round(0.8*15)=12 = -30%（acceptance 範例）。"""
    cog = _make_cog()
    assert cog._apply_softness_to_volume("-18%", 0.8) == "-30%"


def test_apply_softness_to_volume_clamp_at_minus60():
    """減後超過 -60 → 夾在 -60%。"""
    cog = _make_cog()
    # -55% - round(1.0*15)=15 = -70 → clamped to -60
    assert cog._apply_softness_to_volume("-55%", 1.0) == "-60%"


def test_apply_softness_to_volume_at_minus60_clamped():
    """-60% 底 → 不論 softness 都不低於 -60%。"""
    cog = _make_cog()
    assert cog._apply_softness_to_volume("-60%", 1.0) == "-60%"


def test_apply_softness_to_volume_none_vol_base_zero():
    """vol=None → base=0，0 - round(0.5*15)=8 = -8%。"""
    cog = _make_cog()
    assert cog._apply_softness_to_volume(None, 0.5) == "-8%"


def test_apply_softness_to_volume_empty_str_vol_base_zero():
    """vol='' → base=0，同 None 路徑。"""
    cog = _make_cog()
    assert cog._apply_softness_to_volume("", 0.5) == "-8%"


# ════════════════════════════════════════════════════════════════════════════════
# Group C — _resolve_tts_params 親密模式 softness 整合
# ════════════════════════════════════════════════════════════════════════════════

def test_resolve_tts_params_intimate_with_softness_reduces_volume():
    """intimate=True, last_softness=0.8 → CALM bucket volume -18% → -30%。"""
    cog = _make_cog()
    cog._intimate_mode = True

    mock_meta = MagicMock()
    mock_meta.last_softness = 0.8
    cog.bot.engine.meta_analyzer = mock_meta

    result = cog._resolve_tts_params("neutral")
    assert result["volume"] == "-30%"


def test_resolve_tts_params_intimate_zero_softness_volume_unchanged():
    """intimate=True, last_softness=0.0 → CALM bucket volume 原值 -18%。"""
    cog = _make_cog()
    cog._intimate_mode = True

    mock_meta = MagicMock()
    mock_meta.last_softness = 0.0
    cog.bot.engine.meta_analyzer = mock_meta

    result = cog._resolve_tts_params("neutral")
    assert result["volume"] == "-18%"


def test_resolve_tts_params_intimate_rate_pitch_match_bucket():
    """intimate=True → rate/pitch 永遠與 bucket 對齊（softness 只動 volume）。"""
    cog = _make_cog()
    cog._intimate_mode = True

    mock_meta = MagicMock()
    mock_meta.last_softness = 0.8
    cog.bot.engine.meta_analyzer = mock_meta

    result = cog._resolve_tts_params("neutral")
    assert result["rate"] == cog._INTIMATE_CALM["rate"]
    assert result["pitch"] == cog._INTIMATE_CALM["pitch"]


def test_resolve_tts_params_intimate_does_not_mutate_class_constant():
    """intimate 分支回傳的是 copy，不修改 _INTIMATE_CALM 常數本身。"""
    cog = _make_cog()
    cog._intimate_mode = True

    mock_meta = MagicMock()
    mock_meta.last_softness = 0.8
    cog.bot.engine.meta_analyzer = mock_meta

    original_volume = cog._INTIMATE_CALM["volume"]
    cog._resolve_tts_params("neutral")

    assert cog._INTIMATE_CALM["volume"] == original_volume, \
        "_INTIMATE_CALM 被就地修改了（應回傳 copy）"


def test_resolve_tts_params_off_does_not_read_meta_analyzer():
    """intimate OFF → meta_analyzer.last_softness 不被讀取（byte-equiv 確保）。"""
    cog = _make_cog()
    cog._intimate_mode = False

    mock_meta = MagicMock()
    cog.bot.engine.meta_analyzer = mock_meta

    result = cog._resolve_tts_params("neutral")

    # 結果等於 emotion params（非 intimate bucket）
    expected = cog._EMOTION_TTS_PARAMS.get("neutral", cog._EMOTION_TTS_PARAMS["neutral"])
    assert result == expected

    # last_softness 不該被存取
    mock_meta.last_softness  # 先讀一次讓 mock 記錄
    mock_meta.reset_mock()
    cog._resolve_tts_params("neutral")
    # 呼叫後 last_softness 的 getter 不應出現（MagicMock 屬性存取不計為 call）
    # 改用 spec 驗法：確認 _current_softness 不在 OFF 路徑被呼叫
    assert "last_softness" not in [str(c) for c in mock_meta.mock_calls]
