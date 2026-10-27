"""Unit tests for associative curation integration in MusicAutopilotMixin."""
import time
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

import associative_history
from associative_curation import AssociativePick


from cogs.music_cog_dj_lyrics import MusicDJLyricsMixin


@pytest.fixture(autouse=True)
def _isolate_associative_history(tmp_path, monkeypatch):
    monkeypatch.setattr(associative_history, "HISTORY_PATH", tmp_path / "associative_picks.jsonl")


class DummyAutopilotHost(MusicDJLyricsMixin):
    """模擬帶有 MusicAutopilotMixin 與 MusicDJLyricsMixin 的 Cog。"""
    def __init__(self):
        self.stream_queue = []
        self.bot = MagicMock()
        self.bot.music_memory = None
        self.bot.engine.conv_buffer.get_last_n_utterances.return_value = [
            {"speaker": "狗與露", "text": "手機錢包鑰匙煙", "timestamp": time.time() - 30},
            {"speaker": "showay", "text": "還有打火機", "timestamp": time.time() - 25},
            {"speaker": "showay", "text": "我的詞被偷走了", "timestamp": time.time() - 20},
        ]
        self._associative_cooldown_s = 900.0
        self._last_associative_pick_ts = 0.0
        self._round_size = 3

    def _associative_gate_open(self, now: float) -> bool:
        from cogs.music_cog_autopilot import MusicAutopilotMixin
        return MusicAutopilotMixin._associative_gate_open(self, now)

    def _check_song_duplicate(self, url="", title="", username="", webpage_url=""):
        return False

    def _republish_queue_snapshot(self):
        pass

    def _load_taste_fingerprint(self):
        return {"core_artists": [("美秀集團", 10)]}

    async def _resolve_yt_query(self, query: str):
        return {
            "title": "美秀集團 Amazing Show－手機錢包鑰匙菸",
            "url": "https://www.youtube.com/watch?v=dummy",
            "webpage_url": "https://www.youtube.com/watch?v=dummy",
            "duration": 210,
        }

    async def _try_associative_pick(self, members: list, exclude_titles: list, spotlight: str, mm) -> int:
        from cogs.music_cog_autopilot import MusicAutopilotMixin
        return await MusicAutopilotMixin._try_associative_pick(self, members, exclude_titles, spotlight, mm)

    def _load_qualifying_theme_tags(self, members, now):
        from cogs.music_cog_autopilot import MusicAutopilotMixin
        return MusicAutopilotMixin._load_qualifying_theme_tags(self, members, now)

    def _theme_entries_for_tags(self, qualifying, mm, exclude_titles, excluded_vids):
        from cogs.music_cog_autopilot import MusicAutopilotMixin
        return MusicAutopilotMixin._theme_entries_for_tags(self, qualifying, mm, exclude_titles, excluded_vids)


@pytest.mark.asyncio
async def test_associative_gate_cooldown():
    host = DummyAutopilotHost()
    now = 1000.0
    assert host._associative_gate_open(now) is True

    host._last_associative_pick_ts = now - 300.0  # 300s ago (< 900s)
    assert host._associative_gate_open(now) is False

    host._last_associative_pick_ts = now - 1000.0  # 1000s ago (>= 900s)
    assert host._associative_gate_open(now) is True


@pytest.mark.asyncio
async def test_associative_gate_env_disabled(monkeypatch):
    monkeypatch.setenv("ASSOCIATIVE_CURATION", "off")
    host = DummyAutopilotHost()
    assert host._associative_gate_open(time.time()) is False


@pytest.mark.asyncio
async def test_try_associative_pick_success(monkeypatch):
    monkeypatch.setenv("ASSOCIATIVE_CURATION", "on")
    host = DummyAutopilotHost()

    mock_pick = AssociativePick(
        observed_topic="出門口訣被偷",
        target_lyric="手機錢包鑰匙菸，反覆唸幾遍",
        artist="美秀集團",
        song="手機錢包鑰匙菸",
        reason="聊到出門口訣被寫成歌",
        dj_line="剛才聽showay抱怨出門默念的口訣被樂團偷去寫歌。美秀集團這首手機錢包鑰匙菸奉上，出門前口袋拍兩下吧。",
    )

    mm = MagicMock()
    mm.get_skipped_video_ids.return_value = set()
    mm.get_recently_played_video_ids.return_value = set()

    with patch("associative_curation.curate_associative_song", AsyncMock(return_value=[mock_pick])):
        n = await host._try_associative_pick(
            members=["showay", "狗與露"],
            exclude_titles=[],
            spotlight="showay",
            mm=mm,
        )

    assert n == 1
    assert len(host.stream_queue) == 1
    info = host.stream_queue[0]
    assert info["_lane"] == "associative"
    assert info["_dj_line"] == mock_pick.dj_line
    assert info["_target_lyric"] == mock_pick.target_lyric
    assert info["_explanation"] == mock_pick.reason
    assert host._last_associative_pick_ts > 0


