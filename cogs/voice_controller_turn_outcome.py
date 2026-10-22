"""
TurnOutcomeMixin —— VoiceController 的喚醒候選守衛判斷紀錄接線（turn_outcome.py）。

抽成獨立 mixin（棘輪只准降，見 tests/test_voice_controller_size_budget.py）。
"""
from __future__ import annotations

import turn_outcome


class TurnOutcomeMixin:
    def _note_gate_decision(self, speaker, timestamp, track, is_wake_check, is_text_input, reason, confidence) -> None:
        turn_outcome.write_gate_decision(
            speaker=speaker, ts=timestamp,
            track="text" if is_text_input else ("wake_check" if is_wake_check else (track or "A")),
            reason=reason, iba_confidence=confidence)
