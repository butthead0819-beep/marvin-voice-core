"""manifest_to_function_declarations / parse_tool_call — 純資料轉換測試。

不打 Gemini：只驗證 build_intent_manifest() 的輸出格式能正確轉成 Gemini
FunctionDeclaration，以及 tool call name 能正確反查回 (agent, intent)。
"""
from __future__ import annotations

import pytest

from intent_agents.audio_rescue_tools import (
    READONLY_FUNCTION_DECLARATIONS,
    READONLY_TOOL_NAMES,
    manifest_to_function_declarations,
    parse_tool_call,
)


def _manifest():
    return {
        "version": "2026-08-22",
        "agents": [
            {
                "name": "volume",
                "intents": [
                    {"name": "volume_down", "required_slots": [], "reason_template": "x"},
                    {"name": "volume_up", "required_slots": [], "reason_template": "x"},
                ],
            },
            {
                "name": "find_song",
                "intents": [
                    {"name": "find_artist", "required_slots": ["artist"], "reason_template": "x"},
                ],
            },
        ],
    }


def test_produces_one_declaration_per_manifest_intent():
    decls = manifest_to_function_declarations(_manifest())
    names = {d.name for d in decls}
    assert names == {"volume__volume_down", "volume__volume_up", "find_song__find_artist"}


def test_required_slots_mapped_into_schema():
    decls = manifest_to_function_declarations(_manifest())
    find_artist = next(d for d in decls if d.name == "find_song__find_artist")
    assert find_artist.parameters.required == ["artist"]
    assert "artist" in find_artist.parameters.properties


def test_no_slots_intent_has_no_required():
    decls = manifest_to_function_declarations(_manifest())
    volume_down = next(d for d in decls if d.name == "volume__volume_down")
    assert not volume_down.parameters.required


def test_empty_manifest_produces_empty_list():
    assert manifest_to_function_declarations({"version": "x", "agents": []}) == []


def test_agent_name_containing_separator_raises():
    bad_manifest = {
        "version": "x",
        "agents": [{"name": "vol__ume", "intents": [
            {"name": "down", "required_slots": [], "reason_template": "x"},
        ]}],
    }
    with pytest.raises(ValueError):
        manifest_to_function_declarations(bad_manifest)


class _FakeFunctionCall:
    def __init__(self, name, args=None):
        self.name = name
        self.args = args or {}


def test_parse_tool_call_round_trips_agent_and_intent():
    result = parse_tool_call(_FakeFunctionCall("volume__volume_down", {}))
    assert result == ("volume", "volume_down", {})


def test_parse_tool_call_preserves_args():
    result = parse_tool_call(_FakeFunctionCall("find_song__find_artist", {"artist": "周杰倫"}))
    assert result == ("find_song", "find_artist", {"artist": "周杰倫"})


def test_parse_tool_call_returns_none_for_malformed_name():
    assert parse_tool_call(_FakeFunctionCall("no_separator_here")) is None


def test_parse_tool_call_returns_none_when_name_missing():
    class _NoName:
        name = None
        args = {}
    assert parse_tool_call(_NoName()) is None


def test_readonly_tool_names_match_declarations():
    decl_names = {d.name for d in READONLY_FUNCTION_DECLARATIONS}
    assert decl_names == READONLY_TOOL_NAMES