@pytest.mark.asyncio
async def test_try_associative_pick_is_non_song_rejected(monkeypatch):
    monkeypatch.setenv("ASSOCIATIVE_CURATION", "on")
    host = DummyAutopilotHost()

    mock_pick = AssociativePick(
        observed_topic="怪談",
        target_lyric="無",
        artist="某Podcast",
        song="聊天大會",
        reason="無",
        dj_line="無",
    )

    mm = MagicMock()
    mm.get_skipped_video_ids.return_value = set()
    mm.get_recently_played_video_ids.return_value = set()

    async def _long_video(query):
        return {"title": "某Podcast 聊天大會 完整版", "url": "https://www.youtube.com/watch?v=long",
                "webpage_url": "https://www.youtube.com/watch?v=long", "duration": 3600}
    host._resolve_yt_query = _long_video

    with patch("associative_curation.curate_associative_song", AsyncMock(return_value=[mock_pick])):
        n = await host._try_associative_pick(
            members=["showay"],
            exclude_titles=[],
            spotlight="showay",
            mm=mm,
        )

    # 被品質閘擋掉，不應入隊
    assert n == 0
    assert len(host.stream_queue) == 0


@pytest.mark.asyncio
async def test_dj_interjection_uses_associative_dj_line():
    from cogs.music_cog_dj_lyrics import MusicDJLyricsMixin
    host = DummyAutopilotHost()
    host.bot.tts_engine.generate_audio = AsyncMock(return_value="/tmp/audio.mp3")
    host.bot.tts_engine.get_estimated_duration = MagicMock(return_value=8.0)
    host._vc = MagicMock(return_value=None)
    host._life_cores_async = AsyncMock(return_value=[])
    host._present_interests = MagicMock(return_value=[])
    host._fetch_news_items_async = AsyncMock(return_value=([], {}))
    from dj_topic_selector import TopicCooldownStore
    import tempfile
    store = TopicCooldownStore(tempfile.mktemp(suffix=".json"))
    host._dj_topic_store = MagicMock(return_value=store)
    host._current_season = MagicMock(return_value="秋天")
    host._city_label = MagicMock(return_value="台中")
    host._dj_clean_name = MagicMock(return_value=("手機錢包鑰匙菸", "美秀集團"))

    info = {
        "title": "手機錢包鑰匙菸",
        "artist": "美秀集團",
        "requested_by": "Marvin推薦（對話靈感）",
        "_lane": "associative",
        "_dj_line": "剛才聽showay抱怨出門口訣被偷。美秀集團這首手機錢包鑰匙菸奉上，出門前拍拍口袋吧。",
    }

    res = await MusicDJLyricsMixin._fetch_dj_interjection_raw(host, info)
    assert res is not None
    assert res["text"] == info["_dj_line"]
    assert res["audio_path"] == "/tmp/audio.mp3"



@pytest.mark.asyncio
async def test_associative_cooldown_starts_even_when_llm_returns_nothing(monkeypatch):
    """LLM 沒挑出歌也要冷卻，否則每輪 refill 都重打一次付費 LLM。"""
    monkeypatch.setenv("ASSOCIATIVE_CURATION", "on")
    host = DummyAutopilotHost()
    with patch("associative_curation.curate_associative_song", AsyncMock(return_value=[])):
        n = await host._try_associative_pick(members=["showay"], exclude_titles=[],
                                             spotlight="showay", mm=None)
    assert n == 0
    assert host._last_associative_pick_ts > 0
    assert host._associative_gate_open(time.time()) is False


@pytest.mark.asyncio
async def test_associative_paid_call_tagged_with_own_caller(monkeypatch):
    """付費呼叫必須以 caller="associative_curation" 記帳，不能混進 paid_review。"""
    monkeypatch.setenv("ASSOCIATIVE_CURATION", "on")
    host = DummyAutopilotHost()
    seen = {}

    async def fake_paid(content, *, system, **kw):
        seen.update(kw)
        return None

    with patch("llm_pool.call_paid_review", fake_paid):
        await host._try_associative_pick(members=["showay"], exclude_titles=[],
                                         spotlight="showay", mm=None)
    assert seen.get("caller") == "associative_curation"


@pytest.mark.asyncio
async def test_associative_slow_llm_times_out_fast(monkeypatch):
    """LLM 卡住不能讓空佇列一直等：整段選曲有硬上限，逾時回 0 走一般 autopilot。"""
    import asyncio
    import cogs.music_cog_autopilot as ap
    monkeypatch.setenv("ASSOCIATIVE_CURATION", "on")
    monkeypatch.setattr(ap, "_ASSOCIATIVE_LLM_TIMEOUT_S", 0.05)
    host = DummyAutopilotHost()

    async def hang(*a, **kw):
        await asyncio.sleep(30)

    t0 = time.monotonic()
    with patch("associative_curation.curate_associative_song", hang):
        n = await host._try_associative_pick(members=["showay"], exclude_titles=[],
                                             spotlight="showay", mm=None)
    assert n == 0
    assert time.monotonic() - t0 < 2.0
    assert host.stream_queue == []


