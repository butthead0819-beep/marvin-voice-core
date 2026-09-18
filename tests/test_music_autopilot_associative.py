"""Unit tests for associative curation integration in MusicAutopilotMixin."""
import time
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from associative_curation import AssociativePick


from cogs.music_cog_dj_lyrics import MusicDJLyricsMixin


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

    with patch("associative_curation.curate_associative_song", AsyncMock(return_value=mock_pick)):
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

    with patch("associative_curation.curate_associative_song", AsyncMock(return_value=mock_pick)):
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
    host._recent_emotional_highlight = MagicMock(return_value=None)
    host._fetch_news_items_async = AsyncMock(return_value=[])
    from dj_topic_selector import TopicCooldownStore
    import tempfile
    store = TopicCooldownStore(tempfile.mktemp(suffix=".json"))
    host._dj_topic_store = MagicMock(return_value=store)
    host._current_season = MagicMock(return_value="秋天")
    host._city_label = MagicMock(return_value="台中")
    host._dj_clean_name = MagicMock(return_value=("手機錢包鑰匙菸", "美秀集團"))
    host._themed_dj_text = MagicMock(return_value="")

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
    with patch("associative_curation.curate_associative_song", AsyncMock(return_value=None)):
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
