"""TDD — dj_presence_material.presence_materials（10/8 使用者定案）：
語音頻道在場成員的 Discord「正在玩」/「自訂狀態」轉成 DJ 串場素材句子。
"""
from __future__ import annotations

import types

import discord

from dj_presence_material import presence_materials


def _member(name, *, bot=False, activities=()):
    return types.SimpleNamespace(display_name=name, bot=bot, activities=activities)


def _playing(game):
    return types.SimpleNamespace(type=discord.ActivityType.playing, name=game)


def _custom(name=None, state=None):
    return types.SimpleNamespace(type=discord.ActivityType.custom, name=name, state=state)


def _other(activity_type):
    return types.SimpleNamespace(type=activity_type, name="x", state="x")


def test_playing_activity_becomes_sentence():
    members = [_member("小明", activities=[_playing("Ball X Pit")])]
    assert presence_materials(members) == ["小明 正在玩《Ball X Pit》"]


def test_custom_status_uses_name():
    members = [_member("小明", activities=[_custom(name="Grounded2")])]
    assert presence_materials(members) == ["小明 的 Discord 狀態寫著「Grounded2」"]


def test_custom_status_falls_back_to_state_when_name_none():
    members = [_member("小明", activities=[_custom(name=None, state="趕報告中")])]
    assert presence_materials(members) == ["小明 的 Discord 狀態寫著「趕報告中」"]


def test_custom_status_default_text_skipped():
    members = [_member("小明", activities=[_custom(name="Custom Status")])]
    assert presence_materials(members) == []


def test_custom_status_blank_skipped():
    members = [_member("小明", activities=[_custom(name="   ")])]
    assert presence_materials(members) == []


def test_custom_status_truncated_to_30_chars():
    long_text = "x" * 40
    members = [_member("小明", activities=[_custom(name=long_text)])]
    [line] = presence_materials(members)
    assert line == f"小明 的 Discord 狀態寫著「{'x' * 30}」"


def test_bot_member_skipped():
    members = [_member("機器人", bot=True, activities=[_playing("Game")])]
    assert presence_materials(members) == []


def test_other_activity_types_skipped():
    members = [_member(
        "小明",
        activities=[_other(discord.ActivityType.listening), _other(discord.ActivityType.streaming)],
    )]
    assert presence_materials(members) == []


def test_two_members_same_order_preserved():
    members = [
        _member("Alice", activities=[_playing("Game A")]),
        _member("Bob", activities=[_playing("Game B")]),
    ]
    assert presence_materials(members) == ["Alice 正在玩《Game A》", "Bob 正在玩《Game B》"]


def test_duplicate_sentences_across_members_deduped():
    members = [
        _member("Alice", activities=[_playing("Game A")]),
        _member("Alice", activities=[_playing("Game A")]),
    ]
    assert presence_materials(members) == ["Alice 正在玩《Game A》"]


def test_duplicate_identical_sentence_deduped_within_same_member():
    members = [_member("Alice", activities=[_playing("Game A"), _playing("Game A")])]
    assert presence_materials(members) == ["Alice 正在玩《Game A》"]


def test_members_none_returns_empty():
    assert presence_materials(None) == []


def test_members_iteration_error_returns_empty():
    class _Boom:
        def __iter__(self):
            raise RuntimeError("boom")

    assert presence_materials(_Boom()) == []
