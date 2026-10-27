"""Unit tests for associative_curation.py — 對話關聯選曲與歌詞金句 DJ 模組。"""
import pytest
from associative_curation import (
    AssociativePick,
    _ASSOCIATIVE_SYS_PROMPT,
    build_associative_prompt,
    parse_associative_picks,
    curate_associative_song,
    local_associative_candidates,
)


def test_build_associative_prompt_structure():
    utterances = [
        {"speaker": "狗與露", "text": "手機錢包鑰匙煙"},
        {"speaker": "showay", "text": "還有打火機"},
        {"speaker": "showay", "text": "我的詞被偷走了"},
    ]
    core_artists = ["美秀集團", "告五人", "草東沒有派對"]
    exclude_titles = ["捲菸", "披星戴月的想你"]
    members = ["狗與露", "showay"]

    sys_p, user_p = build_associative_prompt(
        utterances,
        core_artists=core_artists,
        exclude_titles=exclude_titles,
        members=members,
    )

    # 驗證 System prompt 包含核心維度與 45-55 字規則
    assert "歌詞" in sys_p or "金句" in sys_p
    assert "45-55" in sys_p or "45~55" in sys_p
    assert "JSON" in sys_p

    # 驗證 User prompt 包含對話內容與上下文
    assert "手機錢包鑰匙煙" in user_p
    assert "我的詞被偷走了" in user_p
    assert "美秀集團" in user_p
    assert "捲菸" in user_p
    assert "showay" in user_p


def test_parse_associative_pick_valid_json():
    raw = """
    {
        "observed_topic": "出門口訣被寫成歌，抱怨詞被偷走",
        "target_lyric": "手機錢包鑰匙菸，反覆唸幾遍",
        "artist": "美秀集團",
        "song": "手機錢包鑰匙菸",
        "reason": "剛才聊到出門口訣被偷走，這首歌是完美呼應",
        "dj_line": "剛才聽showay抱怨出門默念的口訣被樂團偷去寫歌。美秀集團這首手機錢包鑰匙菸直接奉上，出門前口袋再拍兩下吧。"
    }
    """
    picks = parse_associative_picks(raw)
    assert len(picks) == 1
    pick = picks[0]
    assert isinstance(pick, AssociativePick)
    assert pick.artist == "美秀集團"
    assert pick.song == "手機錢包鑰匙菸"
    assert "手機錢包鑰匙菸" in pick.target_lyric
    assert pick.observed_topic == "出門口訣被寫成歌，抱怨詞被偷走"
    assert len(pick.dj_line) > 20


def test_parse_associative_pick_markdown_wrapped():
    raw = """```json
    {
        "observed_topic": "大肚想喝酒被放鳥",
        "target_lyric": "菸一支一支一支的點，酒一杯一杯一杯的乾",
        "artist": "茄子蛋",
        "song": "浪子回頭",
        "reason": "全場只有大肚放假想喝酒，其他人都不能喝",
        "dj_line": "大肚一進語音就瘋狂揪人喝酒，結果全場只有他明天放假。茄子蛋這首浪子回頭，留給大肚自己乾一杯。"
    }
    ```"""
    picks = parse_associative_picks(raw)
    assert len(picks) == 1
    assert picks[0].artist == "茄子蛋"
    assert picks[0].song == "浪子回頭"


def test_parse_associative_pick_invalid_or_missing_fields():
    # 缺歌名
    bad1 = '{"artist": "周杰倫", "song": ""}'
    assert parse_associative_picks(bad1) == []

    # 非 JSON
    bad2 = "這不是 JSON"
    assert parse_associative_picks(bad2) == []

    # 空字串
    assert parse_associative_picks("") == []


