"""故事弧線播放整合 wiring（mock cog，不需真 TTS/Discord）。

Prepare（生成+TTS預渲染，存staged）跟 Play（純播放staged，零LLM/TTS延遲）兩階段拆分。

2026-08-17 二次真機測試後的架構修正：只有片頭（開場一次性 BGM+引導口白）是故事弧
自己播；歌曲本身直接丟進既有 `stream_queue`，交給 `_stream_loop`/`_run_tail_dj`/
`play_stream_song` 接手——不再自己重造一套 per-song BGM 循環（那套踩了三個 bug：
still_active 誤判、BGM音量蓋過口白、webpage_url不是可播網址，本質上都是繞開既有
正確實作造成的）。DJ 尾段口白沿用「相同的 interjection 方法」，`_fetch_dj_interjection_raw`
認得 `_lane == 'story_arc'` 直接用預渲染好的口白，不重新過 LLM/TTS。

重點驗證：
(a) _prepare_and_stage_story_arc 生成+預渲染 TTS + 存檔，回傳的 staged 帶音檔路徑
(b) _play_story_arc 只播片頭，歌曲原樣丟進 stream_queue（保留 url/webpage_url 等
    resolve_story_arc 給的欄位）並喚醒 _stream_loop，不自己播歌
(c) _fetch_dj_interjection_raw 對 _lane=='story_arc' 的節點直接回預渲染口白，不過 LLM/TTS
(d) staged show 播完（交棒）後不清檔，可重複 /story_arc_play
(e) _run_story_arc_pipeline 各階段材料不足/失敗時的早退原因字串
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_cog():
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.tts_engine = MagicMock()
    bot.tts_engine.generate_audio = AsyncMock(return_value="/tmp/story_tts.mp3")
    bot.router = MagicMock()
    bot.music_memory = MagicMock()
    bot.music_memory._data = {"songs": {}}

    from cogs.music_cog import MusicCog
    cog = MusicCog(bot)
    return cog


def _fake_vc():
    vc = MagicMock()
    vc._tts_protected = False
    vc.play_local_file = AsyncMock(return_value=None)
    vc.play_dj_on_tts_layer = AsyncMock(return_value=True)
    return vc


def _staged(*, with_audio=True):
    return {
        "arc_title": "今夜的故事", "narrative_day": "2026-08-17", "target_duration_s": 600.0,
        "intro": {"script": "歡迎收聽", "music_path": "assets/dj_sfx/show_intro.mp3",
                 "audio_path": "/tmp/intro.mp3" if with_audio else None,
                 "audio_duration_s": 8.0 if with_audio else 0.0},
        "infos": [
            {"_story_node_position": 1, "title": "晴天", "duration": 269,
             "url": "https://real-cdn/aaa", "webpage_url": "https://youtu.be/aaa",
             "_story_arc_title": "今夜的故事", "_story_interjection_script": "口白一",
             "_story_interjection_audio_path": "/tmp/a.mp3" if with_audio else None,
             "_story_interjection_duration_s": 12.0 if with_audio else 0.0},
            {"_story_node_position": 2, "title": "流沙", "duration": 300,
             "url": "https://real-cdn/bbb", "webpage_url": "https://youtu.be/bbb",
             "_story_arc_title": "今夜的故事", "_story_interjection_script": "口白二",
             "_story_interjection_audio_path": "/tmp/b.mp3" if with_audio else None,
             "_story_interjection_duration_s": 10.0 if with_audio else 0.0},
        ],
    }


def _brief():
    from dj_story_arc import StoryBrief
    return StoryBrief(narrative_day="2026-08-17", shared_cores=[], member_cores={},
                      liked_items=[], conv_snippets=[], members=["狗與露"],
                      target_duration_s=600.0, node_count=2)


# ── _play_story_arc（片頭自己播，歌曲丟進 stream_queue）──────────────────────

@pytest.mark.asyncio
async def test_play_story_arc_plays_intro_then_enqueues_songs_unchanged():
    cog = _make_cog()
    vc = _fake_vc()
    cog._vc = MagicMock(return_value=vc)
    cog._ensure_stream_loop = MagicMock()
    cog._republish_queue_snapshot = MagicMock()

    with patch("asyncio.sleep", new=AsyncMock()), \
         patch("dj_story_arc.record_story_arc") as mock_record:
        await cog._play_story_arc(_staged())

    # 片頭：BGM播放 + 口白疊播
    vc.play_local_file.assert_any_call("assets/dj_sfx/show_intro.mp3",
                                       volume=cog._STORY_ARC_BGM_VOLUME)
    vc.play_dj_on_tts_layer.assert_any_call("/tmp/intro.mp3")

    # 歌曲：原樣進 stream_queue，不是另外播放；url 保留（不是只有 webpage_url）
    assert [i["title"] for i in cog.stream_queue] == ["晴天", "流沙"]
    assert cog.stream_queue[0]["url"] == "https://real-cdn/aaa"
    assert all(i["_lane"] == "story_arc" for i in cog.stream_queue)
    assert all(i["requested_by"] for i in cog.stream_queue)
    cog._ensure_stream_loop.assert_called_once()
    cog._republish_queue_snapshot.assert_called_once()
    mock_record.assert_called_once()


@pytest.mark.asyncio
async def test_play_story_arc_does_not_mutate_staged_dict_in_place():
    """stream_queue 裡的 info 要是 copy，不能共用 staged dict 的可變狀態（同一份 staged
    要能重複 /story_arc_play，被上次播放動過手腳會壞掉下次）。"""
    cog = _make_cog()
    vc = _fake_vc()
    cog._vc = MagicMock(return_value=vc)
    cog._ensure_stream_loop = MagicMock()
    cog._republish_queue_snapshot = MagicMock()
    staged = _staged()
    original_first_info = dict(staged["infos"][0])

    with patch("asyncio.sleep", new=AsyncMock()), patch("dj_story_arc.record_story_arc"):
        await cog._play_story_arc(staged)

    assert staged["infos"][0] == original_first_info   # 原始 staged 沒被動過


@pytest.mark.asyncio
async def test_play_story_arc_does_not_clear_staged_show_so_it_can_be_replayed():
    """2026-08-17 使用者反饋：測播放設定不該每次都重新 Prepare 燒 LLM token——播完
    (交棒)不該自動刪掉 staged show，同一份內容要能重複 /story_arc_play。"""
    cog = _make_cog()
    vc = _fake_vc()
    cog._vc = MagicMock(return_value=vc)
    cog._ensure_stream_loop = MagicMock()
    cog._republish_queue_snapshot = MagicMock()

    with patch("asyncio.sleep", new=AsyncMock()), \
         patch("dj_story_arc.record_story_arc"), \
         patch("dj_story_arc.clear_staged_show") as mock_clear:
        await cog._play_story_arc(_staged())
        await cog._play_story_arc(_staged())   # 重播同一份，不該噴錯、不該被清掉擋住

    mock_clear.assert_not_called()
    assert len(cog.stream_queue) == 4   # 兩輪各 enqueue 2 首


@pytest.mark.asyncio
async def test_play_story_arc_skips_enqueue_when_no_infos():
    cog = _make_cog()
    vc = _fake_vc()
    cog._vc = MagicMock(return_value=vc)
    cog._ensure_stream_loop = MagicMock()
    cog._republish_queue_snapshot = MagicMock()
    staged = _staged()
    staged["infos"] = []

    with patch("asyncio.sleep", new=AsyncMock()), patch("dj_story_arc.record_story_arc"):
        await cog._play_story_arc(staged)

    cog._ensure_stream_loop.assert_not_called()
    cog._republish_queue_snapshot.assert_not_called()
    assert cog.stream_queue == []


@pytest.mark.asyncio
async def test_play_story_arc_no_vc_skips_without_crashing():
    cog = _make_cog()
    cog._vc = MagicMock(return_value=None)
    await cog._play_story_arc(_staged())
    assert cog.stream_queue == []


@pytest.mark.asyncio
async def test_play_story_arc_bgm_uses_low_volume_and_protects_intro_tts():
    """2026-08-17 真機測試踩到：BGM 整個蓋過口白——play_local_file 之前沒傳 volume，
    退回預設 1.0（滿音量）。片頭口白也要 protected，不被 barge-in/靜音閘打斷。"""
    cog = _make_cog()
    vc = _fake_vc()
    vc._tts_protected = "sentinel"
    cog._vc = MagicMock(return_value=vc)
    cog._ensure_stream_loop = MagicMock()
    cog._republish_queue_snapshot = MagicMock()

    seen_protected_during = []

    async def _record_protected(*a, **kw):
        seen_protected_during.append(vc._tts_protected)

    vc.play_dj_on_tts_layer = AsyncMock(side_effect=_record_protected)

    with patch("asyncio.sleep", new=AsyncMock()), patch("dj_story_arc.record_story_arc"):
        await cog._play_story_arc(_staged())

    bgm_call = vc.play_local_file.call_args_list[0]
    assert bgm_call.kwargs.get("volume") == cog._STORY_ARC_BGM_VOLUME
    assert bgm_call.kwargs["volume"] < 1.0
    assert seen_protected_during == [True]
    assert vc._tts_protected == "sentinel"   # 播完恢復進場前的值


@pytest.mark.asyncio
async def test_play_story_arc_skips_intro_tts_when_not_prerendered():
    """intro 沒有預渲染音檔（TTS失敗）→ 跳過口白疊播，BGM 仍照播，不中斷。"""
    cog = _make_cog()
    vc = _fake_vc()
    cog._vc = MagicMock(return_value=vc)
    cog._ensure_stream_loop = MagicMock()
    cog._republish_queue_snapshot = MagicMock()

    with patch("asyncio.sleep", new=AsyncMock()), patch("dj_story_arc.record_story_arc"):
        await cog._play_story_arc(_staged(with_audio=False))

    vc.play_dj_on_tts_layer.assert_not_called()
    vc.play_local_file.assert_called_once()   # BGM 還是播了


# ── _fetch_dj_interjection_raw 對 story_arc 節點的處理 ───────────────────────

@pytest.mark.asyncio
async def test_fetch_dj_interjection_raw_uses_prerendered_script_for_story_arc_lane():
    cog = _make_cog()
    info = {"_lane": "story_arc", "_story_interjection_script": "口白內容",
           "_story_interjection_audio_path": "/tmp/a.mp3", "title": "晴天"}
    result = await cog._fetch_dj_interjection_raw(info)
    assert result == {"text": "口白內容", "audio_path": "/tmp/a.mp3"}


@pytest.mark.asyncio
async def test_fetch_dj_interjection_raw_story_arc_lane_without_script_returns_none():
    cog = _make_cog()
    info = {"_lane": "story_arc", "_story_interjection_script": "", "title": "晴天"}
    assert await cog._fetch_dj_interjection_raw(info) is None


@pytest.mark.asyncio
async def test_fetch_dj_interjection_raw_story_arc_lane_never_calls_llm():
    """story_arc 節點走預渲染快路徑，不該碰 LLM/router（那是這函式其餘部分的事）。"""
    cog = _make_cog()
    cog.bot.router.generate_dynamic_system_msg = AsyncMock(
        side_effect=AssertionError("不該呼叫 LLM"))
    info = {"_lane": "story_arc", "_story_interjection_script": "口白",
           "_story_interjection_audio_path": None}
    result = await cog._fetch_dj_interjection_raw(info)
    assert result == {"text": "口白", "audio_path": None}


# ── _prepare_and_stage_story_arc（生成+TTS預渲染+存檔）───────────────────────

@pytest.mark.asyncio
async def test_prepare_and_stage_story_arc_renders_audio_and_saves():
    cog = _make_cog()

    from dj_story_arc import ShowIntro, StoryArc, StoryNode
    arc = StoryArc(arc_title="今夜的故事", nodes=[
        StoryNode(1, "懷舊", {}, "周杰倫", "晴天", "口白一", None, 0.0),
    ])
    infos = [{"title": "晴天", "duration": 269, "webpage_url": "https://youtu.be/x",
             "url": "https://real-cdn/x",
             "_story_arc_title": "今夜的故事", "_story_node_position": 1,
             "_story_interjection_script": "口白一", "_story_emotion_tag": "懷舊",
             "_story_spotlight_member": None, "_story_resonance_link": None,
             "_story_taste_match": True, "_story_bpm_target": 90.0,
             "_story_volume_delta_db": 0.0}]
    intro = ShowIntro(intro_script="歡迎收聽", intro_music_path="assets/dj_sfx/show_intro.mp3")

    cog._run_story_arc_pipeline = AsyncMock(return_value=((arc, infos, _brief(), intro), None))

    with patch("dj_story_arc.save_staged_show") as mock_save:
        staged, err = await cog._prepare_and_stage_story_arc(["狗與露"], 20.0)

    assert err is None
    assert staged["arc_title"] == "今夜的故事"
    assert staged["intro"]["audio_path"] == "/tmp/story_tts.mp3"
    assert staged["infos"][0]["_story_interjection_audio_path"] == "/tmp/story_tts.mp3"
    assert staged["infos"][0]["url"] == "https://real-cdn/x"   # 直連網址原樣保留
    mock_save.assert_called_once()


@pytest.mark.asyncio
async def test_prepare_and_stage_story_arc_propagates_pipeline_failure():
    cog = _make_cog()
    cog._run_story_arc_pipeline = AsyncMock(return_value=(None, "共同回憶素材不足"))
    staged, err = await cog._prepare_and_stage_story_arc(["狗與露"], 20.0)
    assert staged is None and err == "共同回憶素材不足"


# ── _run_story_arc_pipeline 早退原因 ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_pipeline_no_summary_entries():
    cog = _make_cog()
    cog._load_summary_entries = MagicMock(return_value=[])
    result, err = await cog._run_story_arc_pipeline(["狗與露"], 20.0)
    assert result is None and "對話記錄" in err


@pytest.mark.asyncio
async def test_pipeline_brief_none_reports_reason():
    cog = _make_cog()
    cog._load_summary_entries = MagicMock(return_value=[object()])
    with patch("dj_story_arc.gather_story_brief", return_value=None):
        result, err = await cog._run_story_arc_pipeline(["狗與露"], 20.0)
    assert result is None and "共同回憶" in err


@pytest.mark.asyncio
async def test_pipeline_no_music_memory_reports_reason():
    cog = _make_cog()
    cog.bot.music_memory = None
    cog._load_summary_entries = MagicMock(return_value=[object()])
    with patch("dj_story_arc.gather_story_brief", return_value=_brief()):
        result, err = await cog._run_story_arc_pipeline(["狗與露"], 20.0)
    assert result is None and "音樂記憶" in err