@pytest.mark.asyncio
async def test_try_associative_pick_rejects_30day_repeat(monkeypatch):
    """LLM 選了 30 天內選過的歌要被擋，不入隊。"""
    monkeypatch.setenv("ASSOCIATIVE_CURATION", "on")
    host = DummyAutopilotHost()
    associative_history.append_pick("美秀集團", "手機錢包鑰匙菸", time.time() - 3600)

    mock_pick = AssociativePick(
        observed_topic="出門口訣被偷",
        target_lyric="手機錢包鑰匙菸，反覆唸幾遍",
        artist="美秀集團",
        song="手機錢包鑰匙菸",
        reason="聊到出門口訣被寫成歌",
        dj_line="剛才聽showay抱怨出門默念的口訣被樂團偷去寫歌。美秀集團這首手機錢包鑰匙菸奉上，出門前口袋拍兩下吧。",
    )

    with patch("associative_curation.curate_associative_song", AsyncMock(return_value=[mock_pick])):
        n = await host._try_associative_pick(members=["showay"], exclude_titles=[],
                                             spotlight="showay", mm=None)

    assert n == 0
    assert host.stream_queue == []


@pytest.mark.asyncio
async def test_try_associative_pick_success_appends_history(monkeypatch):
    """成功入隊後，30 天選曲紀錄要多一行該 artist/song。"""
    monkeypatch.setenv("ASSOCIATIVE_CURATION", "on")
    host = DummyAutopilotHost()

    mock_pick = AssociativePick(
        observed_topic="出門口訣被偷",
        target_lyric="手機錢包鑰匙菸，反覆唸幾遍",
        artist="美秀集團",
        song="手機錢包鑰匙菸",
        reason="聊到出門口訣被寫成歌",
        dj_line="剛才聽showay抱怨出門默念的口訣被樂團偷去寫歌。美秀集團這首手機錢包鑰匙菸奉上，出門前口袋拍兩下吧。",
    )

    mm = MagicMock()
    mm.get_skipped_video_ids.return_value = set()
    mm.get_recently_played_video_ids.return_value = set()

    with patch("associative_curation.curate_associative_song", AsyncMock(return_value=[mock_pick])):
        n = await host._try_associative_pick(members=["showay"], exclude_titles=[],
                                             spotlight="showay", mm=mm)

    assert n == 1
    assert associative_history.HISTORY_PATH.exists()
    lines = associative_history.HISTORY_PATH.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    import json as _json
    data = _json.loads(lines[0])
    assert data["artist"] == "美秀集團"
    assert data["song"] == "手機錢包鑰匙菸"


@pytest.mark.asyncio
async def test_try_associative_pick_passes_recent_picks_kwarg(monkeypatch):
    """curate_associative_song 被呼叫時 recent_picks kwarg 要等於 load_recent_picks 的結果。"""
    monkeypatch.setenv("ASSOCIATIVE_CURATION", "on")
    host = DummyAutopilotHost()
    associative_history.append_pick("阿杜", "他一定很愛你", time.time() - 3600)

    expected_recent = associative_history.load_recent_picks(time.time())

    mock_fn = AsyncMock(return_value=None)
    with patch("associative_curation.curate_associative_song", mock_fn):
        await host._try_associative_pick(members=["showay"], exclude_titles=[],
                                         spotlight="showay", mm=None)

    assert mock_fn.await_args.kwargs["recent_picks"] == expected_recent


# ── 解析後以實際結果重查 30 天紀錄（#104 review：LLM 拼錯/搜尋歧義時只比 pick.song 會漏）──

_VID = "MEISHOW0001"  # extract_video_id 只認 11 碼


def _host_resolving_vid():
    host = DummyAutopilotHost()
    host._resolve_yt_query = AsyncMock(return_value={
        "title": "美秀集團 Amazing Show－手機錢包鑰匙菸",
        "url": f"https://www.youtube.com/watch?v={_VID}",
        "webpage_url": f"https://www.youtube.com/watch?v={_VID}",
        "duration": 210,
    })
    return host


def _pick(song):
    return AssociativePick(
        observed_topic="出門口訣被偷", target_lyric="手機錢包鑰匙菸", artist="美秀集團",
        song=song, reason="聊到出門口訣", dj_line="剛才聽showay抱怨出門默念的口訣被樂團偷去寫歌，這首奉上。",
    )


@pytest.mark.asyncio
async def test_rejects_when_resolved_video_id_was_picked_within_30_days(monkeypatch):
    """LLM 這次寫成別的歌名，但 YouTube 解析到同一支影片 → 擋。"""
    monkeypatch.setenv("ASSOCIATIVE_CURATION", "on")
    host = _host_resolving_vid()
    associative_history.append_pick("美秀集團", "某個完全不同的寫法", time.time() - 3600,
                                    title="不相干的標題", video_id=_VID)
    with patch("associative_curation.curate_associative_song", AsyncMock(return_value=[_pick("出門口訣之歌")])):
        n = await host._try_associative_pick(members=["showay"], exclude_titles=[], spotlight="showay", mm=None)
    assert n == 0
    assert host.stream_queue == []