def test_parse_associative_picks_multi():
    raw = """
    {
        "observed_topic": "出門口訣",
        "picks": [
            {"artist": "A", "song": "歌A", "local": 1, "dj_line": "x"},
            {"artist": "B", "song": "歌B", "dj_line": "y"},
            {"artist": "C", "song": "歌C", "dj_line": "z"}
        ]
    }
    """
    picks = parse_associative_picks(raw, n_local=3)
    assert len(picks) == 3
    assert picks[0].local_index == 1
    assert picks[1].local_index is None
    assert picks[2].local_index is None


def test_parse_associative_picks_local_out_of_range_or_bool():
    raw_oor = '{"picks": [{"artist": "A", "song": "歌A", "local": 5, "dj_line": "x"}]}'
    picks = parse_associative_picks(raw_oor, n_local=3)
    assert picks[0].local_index is None

    raw_bool = '{"picks": [{"artist": "A", "song": "歌A", "local": true, "dj_line": "x"}]}'
    picks = parse_associative_picks(raw_bool, n_local=3)
    assert picks[0].local_index is None


def test_parse_associative_picks_caps_at_three():
    raw = """
    {"picks": [
        {"artist": "A", "song": "歌A", "dj_line": "a"},
        {"artist": "B", "song": "歌B", "dj_line": "b"},
        {"artist": "C", "song": "歌C", "dj_line": "c"},
        {"artist": "D", "song": "歌D", "dj_line": "d"}
    ]}
    """
    picks = parse_associative_picks(raw)
    assert len(picks) == 3
    assert [p.artist for p in picks] == ["A", "B", "C"]


def test_parse_associative_picks_skips_item_missing_song():
    raw = """
    {"picks": [
        {"artist": "A", "song": "", "dj_line": "a"},
        {"artist": "B", "song": "歌B", "dj_line": "b"}
    ]}
    """
    picks = parse_associative_picks(raw)
    assert len(picks) == 1
    assert picks[0].artist == "B"


def test_parse_associative_picks_empty_list():
    assert parse_associative_picks('{"picks": []}') == []


def test_parse_associative_picks_item_uses_top_level_observed_topic():
    raw = """
    {"observed_topic": "共同話題", "picks": [{"artist": "A", "song": "歌A", "dj_line": "a"}]}
    """
    picks = parse_associative_picks(raw)
    assert picks[0].observed_topic == "共同話題"


@pytest.mark.asyncio
async def test_curate_associative_song_success():
    mock_resp = """
    {
        "observed_topic": "出門口訣",
        "target_lyric": "手機錢包鑰匙菸",
        "artist": "美秀集團",
        "song": "手機錢包鑰匙菸",
        "reason": "口訣呼應",
        "dj_line": "剛才聽showay說出門口訣被偷走。美秀集團這首手機錢包鑰匙菸奉上，口袋拍兩下吧。"
    }
    """

    async def mock_call_fn(content, system=None, **kwargs):
        return mock_resp

    utterances = [
        {"speaker": "狗與露", "text": "手機錢包鑰匙煙"},
        {"speaker": "showay", "text": "還有打火機"},
        {"speaker": "showay", "text": "我的詞被偷走了"},
    ]

    picks = await curate_associative_song(
        utterances,
        core_artists=["美秀集團"],
        exclude_titles=[],
        members=["showay"],
        call_fn=mock_call_fn,
        min_utterances=2,
    )

    assert len(picks) == 1
    assert picks[0].artist == "美秀集團"
    assert picks[0].song == "手機錢包鑰匙菸"


@pytest.mark.asyncio
async def test_curate_associative_song_insufficient_utterances():
    async def mock_call_fn(content, system=None, **kwargs):
        return "{}"

    # 只有 1 句，低於門檻
    utterances = [{"speaker": "A", "text": "嗨"}]
    picks = await curate_associative_song(
        utterances,
        core_artists=[],
        exclude_titles=[],
        members=[],
        call_fn=mock_call_fn,
        min_utterances=3,
    )
    assert picks == []


