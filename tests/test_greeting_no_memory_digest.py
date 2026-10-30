"""進場招呼（player_greeting / player_greeting_rhyme）不得注入 highlight/impression：

萃取曾經把別人的家庭債務寫進大肚的紀錄，導致招呼語連續 6 天當眾講大肚「債務壓力」。
定案：進場招呼不再使用 highlight 和 impression，只用 3 天內的原話。
其他 layer（例如 qa_persona）行為不變，仍會注入 highlight/impression。
"""
from unittest.mock import MagicMock

import pytest

from marvin_prompts import PromptManager


def _make_memory_manager():
    mm = MagicMock()
    mm.get_player_memory.return_value = {
        "highlight_of_the_day": "HL_SENTINEL_債務",
        "relationship_stage": "陌生人",
    }
    mm.get_player_impression.return_value = "IMP_SENTINEL_債務"
    mm.get_rich_context.return_value = ""
    return mm


@pytest.mark.parametrize("layer", ["player_greeting", "player_greeting_rhyme"])
def test_greeting_layers_exclude_highlight_and_impression(layer):
    pm = PromptManager()
    mm = _make_memory_manager()

    result = pm.get_instruction(layer, dna=None, speaker="大肚", memory_manager=mm)

    assert "HL_SENTINEL" not in result
    assert "IMP_SENTINEL" not in result


def test_qa_persona_still_includes_highlight_and_impression():
    pm = PromptManager()
    mm = _make_memory_manager()

    result = pm.get_instruction("qa_persona", dna=None, speaker="大肚", memory_manager=mm)

    assert "HL_SENTINEL" in result
    assert "IMP_SENTINEL" in result


def test_player_greeting_rhyme_example_line_removed():
    pm = PromptManager()

    result = pm.get_instruction("player_greeting_rhyme")

    assert "大肚來報到" not in result