@pytest.mark.asyncio
async def test_rejects_when_resolved_title_is_same_song_as_recent_pick(monkeypatch):
    """舊紀錄只有 LLM 歌名（沒有 vid），解析出的 YouTube 標題是同一首 → 擋。"""
    monkeypatch.setenv("ASSOCIATIVE_CURATION", "on")
    host = _host_resolving_vid()
    associative_history.append_pick("美秀集團", "手機錢包鑰匙菸", time.time() - 3600)
    with patch("associative_curation.curate_associative_song", AsyncMock(return_value=[_pick("出門口訣之歌")])):
        n = await host._try_associative_pick(members=["showay"], exclude_titles=[], spotlight="showay", mm=None)
    assert n == 0


@pytest.mark.asyncio
async def test_success_records_resolved_title_and_video_id(monkeypatch):
    monkeypatch.setenv("ASSOCIATIVE_CURATION", "on")
    host = _host_resolving_vid()
    with patch("associative_curation.curate_associative_song", AsyncMock(return_value=[_pick("手機錢包鑰匙菸")])):
        n = await host._try_associative_pick(members=["showay"], exclude_titles=[], spotlight="showay", mm=None)
    assert n == 1
    import json as _json
    data = _json.loads(associative_history.HISTORY_PATH.read_text(encoding="utf-8").splitlines()[-1])
    assert data["title"] == "美秀集團 Amazing Show－手機錢包鑰匙菸"
    assert data["vid"] == _VID


def test_is_resolved_repeat_ignores_records_older_than_30_days():
    associative_history.append_pick("美秀集團", "手機錢包鑰匙菸", time.time() - 31 * 86400, video_id=_VID)
    assert associative_history.is_resolved_repeat(
        "美秀集團 Amazing Show－手機錢包鑰匙菸", _VID, time.time()) is False


def test_resolved_title_matches_pick_contains_with_noise():
    from associative_curation import resolved_title_matches_pick
    assert resolved_title_matches_pick("美秀集團 Amazing Show－手機錢包鑰匙菸", "手機錢包鑰匙菸")


def test_resolved_title_matches_pick_simplified_vs_traditional():
    from associative_curation import resolved_title_matches_pick
    assert resolved_title_matches_pick("陈麒名_拿走了什么 Cover:A Lin 高音质 动态歌词版", "拿走了什麼")


def test_resolved_title_matches_pick_rejects_different_song():
    from associative_curation import resolved_title_matches_pick
    assert not resolved_title_matches_pick("蘇打綠 - 愛你無條件", "剪掉")
    assert not resolved_title_matches_pick("任何標題", "")


@pytest.mark.asyncio
async def test_rejects_when_resolved_title_differs_from_pick(monkeypatch):
    """LLM 選《剪掉》但 YouTube 解析成別首 → 不入隊，走一般 autopilot。"""
    monkeypatch.setenv("ASSOCIATIVE_CURATION", "on")
    host = DummyAutopilotHost()
    with patch("associative_curation.curate_associative_song", AsyncMock(return_value=[_pick("剪掉")])):
        added = await host._try_associative_pick(members=["showay"], exclude_titles=[], spotlight="showay", mm=None)
    assert added == 0
    assert host.stream_queue == []


# ── 10/19：本地歌單優先＋一次三首候選＋內化氣氛 ──────────────────────────


@pytest.mark.asyncio
async def test_three_candidates_first_is_repeat_second_succeeds(monkeypatch):
    """第一首候選 30 天內選過，換第二首入隊。"""
    monkeypatch.setenv("ASSOCIATIVE_CURATION", "on")
    host = DummyAutopilotHost()
    associative_history.append_pick("美秀集團", "手機錢包鑰匙菸", time.time() - 3600)

    picks = [
        _pick("手機錢包鑰匙菸"),  # 30 天內選過
        AssociativePick(
            observed_topic="出門口訣被偷", target_lyric="無", artist="告五人",
            song="披星戴月的想你", reason="無", dj_line="第二首候選奉上。",
        ),
        AssociativePick(
            observed_topic="出門口訣被偷", target_lyric="無", artist="茄子蛋",
            song="浪子回頭", reason="無", dj_line="第三首候選。",
        ),
    ]

    resolved_queries = []

    async def _resolve_by_query(query: str):  # 解析結果跟著 query 走，第二首才會解析成第二首
        resolved_queries.append(query)
        return {"title": query, "url": f"https://www.youtube.com/watch?v={abs(hash(query))}",
                "webpage_url": f"https://www.youtube.com/watch?v={abs(hash(query))}", "duration": 210}
    host._resolve_yt_query = _resolve_by_query

    with patch("associative_curation.curate_associative_song", AsyncMock(return_value=picks)):
        n = await host._try_associative_pick(members=["showay"], exclude_titles=[], spotlight="showay", mm=None)

    assert n == 1
    assert len(host.stream_queue) == 1
    assert "披星戴月的想你" in host.stream_queue[0]["title"]
    assert not any("手機錢包鑰匙菸" in q for q in resolved_queries), "30 天內選過的候選不該白跑 YouTube 解析"


