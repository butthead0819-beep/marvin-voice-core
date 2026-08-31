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
    # opt-in：只有填了 description 的 intent 會曝給 Gemini。
    return {
        "version": "2026-08-22",
        "agents": [
            {
                "name": "volume",
                "intents": [
                    {"name": "volume_down", "required_slots": [], "reason_template": "x",
                     "description": "調小聲"},
                    {"name": "volume_up", "required_slots": [], "reason_template": "x",
                     "description": "調大聲"},
                ],
            },
            {
                "name": "find_song",
                "intents": [
                    {"name": "find_artist", "required_slots": ["artist"], "reason_template": "x",
                     "description": "找某歌手的歌"},
                ],
            },
        ],
    }


def test_produces_one_declaration_per_described_manifest_intent():
    decls = manifest_to_function_declarations(_manifest())
    names = {d.name for d in decls}
    assert names == {"volume__volume_down", "volume__volume_up", "find_song__find_artist"}


def test_intent_without_description_is_not_exposed():
    """opt-in：沒填 manifest_description 的 intent 不曝給 Gemini（generic 預設對
    Gemini 沒有辨識價值，還會稀釋選擇；MusicAgentV2 只曝專用 rescue_play）。"""
    m = {"version": "x", "agents": [{"name": "music", "intents": [
        {"name": "strong_play", "required_slots": [], "reason_template": "x"},
        {"name": "weak_play_directional", "required_slots": ["directional_resolution"],
         "reason_template": "x", "description": "   "},  # 空白也算沒填
        {"name": "rescue_play", "required_slots": ["song_query"], "reason_template": "x",
         "description": "使用者要直接點一首指定的歌"},
    ]}]}
    decls = manifest_to_function_declarations(m)
    assert [d.name for d in decls] == ["music__rescue_play"]


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


def test_intent_description_flows_into_declaration():
    """填了 manifest_description 的 intent → Gemini function description 用它逐字。"""
    m = {"version": "x", "agents": [{"name": "grounded_qa", "intents": [
        {"name": "factual_question", "required_slots": ["topic"],
         "reason_template": "x", "description": "使用者在問需要查證的事實問題"},
    ]}]}
    decls = manifest_to_function_declarations(m)
    fq = next(d for d in decls if d.name == "grounded_qa__factual_question")
    assert fq.description == "使用者在問需要查證的事實問題"


def test_duplicate_intent_name_dedupes_to_one_declaration():
    """一個 agent 宣告兩個同名 IntentSchema（regex 路徑合法，first-match-wins，如
    PersonalShuffleAgent 的兩個 personal_shuffle_start）→ manifest 會帶兩筆同名
    intent。逐 schema 產 declaration 會吐兩個同名 tool → Gemini 整包 request 回
    400 INVALID_ARGUMENT「Duplicate function declaration」→ audio rescue 全失敗
    （2026-08-31 prod 實錄）。轉換層按 tool name 去重，保留第一個。"""
    m = {
        "version": "x",
        "agents": [{"name": "personal_shuffle", "intents": [
            {"name": "personal_shuffle_stop", "required_slots": [], "reason_template": "x",
             "description": "停止個人化洗歌"},
            {"name": "personal_shuffle_start", "required_slots": [], "reason_template": "x",
             "description": "開始個人化洗歌"},
            {"name": "personal_shuffle_start", "required_slots": [], "reason_template": "x",
             "description": "第二條 pattern 的同名 schema"},
        ]}],
    }
    decls = manifest_to_function_declarations(m)
    names = [d.name for d in decls]
    assert names.count("personal_shuffle__personal_shuffle_start") == 1
    assert len(decls) == 2
    kept = next(d for d in decls if d.name == "personal_shuffle__personal_shuffle_start")
    assert kept.description == "開始個人化洗歌"  # 保留第一個


def test_agent_name_containing_separator_raises():
    bad_manifest = {
        "version": "x",
        "agents": [{"name": "vol__ume", "intents": [
            {"name": "down", "required_slots": [], "reason_template": "x", "description": "調低"},
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
