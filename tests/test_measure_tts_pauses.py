"""
docs/tts_breath_plan.md Phase 2：測 parse_silences 這個純函式。
不連網、不呼叫 ffmpeg——用寫死的 ffmpeg silencedetect stderr 樣本。
"""
from __future__ import annotations

from scripts.measure_tts_pauses import parse_silences

_STDERR_SAMPLE = """\
[silencedetect @ 0x7f8b1c004a00] silence_start: 0
[silencedetect @ 0x7f8b1c004a00] silence_end: 0.3 | silence_duration: 0.3
[silencedetect @ 0x7f8b1c004a00] silence_start: 1.234
[silencedetect @ 0x7f8b1c004a00] silence_end: 1.5 | silence_duration: 0.266
[silencedetect @ 0x7f8b1c004a00] silence_start: 2.9
[silencedetect @ 0x7f8b1c004a00] silence_end: 3.15 | silence_duration: 0.25
[silencedetect @ 0x7f8b1c004a00] silence_start: 3.95
[silencedetect @ 0x7f8b1c004a00] silence_end: 4.0 | silence_duration: 0.05
"""

_TOTAL_DUR = 4.0


def test_parse_silences_excludes_leading_and_trailing():
    result = parse_silences(_STDERR_SAMPLE, _TOTAL_DUR)
    assert result == [0.266, 0.25]
