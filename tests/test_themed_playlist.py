"""讀空氣主題歌單 Step 1-2：theme brief（純）+ LLM 策展 call（注入 call_fn）。

設計 doc：~/.gstack/projects/butthead0819-beep-marvin-voice-core/jackhuang-main-design-20260624-192239.md
"""
from __future__ import annotations

import datetime as _dt

import pytest

from themed_playlist import (
    ThemeBrief,
    ThemedSet,
    build_curation_prompt,
    curate_themed_set,
    gather_theme_brief,
    parse_themed_set,
)

_FP = {
    "core_artists": [["周杰倫", 41], ["陶喆", 27], ["關喆", 27], ["費玉清", 9]],
    "language": {"華語": 0.9, "英文": 0.08, "其他": 0.02},
}


def _ts(dt_obj):
    return dt_obj.strftime("%Y-%m-%d %H:%M:%S")


# ── Step 1: gather_theme_brief（純函式）──────────────────────────────────────

def test_gather_theme_brief_collects_recent_cores_and_taste():
    now = _dt.datetime(2026, 6, 24, 22, 0).timestamp()
    base = _dt.datetime(2026, 6, 24, 21, 0)
    entries = [
        (_ts(base), "聊行動電源自燃、Anker 大廠也出包"),
        (_ts(base + _dt.timedelta(minutes=20)), "回憶千禧年的華語情歌"),
    ]
    brief = gather_theme_brief(entries, _FP, ["狗與露", "showay"], now=now)
    assert isinstance(brief, ThemeBrief)
    assert any("情歌" in c for c in brief.cores)
    assert "周杰倫" in brief.core_artists
    assert brief.language_label == "華語"
    assert brief.members == ["狗與露", "showay"]


def test_gather_theme_brief_none_when_too_few_cores():
    """近窗內可用核心句 < min_cores → None（無可偵測主題 → caller fallback 單首 autopilot）。"""
    now = _dt.datetime(2026, 6, 24, 22, 0).timestamp()
    entries = [(_ts(_dt.datetime(2026, 6, 24, 21, 30)), "只有一段對話")]
    assert gather_theme_brief(entries, _FP, ["狗與露"], now=now, min_cores=2) is None


def test_gather_theme_brief_excludes_cores_outside_window():
    """窗外（3 小時前）的核心句不算進主題。"""
    now = _dt.datetime(2026, 6, 24, 22, 0).timestamp()
    entries = [
        (_ts(_dt.datetime(2026, 6, 24, 17, 0)), "下午聊的舊主題"),   # 5h 前，窗外
        (_ts(_dt.datetime(2026, 6, 24, 21, 0)), "晚上的新主題一"),
        (_ts(_dt.datetime(2026, 6, 24, 21, 30)), "晚上的新主題二"),
    ]
    brief = gather_theme_brief(entries, _FP, ["狗與露"], now=now, window_hours=3.0)
    assert brief is not None
    assert not any("下午" in c for c in brief.cores)
    assert len(brief.cores) == 2


# ── Step 2a: build_curation_prompt（純函式）──────────────────────────────────

def test_build_curation_prompt_includes_topic_taste_and_exclusions():
    brief = ThemeBrief(cores=["聊千禧情歌", "聊周杰倫"], core_artists=["周杰倫", "陶喆"],
                       language_label="華語", members=["狗與露"])
    system, user = build_curation_prompt(brief, ["晴天", "稻香"], set_size=6)
    assert "理由" in system          # 系統 prompt 要求每首給選歌理由
    assert "聊千禧情歌" in user        # 對話主題進 prompt
    assert "周杰倫" in user            # 口味歌手進 prompt
    assert "晴天" in user and "稻香" in user  # 排除清單進 prompt
    assert "6" in user                # set_size 進 prompt


# ── Step 2b: parse_themed_set（純函式）───────────────────────────────────────

def test_parse_themed_set_valid_json():
    resp = ('{"theme_title":"千禧深夜抒情","picks":['
            '{"artist":"周杰倫","song":"晴天","reason":"今晚聊到學生時代，這首最對味"},'
            '{"artist":"陶喆","song":"流沙","reason":"接著千禧 R&B 的氣口"}]}')
    s = parse_themed_set(resp)
    assert isinstance(s, ThemedSet)
    assert s.theme_title == "千禧深夜抒情"
    assert len(s.picks) == 2
    assert s.picks[0].artist == "周杰倫" and s.picks[0].song == "晴天"
    assert "今晚" in s.picks[0].reason


def test_parse_themed_set_drops_incomplete_picks():
    resp = ('{"theme_title":"x","picks":['
            '{"artist":"周杰倫","song":"晴天","reason":"r"},'
            '{"artist":"","song":"沒歌手","reason":"r"},'
            '{"song":"沒藝人欄"}]}')
    s = parse_themed_set(resp)
    assert len(s.picks) == 1   # 缺 artist/song 的被丟掉


@pytest.mark.parametrize("resp", ["", "不是 JSON", '{"theme_title":"x","picks":[]}',
                                  '{"picks":[{"artist":"a","song":"b"}]}'])
def test_parse_themed_set_invalid_returns_none(resp):
    assert parse_themed_set(resp) is None   # 空/壞/無 title/無 picks → None


# ── Step 2c: curate_themed_set（協調，注入 call_fn）──────────────────────────

@pytest.mark.asyncio
async def test_curate_themed_set_calls_llm_and_parses():
    brief = ThemeBrief(cores=["聊千禧情歌"], core_artists=["周杰倫"],
                       language_label="華語", members=["狗與露"])
    captured = {}

    async def fake_call(content, *, system, **kw):
        captured["content"] = content
        captured["system"] = system
        return '{"theme_title":"千禧情歌","picks":[{"artist":"周杰倫","song":"晴天","reason":"r"}]}'

    s = await curate_themed_set(brief, ["稻香"], call_fn=fake_call)
    assert s.theme_title == "千禧情歌" and s.picks[0].song == "晴天"
    assert "稻香" in captured["content"]   # 排除清單真的進了 prompt


@pytest.mark.asyncio
async def test_curate_themed_set_none_brief_returns_none():
    async def fake_call(content, *, system, **kw):  # 不該被呼叫
        raise AssertionError("brief=None 不該打 LLM")
    assert await curate_themed_set(None, [], call_fn=fake_call) is None


@pytest.mark.asyncio
async def test_curate_themed_set_llm_failure_returns_none():
    async def fake_call(content, *, system, **kw):
        return None   # LLM 全 model 失敗
    brief = ThemeBrief(cores=["x", "y"], core_artists=[], language_label="華語", members=[])
    assert await curate_themed_set(brief, [], call_fn=fake_call) is None
