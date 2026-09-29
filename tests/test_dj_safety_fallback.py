"""COMEDY_FALLBACK_SCRIPTS 資料合規：長度 10-120、零 FORBIDDEN_DJ_PHRASES 禁詞。"""
from __future__ import annotations

from dj_comedy_fallback import COMEDY_FALLBACK_SCRIPTS
from dj_prompt_builder import FORBIDDEN_DJ_PHRASES


def test_comedy_fallback_scripts_all_conform_to_quality_standards():
    assert len(COMEDY_FALLBACK_SCRIPTS) > 10
    for idx, script in enumerate(COMEDY_FALLBACK_SCRIPTS):
        assert 10 <= len(script) <= 120, f"Script [{idx}] 長度不符: {len(script)}"
        for fb in FORBIDDEN_DJ_PHRASES:
            assert fb not in script, f"Script [{idx}] 踩禁詞 '{fb}': {script}"
