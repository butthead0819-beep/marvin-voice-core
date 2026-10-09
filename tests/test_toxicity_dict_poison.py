"""2026-10-09 事故：性格突變時 LLM 回的 toxicity_reset 是 dict，直接寫進
self.dna["toxicity"]，夾值炸掉、save_dna 沒跑，記憶體裡的值永久壞掉，
之後每次 update_toxicity 都在 += 炸（24h 36 次）。"""
import pytest

from gemini_router_content import GeminiRouterContentMixin


def _make(toxicity, llm_reply):
    inst = GeminiRouterContentMixin.__new__(GeminiRouterContentMixin)
    inst.dna = {"persona_tag": "厭世機器人馬文", "toxicity": toxicity}
    inst.saved = []
    inst.save_dna = lambda dna: inst.saved.append(dict(dna))

    async def _call_llm(*a, **k):
        return llm_reply

    inst._call_llm = _call_llm
    return inst


@pytest.mark.asyncio
async def test_mutation_with_dict_reset_falls_back_to_int():
    inst = _make(1, '{"new_tag": "虛無", "toxicity_reset": {"value": 5}}')
    await inst.update_toxicity(-1)
    assert inst.dna["toxicity"] == 5
    assert inst.saved and inst.saved[-1]["toxicity"] == 5


@pytest.mark.asyncio
async def test_mutation_with_numeric_string_reset_is_coerced():
    inst = _make(1, '{"new_tag": "虛無", "toxicity_reset": "7"}')
    await inst.update_toxicity(-1)
    assert inst.dna["toxicity"] == 7


@pytest.mark.asyncio
async def test_already_poisoned_state_self_heals():
    inst = _make({"value": 5}, "")
    await inst.update_toxicity(1)
    assert isinstance(inst.dna["toxicity"], int)
    assert inst.saved[-1]["toxicity"] == inst.dna["toxicity"]
