"""
SocialDynamicsEngine — Spatial Intelligence & TTS Control Metadata Generator

Evaluates room mood, atmosphere, and speaker positions to produce TTSControl metadata
containing Edge TTS parameters (rate, pitch, volume) and DSP audio_effects
(panning, proximity_boost_db, clarity_boost_db, reverb_preset, distance).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class SpatialContext:
    temporal_density: float = 0.5
    target_user_pan: float = 0.0
    detected_environment: str = "CLEAN_ROOM"
    proximity_boost_db: float = 0.0
    clarity_boost_db: float = 0.0


@dataclass
class TTSControl:
    text: str
    emotion_style: str = "neutral"
    speaking_rate: float = 1.0
    pitch_offset_hz: float = 0.0
    volume_offset_percent: float = 0.0
    audio_effects: dict[str, Any] = field(default_factory=lambda: {
        "panning": 0.0,
        "proximity_boost_db": 0.0,
        "clarity_boost_db": 0.0,
        "reverb_preset": "CLEAN_ROOM",
        "distance": 0.0,
    })

    def to_edge_tts_params(self) -> dict[str, str]:
        """Convert rate, pitch, and volume into Edge TTS string flags (e.g. rate='+10%', pitch='-15Hz')."""
        rate_diff = int(round((self.speaking_rate - 1.0) * 100))
        rate_str = f"{rate_diff:+d}%" if rate_diff != 0 else "+0%"

        pitch_val = int(round(self.pitch_offset_hz))
        pitch_str = f"{pitch_val:+d}Hz" if pitch_val != 0 else "+0Hz"

        vol_val = int(round(self.volume_offset_percent))
        vol_str = f"{vol_val:+d}%" if vol_val != 0 else "+0%"

        return {
            "rate": rate_str,
            "pitch": pitch_str,
            "volume": vol_str,
        }


class SocialDynamicsEngine:
    def __init__(self):
        self._user_positions: dict[str, float] = {}

    def register_user_position(self, username: str, pan: float) -> None:
        """Register or update a user's virtual spatial panning angle (-1.0 to 1.0)."""
        self._user_positions[username] = max(-1.0, min(1.0, float(pan)))

    def get_spatial_context(
        self,
        room_mood: str = "放鬆閒聊",
        active_speakers: list[str] | None = None
    ) -> SpatialContext:
        """Construct SpatialContext from room mood and active speakers."""
        speakers = active_speakers or []
        density = min(1.0, len(speakers) * 0.3)

        if "深夜" in room_mood or "極度平靜" in room_mood or "1-on-1" in room_mood:
            env = "LATE_NIGHT_CHILL"
            prox = 3.0
            clarity = 0.0
        elif "露營" in room_mood or "戶外" in room_mood or "帳篷" in room_mood:
            env = "OUTDOOR_CAMP"
            prox = 2.0
            clarity = 0.0
        elif "熱鬧" in room_mood or "歡樂" in room_mood or "聚會" in room_mood or density > 0.6:
            env = "PARTY_CROWD"
            prox = 0.0
            clarity = 3.5
        else:
            env = "CLEAN_ROOM"
            prox = 1.0 if density < 0.2 else 0.0
            clarity = 1.5 if density > 0.5 else 0.0

        return SpatialContext(
            temporal_density=density,
            target_user_pan=0.0,
            detected_environment=env,
            proximity_boost_db=prox,
            clarity_boost_db=clarity,
        )

    def generate_tts_control(
        self,
        text: str,
        target_user: str | None = None,
        room_mood: str = "放鬆閒聊",
        is_intimate: bool = False,
        speaker_positions: dict[str, float] | None = None
    ) -> TTSControl:
        """Generate complete TTSControl metadata including Edge TTS parameters and audio FX."""
        positions = speaker_positions or self._user_positions
        pan = positions.get(target_user, 0.0) if target_user else 0.0

        # Base environment context
        if "深夜" in room_mood or is_intimate:
            env = "LATE_NIGHT_CHILL"
            rate = 0.85
            pitch = -12.0
            vol = -15.0
            prox_db = 3.0
            clarity_db = 0.0
            distance = 0.2 if target_user else 0.0
        elif "熱鬧" in room_mood or "歡樂" in room_mood or "聚會" in room_mood:
            env = "PARTY_CROWD"
            rate = 1.15
            pitch = 5.0
            vol = 0.0
            prox_db = 0.0
            clarity_db = 3.5
            distance = 0.0
        elif "露營" in room_mood or "戶外" in room_mood:
            env = "OUTDOOR_CAMP"
            rate = 1.0
            pitch = 0.0
            vol = 0.0
            prox_db = 2.0
            clarity_db = 0.0
            distance = 0.0
        else:
            env = "CLEAN_ROOM"
            rate = 1.0
            pitch = 0.0
            vol = 0.0
            prox_db = 0.0
            clarity_db = 1.0
            distance = 0.0

        audio_fx = {
            "panning": pan,
            "proximity_boost_db": prox_db,
            "clarity_boost_db": clarity_db,
            "reverb_preset": env,
            "distance": distance,
        }

        return TTSControl(
            text=text,
            emotion_style="intimate" if is_intimate else "neutral",
            speaking_rate=rate,
            pitch_offset_hz=pitch,
            volume_offset_percent=vol,
            audio_effects=audio_fx,
        )