@pytest.mark.asyncio
async def test_curate_associative_song_call_fn_failure():
    async def mock_call_fn_fail(content, system=None, **kwargs):
        raise RuntimeError("LLM timeout")

    utterances = [
        {"speaker": "A", "text": "今天下大雨"},
        {"speaker": "B", "text": "全身都濕透了"},
        {"speaker": "A", "text": "真的煩死了"},
    ]
    picks = await curate_associative_song(
        utterances,
        core_artists=[],
        exclude_titles=[],
        members=[],
        call_fn=mock_call_fn_fail,
    )
    assert picks == []


def test_build_associative_prompt_includes_recent_picks_when_given():
    utterances = [{"speaker": "showay", "text": "今天好累"}]

    _, user_p_with = build_associative_prompt(
        utterances,
        core_artists=[],
        exclude_titles=[],
        members=["showay"],
        recent_picks=["阿杜 - 他一定很愛你"],
    )
    assert "最近 30 天已經選過" in user_p_with
    assert "阿杜 - 他一定很愛你" in user_p_with

    _, user_p_without = build_associative_prompt(
        utterances,
        core_artists=[],
        exclude_titles=[],
        members=["showay"],
    )
    assert "最近 30 天" not in user_p_without


def test_associative_sys_prompt_has_no_hardcoded_song_examples():
    for banned in ("他一定很愛你", "早安晨之美", "下雨天", "Last Dance", "《"):
        assert banned not in _ASSOCIATIVE_SYS_PROMPT


def test_associative_sys_prompt_has_local_pool_and_no_emotion_leak_rule():
    assert "本地歌單" in _ASSOCIATIVE_SYS_PROMPT
    assert "picks" in _ASSOCIATIVE_SYS_PROMPT
    assert "不准提到任何人的情緒或心情" in _ASSOCIATIVE_SYS_PROMPT


def test_build_associative_prompt_includes_local_titles_and_mood_hint():
    utterances = [{"speaker": "showay", "text": "今天好累"}]

    _, user_p_with = build_associative_prompt(
        utterances,
        core_artists=[],
        exclude_titles=[],
        members=["showay"],
        local_titles=["周杰倫 - 晴天", "告五人 - 披星戴月的想你"],
        mood_hint="戲謔爭執",
    )
    assert "0. 周杰倫 - 晴天" in user_p_with
    assert "1. 告五人 - 披星戴月的想你" in user_p_with
    assert "戲謔爭執" in user_p_with
    assert "不准提到" in user_p_with

    _, user_p_without = build_associative_prompt(
        utterances,
        core_artists=[],
        exclude_titles=[],
        members=["showay"],
    )
    assert "本地歌單" not in user_p_without
    assert "現場氣氛" not in user_p_without


def _song_entry(title, artist, requesters, total_plays, vid="AAAAAAAAAAA"):
    return {
        "title": title,
        "track": title,
        "artist": artist,
        "requesters": requesters,
        "total_plays": total_plays,
        "webpage_url": f"https://www.youtube.com/watch?v={vid}",
    }


def test_local_associative_candidates_filters_and_sorts():
    songs = {
        "a": _song_entry("歌A", "artistA", {"showay": 1}, 5, vid="AAAAAAAAAAA"),
        "b": _song_entry("歌B", "artistB", {"showay": 1}, 9, vid="BBBBBBBBBBB"),
        "c": _song_entry("歌C", "artistC", {"外人": 1}, 100, vid="CCCCCCCCCCC"),
        "d": _song_entry("排除歌", "artistD", {"showay": 1}, 50, vid="DDDDDDDDDDD"),
        "e": _song_entry("歌E", "artistE", {"showay": 1}, 999, vid="EEEEEEEEEEE"),
    }
    result = local_associative_candidates(
        songs,
        members=["showay"],
        exclude_titles=["排除歌"],
        excluded_vids={"EEEEEEEEEEE"},
    )
    assert [display for display, _ in result] == ["artistB - 歌B", "artistA - 歌A"]


