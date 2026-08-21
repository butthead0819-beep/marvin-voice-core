"""_autopilot_pick_reason：DJ LLM 素材優先用 grounded 解釋（見 explanation_slotfill.py），
沒有才退回 lane 分流的固定樣版（2026-08-21，配合 T2 discovery evidence 落地）。
"""
from __future__ import annotations

import sys
from pathlib import Path

base = Path(__file__).parent.parent
if str(base) not in sys.path:
    sys.path.insert(0, str(base))

from cogs.music_cog import MusicCog


class TestAutopilotPickReason:
    def test_prefers_grounded_explanation_when_present(self):
        info = {
            "_lane": "discovery",
            "_spotlight": "jack",
            "_explanation": "YouTube Music 常把這首和你們聽過的《晴天》放在同一份歌單",
        }
        assert MusicCog._autopilot_pick_reason(info) == \
            "YouTube Music 常把這首和你們聽過的《晴天》放在同一份歌單"

    def test_falls_back_to_lane_template_without_explanation(self):
        info = {"_lane": "discovery", "_spotlight": "jack", "_explanation": None}
        assert MusicCog._autopilot_pick_reason(info) == "照 jack 的口味挖出來的新歌"

    def test_falls_back_when_explanation_key_missing(self):
        info = {"_lane": "group_resonance", "_spotlight": "jack"}
        assert MusicCog._autopilot_pick_reason(info) == "這首是大家都有共鳴的歌"

    def test_empty_string_explanation_falls_back(self):
        info = {"_lane": "long_tail", "_spotlight": "suki", "_explanation": ""}
        assert MusicCog._autopilot_pick_reason(info) == "suki 很久沒點到這首了"
