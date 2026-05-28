"""IntentAugmentation tests — LLM 擴 regex 工具的純函式層。

職責切分：
- extract_schemas_from_class(cls, controller_factory)
  動態實例化 agent class → 抓 declare_intents() 真實 schema（含 f-string resolved 後的 pattern）
- make_augment_prompt(schema)
  把 schema info 翻成 LLM user prompt
- parse_augment_response(raw_json)
  解 LLM JSON → {paraphrases, suggested_regex}
- format_report(suggestions)
  把多個 (schema, paraphrases) 轉成 markdown 給人工 review

不打真 LLM；script 入口在 scripts/augment_intent_patterns.py。
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

from intent_agents.base import DeclarativeIntentAgent, IntentSchema
from intent_agents.intent_augmentation import (
    AugmentSuggestion,
    SchemaInfo,
    extract_schemas_from_class,
    format_report,
    make_augment_prompt,
    parse_augment_response,
)


# ── 動態抽 schema：用 toy agent class 驗證 ────────────────────────────────────

class _ToyMusicAgent(DeclarativeIntentAgent):
    name = "toy_music"
    mode_compatible = frozenset({"normal"})

    def __init__(self, controller):
        self.controller = controller

    def declare_intents(self):
        kw = "下一首|跳過|next"  # 模擬 music_agent_v2 的 f-string 運算結果
        return [
            IntentSchema(
                name="skip_track",
                confidence=0.95,
                patterns=[f"(?P<kw>{kw})"],
                reason_template="control:skip",
            ),
            IntentSchema(
                name="strong_play",
                confidence=0.95,
                patterns=["播放|播一下"],
                required_slots=["song_choice"],
                reason_template="strong_play:{kw}",
            ),
        ]


class _StateOnlyAgent(DeclarativeIntentAgent):
    """像 busted/turtle — declare_intents 回 []，沒 pattern 可擴。"""
    name = "state_only"
    mode_compatible = frozenset({"game"})

    def __init__(self, controller):
        self.controller = controller

    def declare_intents(self):
        return []


def test_extract_resolves_fstring_patterns_to_real_strings():
    """f-string pattern 在 declare_intents 內運算，必須拿真實 string 不是模板。"""
    schemas = extract_schemas_from_class(_ToyMusicAgent, lambda: MagicMock())

    assert len(schemas) == 2
    skip = next(s for s in schemas if s.intent_name == "skip_track")
    assert skip.agent_name == "toy_music"
    # f-string 已 resolve，含真實 keyword 而非 {kw}
    assert "下一首" in skip.patterns[0]
    assert "跳過" in skip.patterns[0]
    assert skip.confidence == 0.95


def test_extract_returns_empty_for_state_checking_agent():
    """declare_intents 回 [] 的 agent（busted/turtle）→ 沒東西可擴，跳過。"""
    assert extract_schemas_from_class(_StateOnlyAgent, lambda: MagicMock()) == []


def test_extract_swallows_instantiation_exception():
    """部分 agent constructor 可能需要特殊 mock；炸了不該整批失敗。"""

    class _BrokenAgent(DeclarativeIntentAgent):
        name = "broken"
        def __init__(self, controller):
            raise RuntimeError("needs real bot")
        def declare_intents(self):
            return []

    # 應該回空 list，不該往上拋
    assert extract_schemas_from_class(_BrokenAgent, lambda: MagicMock()) == []


# ── prompt 建構 ──────────────────────────────────────────────────────────────

def test_make_augment_prompt_includes_intent_metadata():
    """LLM 要能看到 agent_name + intent_name + 至少一條 existing pattern 才能 on-domain 擴增。"""
    schema = SchemaInfo(
        agent_name="playback_control",
        intent_name="skip_track",
        confidence=0.95,
        patterns=("(?P<kw>下一首|跳過|next)",),
        reason_template="control:skip",
    )
    prompt = make_augment_prompt(schema)

    assert "skip_track" in prompt
    assert "playback_control" in prompt
    assert "下一首" in prompt or "跳過" in prompt  # 至少一條 pattern 浮現
    # 必須指示中文 + 數量
    assert "中文" in prompt
    assert any(ch in prompt for ch in ["10", "十"])  # paraphrase 數量


def test_make_augment_prompt_asks_for_json_with_required_keys():
    """LLM 必須吐結構化 JSON（paraphrases + suggested_regex），不然下游沒得 parse。"""
    schema = SchemaInfo("a", "b", 0.9, ("x",), "{name}")
    prompt = make_augment_prompt(schema)
    assert "paraphrases" in prompt
    assert "suggested_regex" in prompt
    assert "JSON" in prompt or "json" in prompt


# ── 解 LLM 回應 ──────────────────────────────────────────────────────────────

def test_parse_response_extracts_paraphrases_and_regex():
    raw = json.dumps({
        "paraphrases": ["換一首", "不要這個", "我聽夠了"],
        "suggested_regex": r"換一首|不要這個|聽夠了",
    })
    result = parse_augment_response(raw)
    assert result is not None
    assert result.paraphrases == ("換一首", "不要這個", "我聽夠了")
    assert result.suggested_regex == r"換一首|不要這個|聽夠了"


def test_parse_response_returns_none_on_malformed_json():
    """LLM 沒守 json mode → None，下游略過該 schema。"""
    assert parse_augment_response("not json") is None


def test_parse_response_returns_none_when_paraphrases_missing():
    """必要欄位缺失 → None；不接受半套輸出污染 markdown。"""
    raw = json.dumps({"suggested_regex": "x"})
    assert parse_augment_response(raw) is None


def test_parse_response_tolerates_missing_suggested_regex():
    """suggested_regex 是 nice-to-have；只要有 paraphrases 就算可用。"""
    raw = json.dumps({"paraphrases": ["a", "b"]})
    result = parse_augment_response(raw)
    assert result is not None
    assert result.paraphrases == ("a", "b")
    assert result.suggested_regex is None


def test_parse_response_drops_empty_paraphrase_strings():
    """LLM 偶爾吐空字串污染清單 → 過濾。"""
    raw = json.dumps({"paraphrases": ["x", "", "  ", "y"]})
    result = parse_augment_response(raw)
    assert result is not None
    assert result.paraphrases == ("x", "y")


# ── markdown report ─────────────────────────────────────────────────────────

def _schema(agent="playback_control", intent="skip_track"):
    return SchemaInfo(
        agent_name=agent, intent_name=intent, confidence=0.95,
        patterns=("(?P<kw>下一首|跳過)",), reason_template="control:skip",
    )


def test_format_report_groups_by_agent_then_intent():
    """ops 看報告先掃 agent，再看每個 intent 的 paraphrases；不是平鋪。"""
    suggestions = [
        AugmentSuggestion(
            schema=_schema(agent="playback_control", intent="skip_track"),
            paraphrases=("換一首", "下一個"),
            suggested_regex="換一首|下一個",
        ),
        AugmentSuggestion(
            schema=_schema(agent="playback_control", intent="stop_playback"),
            paraphrases=("停止", "別播了"),
            suggested_regex="停止|別播了",
        ),
        AugmentSuggestion(
            schema=_schema(agent="volume", intent="volume_down"),
            paraphrases=("小聲一點",),
            suggested_regex="小聲一點",
        ),
    ]
    md = format_report(suggestions)

    # agent 標題只各出現一次（用 \n 邊界避免被 ### subsection 子字串誤算）
    assert md.count("\n## playback_control\n") == 1
    assert md.count("\n## volume\n") == 1
    # 三個 intent 各一個 subsection
    assert "### skip_track" in md
    assert "### stop_playback" in md
    assert "### volume_down" in md


def test_format_report_includes_existing_pattern_for_context():
    """human reviewer 要看「現有 pattern」+「LLM 建議擴 pattern」對照，不能只給建議。"""
    suggestion = AugmentSuggestion(
        schema=_schema(),
        paraphrases=("換一首",),
        suggested_regex="換一首",
    )
    md = format_report([suggestion])

    assert "現有" in md or "existing" in md.lower() or "current" in md.lower()
    assert "下一首" in md  # 現有 pattern 內容必須露出


def test_format_report_includes_paraphrases_and_suggested_regex():
    """LLM 兩種輸出都該見到：paraphrases 給直觀感、suggested_regex 給可貼上 schema 的形式。"""
    suggestion = AugmentSuggestion(
        schema=_schema(),
        paraphrases=("換一首", "下一個"),
        suggested_regex="換一首|下一個",
    )
    md = format_report([suggestion])
    assert "換一首" in md
    assert "下一個" in md
    # suggested_regex 要在 code block 內方便複製
    assert "```" in md
    assert "換一首|下一個" in md


def test_format_report_handles_empty():
    """沒任何 suggestion（LLM 全部 fail）→ 仍要產一個可讀的 report 不是 crash。"""
    md = format_report([])
    assert isinstance(md, str)
    assert len(md) > 0  # 至少有 header
