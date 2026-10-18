"""TDD: autopilot DJ 串場不准講成「某人點的」（2026-10-12 使用者定）。

autopilot 自動選歌時 requested_by 以 "Marvin" 開頭（不是真人點播），但 LLM 口白常講
「狗與露剛才點名要聽這首」「showay又點這首了」——沒人點過。只准「X 常聽」「放給 X」；
真人點播（requested_by 不以 "Marvin" 開頭）行為不變。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from dj_prompt_builder import claims_frequent_request, claims_request
from dj_social_affinity import find_song_social_affinity
from persona_loader import load_dj_templates
from cogs.music_cog import MusicCog


# ── claims_request ──────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "狗與露剛才點名要聽這首，看來兩位對這份默契特別有感。不需要多說，音樂下去就知道，鳳飛飛的《家後》。",
    "showay又點這首了，聽得出來對「黑暗中美不美」很有感。",
    "Showay 點過的《如果可以 - From THE FIRST TAKE》，這首留給你。",
    "這是狗與露點的《能不能》。",
])
def test_claims_request_detects_request_claims(text):
    assert claims_request(text) is True


@pytest.mark.parametrize("text", [
    "來聽這首《命運點歌》，這首給你。",
    "放鬆點，十二點了，零點幾秒，看你又點頭了。",
    "showay 常聽的歌，這首放給你，《愛的初體驗》。",
    "忙完十一點發車跟十二點到三點的那些亂事，辛苦了。",
    "",
])
def test_claims_request_allows_non_claim_text(text):
    assert claims_request(text) is False


# ── _dj_source_line ──────────────────────────────────────────────────────

def test_dj_source_line_human_requester():
    line = MusicCog._dj_source_line("狗與露")
    assert line.startswith("點播來源：狗與露 親自點的")
    assert "常聽" in line


def test_dj_source_line_autopilot_for_someone():
    line = MusicCog._dj_source_line("Marvin推薦（為showay）")
    assert "挑給 showay" in line
    assert "點播者" not in line


def test_dj_source_line_autopilot_generic():
    line = MusicCog._dj_source_line("Marvin推薦（點給大家）")
    assert line == "點播來源：Marvin 自動選歌，沒有人點播，不准說成任何人點的"


# ── _autopilot_pick_reason ───────────────────────────────────────────────

def test_autopilot_pick_reason_long_tail_has_no_request_claim():
    info = {"_lane": "long_tail", "_spotlight": "X"}
    reason = MusicCog._autopilot_pick_reason(info)
    assert "點" not in reason


def test_autopilot_pick_reason_anchor_has_no_request_claim():
    info = {"_anchor_title": "Y", "_spotlight": "X"}
    reason = MusicCog._autopilot_pick_reason(info)
    assert "點" not in reason


def test_autopilot_pick_reason_explanation_rewrites_claim_words():
    info = {"_explanation": "這首你點過 5 次，是你的老朋友了"}
    assert MusicCog._autopilot_pick_reason(info) == "這首你聽過 5 次，是你的老朋友了"


# ── find_song_social_affinity（bot requester）───────────────────────────

def test_social_affinity_bot_requester_uses_changting():
    import tempfile
    from music_memory import MusicMemory

    songs = {
        "https://www.youtube.com/watch?v=song3": {
            "title": "稻香",
            "uploader": "周杰倫",
            "webpage_url": "https://www.youtube.com/watch?v=song3",
            "total_plays": 3,
            "plays": [{"by": "Marvin推薦（為weakgogo）", "ts": 0}],
            "requesters": {"Bob": 3},
            "likes": {},
            "reactions": {},
        }
    }
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        mm = MusicMemory(tmp.name)
        mm._data = {"songs": songs, "recommendations": {}}

    info = {"title": "稻香", "webpage_url": "https://www.youtube.com/watch?v=song3"}
    affinity = find_song_social_affinity(
        mm, info, requester="Marvin推薦（為weakgogo）", present_members={"Bob"}
    )
    assert affinity == "在場的 Bob 常聽這首"


# ── autopilot_phrases 模板全檢 ────────────────────────────────────────────

def test_autopilot_phrase_templates_contain_no_request_claim():
    templates = load_dj_templates()
    pool = templates.get("autopilot_phrases", {})
    for lane, phrases in pool.items():
        for phrase in phrases:
            assert "點" not in phrase, f"lane={lane} 模板含「點」: {phrase!r}"


# ── 整合：autopilot vs 真人點播 ───────────────────────────────────────────

def _make_cog():
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.tts_engine = MagicMock()
    bot.tts_engine.generate_audio = AsyncMock(return_value="/tmp/dj_audio.opus")
    bot.tts_engine.get_estimated_duration = MagicMock(return_value=3.0)
    bot.router = MagicMock()
    bot.engine = MagicMock()
    bot.engine.conv_buffer = MagicMock()
    bot.engine.conv_buffer.get_last_n_utterances = MagicMock(return_value=[])
    bot.engine.post_summon_callback = None

    bot.music_memory = MagicMock()
    bot.music_memory._key = MagicMock(return_value="song_key_xyz")
    bot.music_memory._data = {"songs": {}}
    bot.music_memory.time_slot = MagicMock(return_value="深夜")

    cog = MusicCog(bot)
    cog._enable_dj_news_fetch = False
    return cog


def _info(title="周杰倫 - 夜曲", requester="大肚"):
    return {
        "title": title,
        "uploader": "周杰倫",
        "requested_by": requester,
        "url": "https://example/x",
    }


@pytest.mark.asyncio
async def test_autopilot_dj_llm_claim_gets_rejected():
    """autopilot 歌、LLM 講成「點名要聽」→ 不能原樣播出，且 context 不准寫「點播者：Marvin」。"""
    cog = _make_cog()
    claim_text = "狗與露剛才點名要聽這首，看來兩位對這份默契特別有感，鳳飛飛的《家後》。"
    cog.bot.router.generate_dynamic_system_msg = AsyncMock(return_value=claim_text)

    result = await cog._fetch_dj_interjection_raw(
        {"title": "鳳飛飛 - 家後", "uploader": "鳳飛飛",
         "requested_by": "Marvin推薦（為狗與露）", "url": "https://example/x",
         "_spotlight": "狗與露"}
    )
    assert result is not None
    assert result["text"] != claim_text

    call_kwargs = cog.bot.router.generate_dynamic_system_msg.call_args.kwargs
    context = call_kwargs.get("context", "")
    assert "點播來源：Marvin 自動選歌" in context
    assert "點播者：Marvin" not in context


@pytest.mark.asyncio
async def test_human_requester_dj_llm_claim_is_not_blocked():
    """真人點播：LLM 講「狗與露點的這首又來了」不受 autopilot 的 claim 擋拒。"""
    cog = _make_cog()
    human_text = "狗與露點的這首又來了，鳳飛飛的《家後》，大家一起聽。"
    cog.bot.router.generate_dynamic_system_msg = AsyncMock(return_value=human_text)

    result = await cog._fetch_dj_interjection_raw(
        _info(title="鳳飛飛 - 家後", requester="狗與露")
    )
    assert result is not None
    assert result["text"] == human_text


# ── claims_frequent_request（10/18 使用者定：真人點歌不准講成「常點/又點」）────

@pytest.mark.parametrize("text", [
    "weakgogo，看你又點了陶喆的歌。這節奏適合放空，就聽這首《沙灘》。",
    "狗與露，你跟 showay 整天重複點播這首，看來是很適合這氛圍，那就《小半》。",
    "狗與露，你總是在這裡點這首，聽聽這首《葉子》。",
    "showay 常點 陳華的歌。",
    "這首你點過 3 次了。",
])
def test_claims_frequent_request_detects_frequency_claims(text):
    assert claims_frequent_request(text) is True


@pytest.mark.parametrize("text", [
    "這首是狗與露點的《能不能》。",
    "showay 剛才點的，聽聽《Hotel California》。",
    "來聽這首《命運點歌》。",
    "看你又點頭了，十二點了。",
    "",
])
def test_claims_frequent_request_allows_non_frequency_text(text):
    assert claims_frequent_request(text) is False


# ── _dj_narration_requester ───────────────────────────────────────────────

def test_dj_narration_requester_personal_lane_uses_record_as():
    info = {"requested_by": "狗與露", "_record_as": "Marvin推薦（為狗與露）", "_lane": "personal"}
    assert MusicCog._dj_narration_requester(info) == "Marvin推薦（為狗與露）"


def test_dj_narration_requester_human_request():
    info = {"requested_by": "狗與露"}
    assert MusicCog._dj_narration_requester(info) == "狗與露"


def test_dj_narration_requester_personal_lane_without_record_as():
    info = {"requested_by": "狗與露", "_lane": "personal"}
    assert MusicCog._dj_narration_requester(info) == "狗與露"


def test_dj_narration_requester_group_resonance_lane_unaffected():
    info = {"requested_by": "Marvin推薦（點給大家）", "_lane": "group_resonance"}
    assert MusicCog._dj_narration_requester(info) == "Marvin推薦（點給大家）"


# ── _dj_requester_suffix 搭配 _dj_narration_requester ──────────────────────

def test_dj_requester_suffix_for_personal_lane_is_autopilot_phrase():
    info = {"requested_by": "狗與露", "_record_as": "Marvin推薦（為狗與露）", "_lane": "personal"}
    suffix = MusicCog._dj_requester_suffix(MusicCog._dj_narration_requester(info))
    assert suffix == "希望大家喜歡"


# ── find_song_social_affinity：真人點歌素材字串不准帶「點」────────────────

def test_social_affinity_human_requester_social_bridge_has_no_dian():
    import tempfile
    from music_memory import MusicMemory

    songs = {
        "https://www.youtube.com/watch?v=song4": {
            "title": "稻香",
            "uploader": "周杰倫",
            "webpage_url": "https://www.youtube.com/watch?v=song4",
            "total_plays": 3,
            "plays": [{"by": "狗與露", "ts": 0}],
            "requesters": {"showay": 2},
            "likes": {},
            "reactions": {},
        }
    }
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        mm = MusicMemory(tmp.name)
        mm._data = {"songs": songs, "recommendations": {}}

    info = {"title": "稻香", "webpage_url": "https://www.youtube.com/watch?v=song4"}
    affinity = find_song_social_affinity(
        mm, info, requester="狗與露", present_members={"showay"}
    )
    assert affinity == "這首 狗與露 常聽，在場的 showay 也常聽"
    assert "點" not in affinity


def test_social_affinity_artist_preference_has_no_dian():
    import tempfile
    from music_memory import MusicMemory

    songs = {
        "https://www.youtube.com/watch?v=song5": {
            "title": "周杰倫 - 晴天",
            "uploader": "周杰倫",
            "webpage_url": "https://www.youtube.com/watch?v=song5",
            "total_plays": 3,
            "plays": [],
            "requesters": {"狗與露": 3},
            "likes": {},
            "reactions": {},
        },
    }
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        mm = MusicMemory(tmp.name)
        mm._data = {"songs": songs, "recommendations": {}}

    info = {"title": "周杰倫 - 稻香", "webpage_url": "https://www.youtube.com/watch?v=song6"}
    affinity = find_song_social_affinity(
        mm, info, requester="狗與露", present_members=set()
    )
    assert affinity == "狗與露 常聽 周杰倫 的歌"
    assert "點" not in affinity
    assert "常聽" in affinity


# ── 接線：_fetch_dj_interjection_raw 真的用上個人歌單判定與頻率檢查 ──────────

@pytest.mark.asyncio
async def test_personal_shuffle_dj_llm_claim_gets_rejected():
    """個人歌單墊歌（requested_by 是真人名）：LLM 講「狗與露點的」→ 照 autopilot 擋掉，context 不寫「親自點的」。"""
    cog = _make_cog()
    claim_text = "狗與露點的《If Only…》，休息一下吧。"
    cog.bot.router.generate_dynamic_system_msg = AsyncMock(return_value=claim_text)

    result = await cog._fetch_dj_interjection_raw(
        {"title": "If Only…", "uploader": "x", "requested_by": "狗與露",
         "_record_as": "Marvin推薦（為狗與露）", "_lane": "personal",
         "url": "https://example/x"}
    )
    assert result is not None
    assert result["text"] != claim_text
    assert "點的" not in result["text"]

    context = cog.bot.router.generate_dynamic_system_msg.call_args.kwargs.get("context", "")
    assert "親自點的" not in context
    assert "點播來源：Marvin 自動選歌" in context


@pytest.mark.asyncio
async def test_human_requester_frequency_claim_gets_rejected():
    """真人點播：LLM 講「又點了」→ 不能原樣播出。"""
    cog = _make_cog()
    freq_text = "weakgogo，看你又點了陶喆的歌。這節奏適合放空，就聽這首《沙灘》。"
    cog.bot.router.generate_dynamic_system_msg = AsyncMock(return_value=freq_text)

    result = await cog._fetch_dj_interjection_raw(
        _info(title="陶喆 - 沙灘", requester="weakgogo")
    )
    assert result is not None
    assert result["text"] != freq_text
