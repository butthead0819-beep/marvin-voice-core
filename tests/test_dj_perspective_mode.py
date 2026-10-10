"""TDD — DJ 口白 perspective mode（捧了再翻）：本地主題閘門只注入命中的一條。"""
from __future__ import annotations

from persona_loader import load_dj_perspectives
from dj_narration_orchestrator import (
    DJMaterials,
    MODES,
    NarrationPlan,
    apply_side_effects,
    choose_mode,
    match_perspective_topic,
)
from dj_topic_selector import TopicCooldownStore


def test_load_dj_perspectives_shape():
    cfg = load_dj_perspectives()
    assert cfg["technique"].strip()
    topics = cfg["topics"]
    assert len(topics) == 7
    for name, t in topics.items():
        assert t["keywords"], name
        assert t["praise"].strip(), name
        assert t["flip"].strip(), name


def test_match_perspective_topic_hits_and_misses():
    assert match_perspective_topic(["禮拜五還要加班"]) == "上班"
    assert match_perspective_topic(["今天天空好藍"]) is None
    assert match_perspective_topic(["加班完去打遊戲"]) == "上班"  # yaml 順序：上班在遊戲前


def test_render_perspective_with_topic_includes_conv_and_technique_and_only_matched_topic():
    materials = DJMaterials(conv_lines=["Bob：「又要加班」"], perspective_topic="上班")
    lines = MODES["perspective"].render(None, materials)
    joined = "\n".join(lines)
    assert "Bob：「又要加班」" in joined
    cfg = load_dj_perspectives()
    assert cfg["technique"] in lines
    assert cfg["topics"]["上班"]["praise"] in joined
    assert cfg["topics"]["遊戲"]["praise"] not in joined


def test_render_perspective_without_topic_has_no_technique():
    materials = DJMaterials(conv_lines=[], perspective_topic=None)
    lines = MODES["perspective"].render(None, materials)
    cfg = load_dj_perspectives()
    assert cfg["technique"] not in lines
    assert any("串場方向" in l for l in lines)


class _StubRng:
    def __init__(self):
        self.pools = []

    def choices(self, pool, weights=None, k=1):
        self.pools.append(list(pool))
        return [pool[0]]


def test_select_mode_pool_includes_perspective_only_when_has_perspective(tmp_path):
    from dj_topic_selector import select_mode

    store = TopicCooldownStore(path=str(tmp_path / "cd.json"))
    stub = _StubRng()
    select_mode([], [], store, has_conversation=True, has_perspective=True, rng=stub)
    assert "perspective" in stub.pools[-1]

    store2 = TopicCooldownStore(path=str(tmp_path / "cd2.json"))
    stub2 = _StubRng()
    select_mode([], [], store2, has_conversation=True, has_perspective=False, rng=stub2)
    assert "perspective" not in stub2.pools[-1]


def test_choose_mode_forwards_has_perspective(tmp_path, monkeypatch):
    import dj_narration_orchestrator as orch

    captured = {}
    real_select_mode = orch.select_mode

    def _spy(*args, **kwargs):
        captured.update(kwargs)
        return real_select_mode(*args, **kwargs)

    monkeypatch.setattr(orch, "select_mode", _spy)

    store = TopicCooldownStore(path=str(tmp_path / "cd.json"))
    choose_mode(DJMaterials(has_conversation=True, perspective_topic=None), store)
    assert captured.get("has_perspective") is False

    captured.clear()
    store2 = TopicCooldownStore(path=str(tmp_path / "cd2.json"))
    choose_mode(DJMaterials(has_conversation=True, perspective_topic="上班"), store2)
    assert captured.get("has_perspective") is True


def test_apply_side_effects_perspective_includes_technique_conversation_excludes_it():
    entries = [{"speaker": "Bob", "text": "又要加班了"}]

    class _Bank:
        def is_consumed(self, entry):
            return False

        def mark_consumed(self, entries, ts):
            self.marked = entries

    bank = _Bank()
    materials = DJMaterials(perspective_topic="上班")
    cfg = load_dj_perspectives()

    plan = NarrationPlan(mode="perspective", topic=None, ctx_lines=[], tts_emotion="normal",
                          side_effects=["consume_conversation"])
    result = apply_side_effects(
        plan, materials, conv_entries=entries, heat_bank=lambda: bank,
        callback_src={}, consume_callback=lambda who, item: None,
    )
    assert cfg["technique"] in result.ctx_lines

    plan2 = NarrationPlan(mode="conversation", topic=None, ctx_lines=[], tts_emotion="normal",
                           side_effects=["consume_conversation"])
    result2 = apply_side_effects(
        plan2, materials, conv_entries=entries, heat_bank=lambda: bank,
        callback_src={}, consume_callback=lambda who, item: None,
    )
    assert cfg["technique"] not in result2.ctx_lines
