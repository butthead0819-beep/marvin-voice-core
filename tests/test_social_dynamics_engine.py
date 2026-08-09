"""
Unit tests for SocialDynamicsEngine (Spatial Intelligence & TTS Control Metadata Generator)
"""
import pytest
from social_dynamics_engine import SocialDynamicsEngine, TTSControl, SpatialContext


def test_social_dynamics_engine_default_context():
    engine = SocialDynamicsEngine()
    context = engine.get_spatial_context(room_mood="放鬆閒聊", active_speakers=["UserA"])
    assert isinstance(context, SpatialContext)
    assert context.detected_environment in ("CLEAN_ROOM", "LATE_NIGHT_CHILL", "PARTY_CROWD", "OUTDOOR_CAMP")


def test_tts_control_generation_late_night():
    engine = SocialDynamicsEngine()
    # Late night room atmosphere
    tts_control = engine.generate_tts_control(
        text="天色晚了，早點休息吧",
        target_user="UserA",
        room_mood="深夜極度平靜",
        is_intimate=True,
        speaker_positions={"UserA": -0.6}
    )

    assert isinstance(tts_control, TTSControl)
    assert tts_control.text == "天色晚了，早點休息吧"
    assert tts_control.speaking_rate <= 0.9  # Slower rate for late night
    assert tts_control.audio_effects["panning"] == -0.6
    assert tts_control.audio_effects["reverb_preset"] == "LATE_NIGHT_CHILL"


def test_tts_control_generation_party_crowd():
    engine = SocialDynamicsEngine()
    # Energetic / crowded room
    tts_control = engine.generate_tts_control(
        text="大家都嗨起來！這首放下去！",
        target_user="UserB",
        room_mood="熱鬧歡樂",
        is_intimate=False,
        speaker_positions={"UserB": 0.4}
    )

    assert isinstance(tts_control, TTSControl)
    assert tts_control.speaking_rate >= 1.05
    assert tts_control.audio_effects["panning"] == 0.4
    assert tts_control.audio_effects["clarity_boost_db"] > 0.0
    assert tts_control.audio_effects["reverb_preset"] == "PARTY_CROWD"


@pytest.mark.parametrize(
    "group_mood,expect_rate_faster,expect_pitch_lower",
    [
        ("興奮", True, False),
        ("低落", False, True),
        ("分歧", False, True),
        ("放鬆", None, None),
    ],
)
def test_tts_control_driven_by_group_mood_label(group_mood, expect_rate_faster, expect_pitch_lower):
    """RoomMoodState.group_mood 的四個固定標籤（放鬆/興奮/低落/分歧）需能直接驅動 TTS rate/pitch。"""
    engine = SocialDynamicsEngine()
    neutral = engine.generate_tts_control(text="測試", room_mood="放鬆")
    tts_control = engine.generate_tts_control(text="測試", room_mood=group_mood)

    assert isinstance(tts_control, TTSControl)
    if expect_rate_faster is True:
        assert tts_control.speaking_rate > neutral.speaking_rate
    if expect_pitch_lower is True:
        assert tts_control.pitch_offset_hz < neutral.pitch_offset_hz
    if group_mood == "放鬆":
        assert tts_control.speaking_rate == neutral.speaking_rate
        assert tts_control.pitch_offset_hz == neutral.pitch_offset_hz


def test_edge_tts_parameter_export():
    engine = SocialDynamicsEngine()
    tts_control = engine.generate_tts_control(
        text="這是一段測試文字",
        room_mood="深夜極度平靜",
        is_intimate=True
    )
    edge_params = tts_control.to_edge_tts_params()
    assert "rate" in edge_params
    assert "pitch" in edge_params
    assert "volume" in edge_params
    # Verify rate string formatting like '-15%' or '+10%'
    assert edge_params["rate"].endswith("%")