def test_local_associative_candidates_non_dict_songs():
    from unittest.mock import MagicMock
    assert local_associative_candidates(MagicMock(), ["showay"], [], set()) == []


def test_local_associative_candidates_respects_limit():
    songs = {
        "a": _song_entry("歌A", "artistA", {"showay": 1}, 5, vid="AAAAAAAAAAA"),
        "b": _song_entry("歌B", "artistB", {"showay": 1}, 9, vid="BBBBBBBBBBB"),
    }
    result = local_associative_candidates(
        songs, members=["showay"], exclude_titles=[], excluded_vids=set(), limit=1,
    )
    assert [display for display, _ in result] == ["artistB - 歌B"]


# ── 主題連結 MVP：build_associative_prompt(themes=) 驗收 6 ──────────────────

def _fixed_prompt_inputs():
    return dict(
        utterances=[
            {"speaker": "狗與露", "text": "手機錢包鑰匙煙"},
            {"speaker": "showay", "text": "還有打火機"},
        ],
        core_artists=["美秀集團", "告五人"],
        exclude_titles=["捲菸"],
        members=["狗與露", "showay"],
    )


def test_build_associative_prompt_no_themes_is_byte_identical_to_before():
    inputs = _fixed_prompt_inputs()
    sys_p, user_p = build_associative_prompt(**inputs)
    sys_p2, user_p2 = build_associative_prompt(**inputs, themes=None)
    sys_p3, user_p3 = build_associative_prompt(**inputs, themes=[])

    assert sys_p == _ASSOCIATIVE_SYS_PROMPT
    assert sys_p2 == _ASSOCIATIVE_SYS_PROMPT
    assert sys_p3 == _ASSOCIATIVE_SYS_PROMPT
    assert user_p == user_p2 == user_p3
    assert "最近大家反覆聊到的話題" not in user_p


def test_build_associative_prompt_with_themes_adds_block_and_rule():
    inputs = _fixed_prompt_inputs()
    sys_p, user_p = build_associative_prompt(**inputs, themes=[("露營", "戶外過夜；延伸：遠離城市")])

    assert sys_p != _ASSOCIATIVE_SYS_PROMPT
    assert "source" in sys_p
    assert "tag:" in sys_p
    assert "最近大家反覆聊到的話題" in user_p
    assert "露營：戶外過夜；延伸：遠離城市" in user_p


# ── parse_associative_picks(allowed_tags=) 驗收 7 ───────────────────────────

def test_parse_associative_picks_source_tag_kept_when_allowed_and_local():
    raw = '{"picks": [{"artist": "A", "song": "歌A", "local": 0, "source": "tag:露營", "dj_line": "x"}]}'
    picks = parse_associative_picks(raw, n_local=1, allowed_tags={"露營"})
    assert len(picks) == 1
    assert picks[0].source == "tag"
    assert picks[0].tag == "露營"


def test_parse_associative_picks_source_tag_dropped_when_not_allowed():
    raw = '{"picks": [{"artist": "A", "song": "歌A", "local": 0, "source": "tag:搬家", "dj_line": "x"}]}'
    picks = parse_associative_picks(raw, n_local=1, allowed_tags={"露營"})
    assert picks == []


def test_parse_associative_picks_source_tag_dropped_when_no_local():
    raw = '{"picks": [{"artist": "A", "song": "歌A", "source": "tag:露營", "dj_line": "x"}]}'
    picks = parse_associative_picks(raw, n_local=0, allowed_tags={"露營"})
    assert picks == []


def test_parse_associative_picks_missing_source_defaults_now():
    raw = '{"picks": [{"artist": "A", "song": "歌A", "dj_line": "x"}]}'
    picks = parse_associative_picks(raw, n_local=0, allowed_tags={"露營"})
    assert len(picks) == 1
    assert picks[0].source == "now"
    assert picks[0].tag == ""
