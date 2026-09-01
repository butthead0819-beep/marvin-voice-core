"""audio_rescue_routing_corpus.jsonl 自檢：每個 expect 必須是真的曝給 Gemini 的
tool（agent__intent），或 just_chatting。intent 被改名時這裡會紅，提醒同步驗收集。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from intent_agents.audio_rescue_tools import ABSTAIN_TOOL_NAME, manifest_to_function_declarations

CORPUS = Path(__file__).resolve().parent / "fixtures" / "audio_rescue_routing_corpus.jsonl"


def _rows():
    for line in CORPUS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            yield json.loads(line)


def _exposed_tool_names() -> set[str]:
    from intent_agents.grounded_qa_agent import GroundedQAAgent
    from intent_agents.volume_agent import VolumeAgent
    from intent_agents.playback_control_agent import PlaybackControlAgent
    from intent_agents.music_agent_v2 import MusicAgentV2
    from intent_agents.find_song_agent import FindSongAgent

    ctrl = SimpleNamespace()
    agents = [GroundedQAAgent(ctrl), VolumeAgent(ctrl), PlaybackControlAgent(ctrl),
              MusicAgentV2(ctrl), FindSongAgent(ctrl)]
    manifest = {"version": "x", "agents": [
        {"name": a.name, "intents": [
            {"name": s.name, "required_slots": list(s.required_slots),
             "reason_template": s.reason_template, "description": s.manifest_description}
            for s in a.declare_intents()
        ]} for a in agents
    ]}
    return {d.name for d in manifest_to_function_declarations(manifest)}


def test_corpus_parses_and_has_all_boundary_categories():
    rows = list(_rows())
    assert len(rows) >= 20
    for r in rows:
        assert r["utterance"] and r["expect"] and r["note"]
    notes = " ".join(r["note"] for r in rows)
    for boundary in ("邊界1", "邊界2", "邊界3", "邊界4", "邊界5"):
        assert boundary in notes, f"驗收集缺 {boundary} 的 case"


def test_every_expect_is_a_real_exposed_tool_or_abstain():
    valid = _exposed_tool_names() | {ABSTAIN_TOOL_NAME}
    bad = sorted({r["expect"] for r in _rows()} - valid)
    assert not bad, f"驗收集的 expect 不是有效 tool（intent 改名了？）: {bad}"


def test_corpus_covers_all_five_opt_in_agents():
    agents_hit = {r["expect"].split("__")[0] for r in _rows() if "__" in r["expect"]}
    assert agents_hit == {"music", "grounded_qa", "playback_control", "volume", "find_song"}
