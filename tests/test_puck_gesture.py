"""puck_gesture.py 單元測試（puck 頭像 2b：每則回覆開口做一個動作）。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import turn_outcome
import puck_gesture
from puck_gesture import (
    GESTURE_CODES,
    PuckGestureDirector,
    choose_reply_gesture,
    extract_gesture_tag,
    on_speech,
    strip_gesture_stream,
)


async def _agen(chunks):
    for c in chunks:
        yield c


# ---------- extract_gesture_tag ----------

def test_extract_leading_valid_tag():
    text, code = extract_gesture_tag("[[YES]]你好")
    assert text == "你好"
    assert code == "Y"


def test_extract_lowercase_and_inner_space():
    text, code = extract_gesture_tag("[[ yes ]]你好")
    assert text == "你好"
    assert code == "Y"


def test_extract_invalid_tag_removed_but_no_hint():
    text, code = extract_gesture_tag("[[DANCE]]你好")
    assert text == "你好"
    assert code is None


def test_extract_mid_string_tag_removed():
    text, code = extract_gesture_tag("你好[[NO]]啊")
    assert text == "你好啊"
    assert code == "N"


def test_extract_single_bracket_skip_tag_untouched():
    text, code = extract_gesture_tag("[SKIP]")
    assert text == "[SKIP]"
    assert code is None


def test_extract_multiple_tags_returns_first_valid_only():
    text, code = extract_gesture_tag("[[DANCE]][[YES]]你好[[NO]]")
    assert text == "你好"
    assert code == "Y"


# ---------- strip_gesture_stream ----------

@pytest.mark.asyncio
async def test_strip_gesture_stream_hint_split_across_chunks(tmp_path, monkeypatch):
    monkeypatch.setattr(turn_outcome, "OUTCOMES_PATH", tmp_path / "o.jsonl")
    result = {}

    async def body():
        out = []
        async for piece in strip_gesture_stream(_agen(["[[", "YE", "S]]對，", "你好"])):
            out.append(piece)
        rec = turn_outcome.claim_first_speech()
        result["hint"] = rec.get("_gesture_hint") if rec else None
        result["out"] = "".join(out)

    await turn_outcome.run_turn(body(), speaker="A", ts=1.0, discovered_by="wake_word")
    assert result["hint"] == "Y"
    assert result["out"] == "對，你好"


@pytest.mark.asyncio
async def test_strip_gesture_stream_midstream_tag_split_across_chunks_removed():
    out = []
    async for piece in strip_gesture_stream(_agen(["對，", "然後[[N", "O]]好"])):
        out.append(piece)
    joined = "".join(out)
    assert joined == "對，然後好"


@pytest.mark.asyncio
async def test_strip_gesture_stream_plain_text_concat_unchanged():
    out = []
    async for piece in strip_gesture_stream(_agen(["你好，", "今天", "天氣真好"])):
        out.append(piece)
    assert "".join(out) == "你好，今天天氣真好"


@pytest.mark.asyncio
async def test_strip_gesture_stream_midstream_tag_removed_no_hint(tmp_path, monkeypatch):
    monkeypatch.setattr(turn_outcome, "OUTCOMES_PATH", tmp_path / "o.jsonl")
    result = {}

    async def body():
        out = []
        async for piece in strip_gesture_stream(_agen(["你好", "[[NO]]啊"])):
            out.append(piece)
        rec = turn_outcome.claim_first_speech()
        result["hint"] = rec.get("_gesture_hint") if rec else None
        result["out"] = "".join(out)

    await turn_outcome.run_turn(body(), speaker="A", ts=1.0, discovered_by="wake_word")
    assert result["out"] == "你好啊"
    assert result["hint"] is None


@pytest.mark.asyncio
async def test_strip_gesture_stream_searching_passthrough():
    out = []
    async for piece in strip_gesture_stream(_agen(["你好", "__SEARCHING__", "更多"])):
        out.append(piece)
    assert "__SEARCHING__" in out
    assert "".join(out) == "你好__SEARCHING__更多"


@pytest.mark.asyncio
async def test_strip_gesture_stream_skip_tag_passthrough():
    out = []
    async for piece in strip_gesture_stream(_agen(["[SKIP]"])):
        out.append(piece)
    assert "".join(out) == "[SKIP]"


@pytest.mark.asyncio
async def test_strip_gesture_stream_no_empty_strings_yielded():
    out = []
    async for piece in strip_gesture_stream(_agen(["[[DANCE]]"])):
        out.append(piece)
    assert all(piece != "" for piece in out)


@pytest.mark.asyncio
async def test_strip_gesture_stream_unclosed_leading_tag_passes_through_verbatim():
    raw = "[[" + "A" * 30
    out = []
    async for piece in strip_gesture_stream(_agen([raw])):
        out.append(piece)
    assert "".join(out) == raw


# ---------- choose_reply_gesture ----------

def test_rule1_haoacuo_buocuo_prefix_is_yes():
    code, _ = choose_reply_gesture("沒錯啊", speaker=None, hint=None, hot_chat=False, recent=[])
    assert code == "Y"
    code2, _ = choose_reply_gesture("不錯喔", speaker=None, hint=None, hot_chat=False, recent=[])
    assert code2 == "Y"


def test_rule2_negation_prefix_is_no():
    code, _ = choose_reply_gesture("不要啦", speaker=None, hint=None, hot_chat=False, recent=[])
    assert code == "N"


def test_rule3_agreement_prefix_is_yes_but_haoxiang_excluded():
    code, _ = choose_reply_gesture("好啊", speaker=None, hint=None, hot_chat=False, recent=[])
    assert code == "Y"
    code2, _ = choose_reply_gesture("好像不太好聽", speaker=None, hint=None, hot_chat=False, recent=[])
    assert code2 != "Y"


def test_rule4_question_mark_ending_first_clause_is_tilt():
    # recent 含 T：若問號規則沒生效，輪替會給 Y，才分得出是規則命中而非輪替巧合
    code, _ = choose_reply_gesture("你確定？還是再想想", speaker=None, hint=None, hot_chat=False, recent=["T"])
    assert code == "T"


def test_rule5_sigh_markers_is_glance():
    code, _ = choose_reply_gesture("（嘆氣）真的假的", speaker=None, hint=None, hot_chat=False, recent=[])
    assert code == "G"


def test_rule6_long_sentence_is_ponder():
    text = "測" * 45
    code, _ = choose_reply_gesture(text, speaker=None, hint=None, hot_chat=False, recent=[])
    assert code == "P"


def test_rule7_hint_used_when_no_earlier_rule_matches():
    code, _ = choose_reply_gesture("測測測測測", speaker=None, hint="N", hot_chat=False, recent=[])
    assert code == "N"


def test_rule8_rotation_skips_recent():
    code, _ = choose_reply_gesture("測測測測測", speaker=None, hint=None, hot_chat=False, recent=["T", "Y"])
    assert code == "G"


def test_rule8_rotation_falls_back_to_tilt_when_all_recent():
    code, _ = choose_reply_gesture(
        "測測測測測", speaker=None, hint=None, hot_chat=False, recent=["T", "Y", "G", "P"])
    assert code == "T"


def test_speaker_prefix_stripped_before_rules():
    code, _ = choose_reply_gesture("showay，不要", speaker="showay", hint=None, hot_chat=False, recent=[])
    assert code == "N"


def test_stage_direction_prefix_stripped_before_rules():
    code, _ = choose_reply_gesture("（嘆氣）不要", speaker=None, hint=None, hot_chat=False, recent=[])
    assert code == "N"


def test_level_exclaim_is_3():
    _, level = choose_reply_gesture("好棒！", speaker=None, hint=None, hot_chat=False, recent=[])
    assert level == 3


def test_level_hot_chat_forces_3():
    _, level = choose_reply_gesture("還好", speaker=None, hint=None, hot_chat=True, recent=[])
    assert level == 3


def test_level_sigh_ellipsis_is_1():
    code, level = choose_reply_gesture("唉…沒事", speaker=None, hint=None, hot_chat=False, recent=[])
    assert code == "G"
    assert level == 1


def test_level_default_is_2():
    _, level = choose_reply_gesture("還好", speaker=None, hint=None, hot_chat=False, recent=[])
    assert level == 2


# ---------- PuckGestureDirector ----------

def test_director_field_format():
    d = PuckGestureDirector()
    d.publish("Y", 2)
    assert d.field == "1:Y2"


def test_director_seq_wraps_after_9999():
    d = PuckGestureDirector()
    d._seq = 9999
    d.publish("T", 1)
    assert d.field == "1:T1"


def test_director_recent_keeps_last_two():
    d = PuckGestureDirector()
    d.publish("T", 1)
    d.publish("Y", 2)
    d.publish("G", 3)
    assert d.recent == ["Y", "G"]


def test_director_field_none_before_first_publish():
    d = PuckGestureDirector()
    assert d.field is None


# ---------- on_speech ----------

def test_on_speech_outside_turn_is_noop():
    vc = SimpleNamespace()
    on_speech(vc, "好的", None)
    assert getattr(vc, "_puck_gesture_director", None) is None


@pytest.mark.asyncio
async def test_on_speech_voice_not_none_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(turn_outcome, "OUTCOMES_PATH", tmp_path / "o.jsonl")
    vc = SimpleNamespace()

    async def body():
        on_speech(vc, "好的", "some_voice")

    await turn_outcome.run_turn(body(), speaker="A", ts=1.0, discovered_by="wake_word")
    assert getattr(vc, "_puck_gesture_director", None) is None


@pytest.mark.asyncio
async def test_on_speech_first_speech_publishes_second_does_not(tmp_path, monkeypatch):
    monkeypatch.setattr(turn_outcome, "OUTCOMES_PATH", tmp_path / "o.jsonl")
    vc = SimpleNamespace()

    async def body():
        on_speech(vc, "好的", None)
        on_speech(vc, "不要", None)

    await turn_outcome.run_turn(body(), speaker="A", ts=1.0, discovered_by="wake_word")
    director = vc._puck_gesture_director
    assert director._seq == 1
    assert director.field.startswith("1:Y")


@pytest.mark.asyncio
async def test_on_speech_child_task_counts_as_same_turn(tmp_path, monkeypatch):
    monkeypatch.setattr(turn_outcome, "OUTCOMES_PATH", tmp_path / "o.jsonl")
    vc = SimpleNamespace()

    async def body():
        async def sub():
            on_speech(vc, "好的", None)
        await asyncio.create_task(sub())
        on_speech(vc, "不要", None)

    await turn_outcome.run_turn(body(), speaker="A", ts=1.0, discovered_by="wake_word")
    director = vc._puck_gesture_director
    assert director._seq == 1


@pytest.mark.asyncio
async def test_on_speech_missing_room_mood_store_no_exception(tmp_path, monkeypatch):
    monkeypatch.setattr(turn_outcome, "OUTCOMES_PATH", tmp_path / "o.jsonl")
    vc = SimpleNamespace()  # 沒有 _room_mood_store

    async def body():
        on_speech(vc, "好的", None)

    await turn_outcome.run_turn(body(), speaker="A", ts=1.0, discovered_by="wake_word")
    assert vc._puck_gesture_director.field is not None