@pytest.mark.asyncio
async def test_all_three_candidates_rejected_no_enqueue(monkeypatch):
    """三首候選全被 30 天內選過擋下 → 不入隊。"""
    monkeypatch.setenv("ASSOCIATIVE_CURATION", "on")
    host = DummyAutopilotHost()
    for song in ("歌A", "歌B", "歌C"):
        associative_history.append_pick("某歌手", song, time.time() - 3600)

    picks = [AssociativePick(observed_topic="t", target_lyric="無", artist="某歌手",
                             song=s, reason="無", dj_line="d") for s in ("歌A", "歌B", "歌C")]

    with patch("associative_curation.curate_associative_song", AsyncMock(return_value=picks)):
        n = await host._try_associative_pick(members=["showay"], exclude_titles=[], spotlight="showay", mm=None)

    assert n == 0
    assert host.stream_queue == []


def _local_song_entry(vid="LOCALSONG01"):
    return {
        "title": "本地老歌",
        "track": "本地老歌",
        "artist": "本地歌手",
        "requesters": {"showay": 3},
        "total_plays": 3,
        "webpage_url": f"https://www.youtube.com/watch?v={vid}",
    }


@pytest.mark.asyncio
async def test_local_hit_skips_title_match_check(monkeypatch):
    """local_index 命中本地歌單：即使 LLM song 名跟解析標題不同，也不做標題比對，照樣入隊，
    且 _resolve_yt_query 收到的 query 是該首的 webpage_url。"""
    monkeypatch.setenv("ASSOCIATIVE_CURATION", "on")
    host = DummyAutopilotHost()
    mm = MagicMock()
    mm.get_skipped_video_ids.return_value = set()
    mm.get_recently_played_video_ids.return_value = set()
    mm.all_songs.return_value = {"https://www.youtube.com/watch?v=LOCALSONG01": _local_song_entry()}

    seen_queries = []

    async def _resolve(query):
        seen_queries.append(query)
        return {
            "title": "完全不同的解析標題",
            "url": "https://www.youtube.com/watch?v=LOCALSONG01",
            "webpage_url": "https://www.youtube.com/watch?v=LOCALSONG01",
            "duration": 200,
        }
    host._resolve_yt_query = _resolve

    local_pick = AssociativePick(
        observed_topic="t", target_lyric="無", artist="本地歌手",
        song="跟解析標題完全不同的歌名", reason="無", dj_line="本地歌單命中。",
        local_index=0,
    )
    with patch("associative_curation.curate_associative_song", AsyncMock(return_value=[local_pick])):
        n = await host._try_associative_pick(members=["showay"], exclude_titles=[], spotlight="showay", mm=mm)

    assert n == 1
    assert seen_queries == ["https://www.youtube.com/watch?v=LOCALSONG01"]


@pytest.mark.asyncio
async def test_non_local_pick_with_mismatched_title_rejected(monkeypatch):
    """對照組：同樣的 song/解析標題不符，但 local_index=None（提名）→ 被標題比對擋下。"""
    monkeypatch.setenv("ASSOCIATIVE_CURATION", "on")
    host = DummyAutopilotHost()
    mm = MagicMock()
    mm.get_skipped_video_ids.return_value = set()
    mm.get_recently_played_video_ids.return_value = set()
    mm.all_songs.return_value = {"https://www.youtube.com/watch?v=LOCALSONG01": _local_song_entry()}

    async def _resolve(query):
        return {
            "title": "完全不同的解析標題",
            "url": "https://www.youtube.com/watch?v=LOCALSONG01",
            "webpage_url": "https://www.youtube.com/watch?v=LOCALSONG01",
            "duration": 200,
        }
    host._resolve_yt_query = _resolve

    nominated_pick = AssociativePick(
        observed_topic="t", target_lyric="無", artist="本地歌手",
        song="跟解析標題完全不同的歌名", reason="無", dj_line="提名。",
        local_index=None,
    )
    with patch("associative_curation.curate_associative_song", AsyncMock(return_value=[nominated_pick])):
        n = await host._try_associative_pick(members=["showay"], exclude_titles=[], spotlight="showay", mm=mm)

    assert n == 0
    assert host.stream_queue == []


def _diary_entry_now(mood: str, speakers: list[str]):
    from chat_summary_parser import DiaryEntry
    return DiaryEntry(
        ts_str=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        core="",
        speakers=speakers,
        mood=mood,
    )


@pytest.mark.asyncio
async def test_dj_line_leaking_mood_is_cleared(monkeypatch):
    """dj_line 講破氣氛 → 仍入隊，但 info["_dj_line"] 被清空。"""
    monkeypatch.setenv("ASSOCIATIVE_CURATION", "on")
    host = DummyAutopilotHost()
    host._load_summary_entries = MagicMock(return_value=[
        _diary_entry_now("戲謔爭執｜showay不爽因被重複問", ["showay"])
    ])

    leaking_pick = AssociativePick(
        observed_topic="t", target_lyric="無", artist="美秀集團",
        song="手機錢包鑰匙菸", reason="無",
        dj_line="剛才一片戲謔爭執，這首給大家壓壓驚。",
    )
    with patch("associative_curation.curate_associative_song", AsyncMock(return_value=[leaking_pick])):
        n = await host._try_associative_pick(members=["showay"], exclude_titles=[], spotlight="showay", mm=None)

    assert n == 1
    assert host.stream_queue[0]["_dj_line"] == ""


