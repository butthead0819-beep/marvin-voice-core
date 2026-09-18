"""Unit tests for associative_curation.py — 對話關聯選曲與歌詞金句 DJ 模組。"""
import pytest
from associative_curation import (
    AssociativePick,
    build_associative_prompt,
    parse_associative_pick,
    curate_associative_song,
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
    pick = parse_associative_pick(raw)
    assert pick is not None
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
    pick = parse_associative_pick(raw)
    assert pick is not None
    assert pick.artist == "茄子蛋"
    assert pick.song == "浪子回頭"


def test_parse_associative_pick_invalid_or_missing_fields():
    # 缺歌名
    bad1 = '{"artist": "周杰倫", "song": ""}'
    assert parse_associative_pick(bad1) is None

    # 非 JSON
    bad2 = "這不是 JSON"
    assert parse_associative_pick(bad2) is None

    # 空字串
    assert parse_associative_pick("") is None


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

    pick = await curate_associative_song(
        utterances,
        core_artists=["美秀集團"],
        exclude_titles=[],
        members=["showay"],
        call_fn=mock_call_fn,
        min_utterances=2,
    )

    assert pick is not None
    assert pick.artist == "美秀集團"
    assert pick.song == "手機錢包鑰匙菸"


@pytest.mark.asyncio
async def test_curate_associative_song_insufficient_utterances():
    async def mock_call_fn(content, system=None, **kwargs):
        return "{}"

    # 只有 1 句，低於門檻
    utterances = [{"speaker": "A", "text": "嗨"}]
    pick = await curate_associative_song(
        utterances,
        core_artists=[],
        exclude_titles=[],
        members=[],
        call_fn=mock_call_fn,
        min_utterances=3,
    )
    assert pick is None


@pytest.mark.asyncio
async def test_curate_associative_song_call_fn_failure():
    async def mock_call_fn_fail(content, system=None, **kwargs):
        raise RuntimeError("LLM timeout")

    utterances = [
        {"speaker": "A", "text": "今天下大雨"},
        {"speaker": "B", "text": "全身都濕透了"},
        {"speaker": "A", "text": "真的煩死了"},
    ]
    pick = await curate_associative_song(
        utterances,
        core_artists=[],
        exclude_titles=[],
        members=[],
        call_fn=mock_call_fn_fail,
    )
    assert pick is None