@pytest.mark.asyncio
async def test_dj_line_unrelated_to_mood_is_kept(monkeypatch):
    """對照組：dj_line 跟氣氛無關 → 原樣保留。"""
    monkeypatch.setenv("ASSOCIATIVE_CURATION", "on")
    host = DummyAutopilotHost()
    host._load_summary_entries = MagicMock(return_value=[
        _diary_entry_now("戲謔爭執｜showay不爽因被重複問", ["showay"])
    ])

    unrelated_pick = AssociativePick(
        observed_topic="t", target_lyric="無", artist="美秀集團",
        song="手機錢包鑰匙菸", reason="無",
        dj_line="出門前記得檢查手機錢包鑰匙菸，這首奉上。",
    )
    with patch("associative_curation.curate_associative_song", AsyncMock(return_value=[unrelated_pick])):
        n = await host._try_associative_pick(members=["showay"], exclude_titles=[], spotlight="showay", mm=None)

    assert n == 1
    assert host.stream_queue[0]["_dj_line"] == unrelated_pick.dj_line


@pytest.mark.asyncio
async def test_curate_called_with_local_titles_and_mood_hint_kwargs(monkeypatch):
    """curate_associative_song 呼叫時要收到 local_titles 與 mood_hint kwarg。"""
    monkeypatch.setenv("ASSOCIATIVE_CURATION", "on")
    host = DummyAutopilotHost()
    mm = MagicMock()
    mm.get_skipped_video_ids.return_value = set()
    mm.get_recently_played_video_ids.return_value = set()
    mm.all_songs.return_value = {"https://www.youtube.com/watch?v=LOCALSONG01": _local_song_entry()}
    host._load_summary_entries = MagicMock(return_value=[
        _diary_entry_now("戲謔爭執", ["showay"])
    ])

    mock_fn = AsyncMock(return_value=[])
    with patch("associative_curation.curate_associative_song", mock_fn):
        await host._try_associative_pick(members=["showay"], exclude_titles=[], spotlight="showay", mm=mm)

    assert mock_fn.await_args.kwargs["local_titles"] == ["本地歌手 - 本地老歌"]
    assert mock_fn.await_args.kwargs["mood_hint"] == "戲謔爭執"


# ── 🧭 主題連結（theme-link-mvp.md B 段）驗收 8/9 ───────────────────────────

import sqlite3

import cogs.music_cog_autopilot as _autopilot_mod


def _init_theme_db(path):
    con = sqlite3.connect(str(path))
    con.execute("""
        CREATE TABLE theme_tags (
            tag TEXT PRIMARY KEY, meaning TEXT, sensitive INTEGER, room_veto INTEGER,
            heard_by TEXT, first_seen REAL, last_seen REAL, day_count INTEGER, updated_at REAL,
            songable INTEGER
        )
    """)
    con.execute("""
        CREATE TABLE theme_picks (tag TEXT, video_id TEXT, rank INTEGER, layer TEXT,
                                   PRIMARY KEY (tag, video_id))
    """)
    con.execute("""
        CREATE TABLE song_cards (video_id TEXT PRIMARY KEY, artist TEXT, title TEXT,
                                  imagery TEXT, deep_story TEXT, relation TEXT, weight TEXT,
                                  familiarity TEXT, created_at REAL)
    """)
    con.commit()
    return con


@pytest.fixture
def theme_db(tmp_path, monkeypatch):
    db_path = tmp_path / "theme_marvin.db"
    con = _init_theme_db(db_path)
    monkeypatch.setattr(_autopilot_mod, "_THEME_LINK_DB_PATH", str(db_path))
    yield con
    con.close()


def _insert_theme_tag(con, tag, *, meaning="戶外過夜；延伸：遠離城市", sensitive=0, room_veto=None,
                      heard_by=None, last_seen=None, songable=1):
    con.execute(
        "INSERT OR REPLACE INTO theme_tags (tag, meaning, sensitive, room_veto, heard_by, "
        "first_seen, last_seen, day_count, updated_at, songable) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (tag, meaning, sensitive, room_veto, __import__("json").dumps(heard_by or []),
         last_seen or time.time(), last_seen or time.time(), 2, time.time(), songable),
    )
    con.commit()


def test_theme_link_off_skips_db_read(monkeypatch, theme_db):
    monkeypatch.setenv("THEME_LINK", "off")
    _insert_theme_tag(theme_db, "露營", heard_by=["showay"])
    host = DummyAutopilotHost()

    qualifying = host._load_qualifying_theme_tags(["showay"], time.time())
    assert qualifying == []


@pytest.mark.asyncio
async def test_theme_link_off_prompt_has_no_theme_kwarg(monkeypatch, theme_db):
    monkeypatch.setenv("THEME_LINK", "off")
    monkeypatch.setenv("ASSOCIATIVE_CURATION", "on")
    _insert_theme_tag(theme_db, "露營", heard_by=["showay"])
    host = DummyAutopilotHost()

    mock_fn = AsyncMock(return_value=[])
    with patch("associative_curation.curate_associative_song", mock_fn):
        await host._try_associative_pick(members=["showay"], exclude_titles=[], spotlight="showay", mm=None)

    assert mock_fn.await_args.kwargs["themes"] is None


@pytest.mark.asyncio
async def test_today_already_used_tag_gives_no_qualifying_tags(theme_db):
    _insert_theme_tag(theme_db, "露營", heard_by=["showay"])
    associative_history.append_pick("A", "歌A", time.time(), source="tag", tag="搬家")
    host = DummyAutopilotHost()

    qualifying = host._load_qualifying_theme_tags(["showay"], time.time())
    assert qualifying == []


@pytest.mark.asyncio
async def test_tag_used_within_14_days_excluded(theme_db):
    _insert_theme_tag(theme_db, "露營", heard_by=["showay"])
    associative_history.append_pick("A", "歌A", time.time() - 86400, source="tag", tag="露營")
    host = DummyAutopilotHost()

    qualifying = host._load_qualifying_theme_tags(["showay"], time.time())
    assert qualifying == []


@pytest.mark.asyncio
async def test_member_not_in_heard_by_excludes_tag(theme_db):
    _insert_theme_tag(theme_db, "露營", heard_by=["showay"])  # 大肚不在 heard_by
    host = DummyAutopilotHost()

    qualifying = host._load_qualifying_theme_tags(["showay", "大肚"], time.time())
    assert qualifying == []


@pytest.mark.asyncio
async def test_qualifying_tag_found_when_all_members_in_heard_by(theme_db):
    _insert_theme_tag(theme_db, "露營", heard_by=["showay", "大肚"])
    host = DummyAutopilotHost()

    qualifying = host._load_qualifying_theme_tags(["showay", "大肚"], time.time())
    assert qualifying == [("露營", "戶外過夜；延伸：遠離城市")]


@pytest.mark.asyncio
async def test_zero_utterances_with_qualifying_tag_still_calls_llm(theme_db):
    _insert_theme_tag(theme_db, "露營", heard_by=["showay"])
    host = DummyAutopilotHost()
    host.bot.engine.conv_buffer.get_last_n_utterances.return_value = []  # 0 句對話

    mock_fn = AsyncMock(return_value=[])
    with patch("associative_curation.curate_associative_song", mock_fn):
        n = await host._try_associative_pick(members=["showay"], exclude_titles=[],
                                             spotlight="showay", mm=None)

    assert mock_fn.await_count == 1
    assert mock_fn.await_args.kwargs["min_utterances"] == 0
    assert n == 0


@pytest.mark.asyncio
async def test_zero_utterances_without_qualifying_tag_returns_zero_without_calling_llm(theme_db):
    host = DummyAutopilotHost()
    host.bot.engine.conv_buffer.get_last_n_utterances.return_value = []  # 0 句對話，沒有合格 tag

    mock_fn = AsyncMock(return_value=[])
    with patch("associative_curation.curate_associative_song", mock_fn):
        n = await host._try_associative_pick(members=["showay"], exclude_titles=[],
                                             spotlight="showay", mm=None)

    assert n == 0
    assert mock_fn.await_count == 0


@pytest.mark.asyncio
async def test_theme_pick_enqueues_with_no_dj_line_and_group_attribution(theme_db):
    _insert_theme_tag(theme_db, "露營", heard_by=["showay", "狗與露"])
    theme_db.execute(
        "INSERT INTO song_cards (video_id, artist, title, imagery, deep_story, relation, weight, "
        "familiarity, created_at) VALUES ('AAAAAAAAAAA','美秀集團','手機錢包鑰匙菸','海邊露營','深夜聊天','朋友','中','高',0)"
    )
    theme_db.execute("INSERT INTO theme_picks (tag, video_id, rank, layer) VALUES ('露營','AAAAAAAAAAA',0,'深層')")
    theme_db.commit()

    host = DummyAutopilotHost()
    mm = MagicMock()
    mm.get_skipped_video_ids.return_value = set()
    mm.get_recently_played_video_ids.return_value = set()
    mm.all_songs.return_value = {
        "k1": {"title": "手機錢包鑰匙菸", "artist": "美秀集團", "track": "手機錢包鑰匙菸",
               "requesters": {"showay": 1}, "total_plays": 5,
               "webpage_url": "https://www.youtube.com/watch?v=AAAAAAAAAAA"},
    }

    mock_pick = AssociativePick(
        observed_topic="露營", target_lyric="手機錢包鑰匙菸", artist="美秀集團",
        song="手機錢包鑰匙菸", reason="呼應露營話題", dj_line="這句不該被講出來",
        local_index=0, source="tag", tag="露營",
    )

    with patch("associative_curation.curate_associative_song", AsyncMock(return_value=[mock_pick])):
        n = await host._try_associative_pick(members=["showay", "狗與露"], exclude_titles=[],
                                             spotlight="showay", mm=mm)

    assert n == 1
    info = host.stream_queue[0]
    assert info["_dj_line"] == ""
    assert info["requested_by"] == "Marvin推薦（點給大家）"
    assert info["_lane"] == "theme"
    assert info["_theme_tag"] == "露營"

    lines = associative_history.HISTORY_PATH.read_text(encoding="utf-8").splitlines()
    import json as _json
    rec = _json.loads(lines[-1])
    assert rec["source"] == "tag"
    assert rec["tag"] == "露營"


@pytest.mark.asyncio
async def test_now_source_pick_keeps_old_behavior(theme_db):
    """LLM 回 source=now（即使有合格 tag 在場）→ requested_by/dj_line 跟改動前一樣。"""
    _insert_theme_tag(theme_db, "露營", heard_by=["showay"])
    host = DummyAutopilotHost()
    mm = MagicMock()
    mm.get_skipped_video_ids.return_value = set()
    mm.get_recently_played_video_ids.return_value = set()

    mock_pick = AssociativePick(
        observed_topic="出門口訣被偷", target_lyric="手機錢包鑰匙菸", artist="美秀集團",
        song="手機錢包鑰匙菸", reason="聊到出門口訣",
        dj_line="剛才聽showay抱怨出門默念的口訣被樂團偷去寫歌，這首奉上。",
        source="now",
    )

    with patch("associative_curation.curate_associative_song", AsyncMock(return_value=[mock_pick])):
        n = await host._try_associative_pick(members=["showay"], exclude_titles=[],
                                             spotlight="showay", mm=mm)

    assert n == 1
    info = host.stream_queue[0]
    assert info["requested_by"] == "Marvin推薦（對話靈感）"
    assert info["_dj_line"] == mock_pick.dj_line
    assert info["_lane"] == "associative"


@pytest.mark.asyncio
async def test_theme_entries_prepended_before_local_cands_and_capped(theme_db):
    from associative_curation import LOCAL_POOL_LIMIT

    _insert_theme_tag(theme_db, "露營", heard_by=["showay"])
    theme_db.execute(
        "INSERT INTO song_cards (video_id, artist, title, imagery, deep_story, relation, weight, "
        "familiarity, created_at) VALUES ('AAAAAAAAAAA','美秀集團','手機錢包鑰匙菸','海邊露營','深夜聊天','朋友','中','高',0)"
    )
    theme_db.execute("INSERT INTO theme_picks (tag, video_id, rank, layer) VALUES ('露營','AAAAAAAAAAA',0,'深層')")
    theme_db.commit()

    host = DummyAutopilotHost()
    songs = {
        "k1": {"title": "手機錢包鑰匙菸", "artist": "美秀集團", "track": "手機錢包鑰匙菸",
               "requesters": {"showay": 1}, "total_plays": 999,
               "webpage_url": "https://www.youtube.com/watch?v=AAAAAAAAAAA"},
    }
    for i in range(50):
        vid = f"{i:011d}".replace("0", "B")[:11]
        songs[f"local{i}"] = {
            "title": f"本地歌{i}", "artist": "本地歌手", "track": f"本地歌{i}",
            "requesters": {"showay": 1}, "total_plays": 1,
            "webpage_url": f"https://www.youtube.com/watch?v={vid}",
        }
    mm = MagicMock()
    mm.get_skipped_video_ids.return_value = set()
    mm.get_recently_played_video_ids.return_value = set()
    mm.all_songs.return_value = songs

    mock_fn = AsyncMock(return_value=[])
    with patch("associative_curation.curate_associative_song", mock_fn):
        await host._try_associative_pick(members=["showay"], exclude_titles=[], spotlight="showay", mm=mm)

    local_titles = mock_fn.await_args.kwargs["local_titles"]
    assert len(local_titles) <= LOCAL_POOL_LIMIT
    assert "意象：海邊露營" in local_titles[0]
    assert local_titles[0].startswith("美秀集團 - 手機錢包鑰匙菸")


@pytest.mark.asyncio
async def test_zero_utterances_drops_now_source_picks(theme_db):
    """對話 0 句、只因合格 tag 才呼叫 → LLM 回 source=now 的歌不准入隊（沒有對話可呼應）。"""
    _insert_theme_tag(theme_db, "露營", heard_by=["showay"])
    host = DummyAutopilotHost()
    host.bot.engine.conv_buffer.get_last_n_utterances.return_value = []
    mm = MagicMock()
    mm.get_skipped_video_ids.return_value = set()
    mm.get_recently_played_video_ids.return_value = set()

    now_pick = AssociativePick(
        observed_topic="（無對話）", target_lyric="", artist="美秀集團", song="手機錢包鑰匙菸",
        reason="", dj_line="剛才大家聊得很熱烈", source="now",
    )
    with patch("associative_curation.curate_associative_song", AsyncMock(return_value=[now_pick])):
        n = await host._try_associative_pick(members=["showay"], exclude_titles=[],
                                             spotlight="showay", mm=mm)

    assert n == 0
    assert host.stream_queue == []


@pytest.mark.asyncio
async def test_not_songable_tag_is_not_qualifying(theme_db):
    """songable=0 的 tag（遊戲／技術話題）即使其他條件都合格也不給。"""
    _insert_theme_tag(theme_db, "bug", heard_by=["showay"], songable=0)
    host = DummyAutopilotHost()
    assert host._load_qualifying_theme_tags(["showay"], time.time()) == []
