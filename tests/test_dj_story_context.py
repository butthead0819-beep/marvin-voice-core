"""TDD: DJ 串場升級成「說故事」而非「唸資訊」。

2026-07-15 使用者：兩天前把 DJ 從 15s 砍到 5s 是因為在唸冗長 YouTube 標題資訊。
改成「只說故事不唸資訊」後可以放寬。新增兩條沉浸感 context：
1. 上一首 ↔ 下一首故事延伸（stream_history 已存，接進 prompt context）
2. 環境沉浸（台北 + 季節，由日期推）

並把 human LLM 串場的長度 gate 從 music_intro(5s) 放寬到 dj_story，
讓 60-90 字的故事不被砍成 16 字；Marvin 模板 / themed 理由維持 5s。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import dj_topic_selector



def _weights(monkeypatch, **w):
    """扭蛋池權重固定（dj_topic_selector.MODE_WEIGHTS）：串場 mode 改成加權隨機後，
    斷言特定 mode 素材/路徑的測試要把權重釘住才是決定性的。"""
    import dj_topic_selector
    monkeypatch.setattr(dj_topic_selector, "MODE_WEIGHTS", w)


def _no_quick(monkeypatch):
    """排除 quick（本地模板、不呼叫 LLM），其餘照預設權重——只在乎有走 LLM 路徑的測試用。"""
    import dj_topic_selector
    w = dict(dj_topic_selector.MODE_WEIGHTS)
    w["quick"] = 0.0
    monkeypatch.setattr(dj_topic_selector, "MODE_WEIGHTS", w)

def _only(monkeypatch, *modes):
    """扭蛋池固定只抽 modes 列的那些（其餘權重 0）——這幾條測試斷言 ctx 帶特定 mode
    的素材，本地扭蛋池改版後 mode 不再是決定性優先序，用固定權重讓測試維持決定性。"""
    monkeypatch.setattr(dj_topic_selector, "MODE_WEIGHTS", {m: 1.0 for m in modes})


def _exclude(monkeypatch, *modes):
    """扭蛋池排除 modes 列的那些（權重 0，其餘照舊 1.0）——這幾條測試只在乎 LLM 有被
    呼叫，不在乎抽中哪個具體 mode，只需要排除會跳過 LLM 的 quick。"""
    weights = {m: 1.0 for m in dj_topic_selector.NON_TOPIC_MODES + dj_topic_selector.TOPIC_MODES}
    for m in modes:
        weights[m] = 0.0
    monkeypatch.setattr(dj_topic_selector, "MODE_WEIGHTS", weights)


def _make_cog(est_per_char: float = 0.0, tmp_path=None):
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.tts_engine = MagicMock()
    bot.tts_engine.generate_audio = AsyncMock(return_value="/tmp/dj_audio.opus")
    if est_per_char > 0:
        # 真實字速估算：讓 truncate gate 真的會作用（測長度放寬）
        bot.tts_engine.get_estimated_duration = MagicMock(
            side_effect=lambda t: len(t) * est_per_char
        )
    else:
        bot.tts_engine.get_estimated_duration = MagicMock(return_value=3.0)
    bot.router = MagicMock()
    bot.router.generate_dynamic_system_msg = AsyncMock(
        return_value="這首夜曲接得剛好，一樣是心事重重的深夜"
    )
    bot.engine = MagicMock()
    bot.engine.conv_buffer = MagicMock()
    bot.engine.conv_buffer.get_last_n_utterances = MagicMock(return_value=[])
    bot.engine.post_summon_callback = None

    bot.music_memory = MagicMock()
    bot.music_memory._key = MagicMock(return_value="song_key_xyz")
    bot.music_memory._data = {"songs": {}}
    bot.music_memory.time_slot = MagicMock(return_value="深夜")

    from cogs.music_cog import MusicCog
    cog = MusicCog(bot)
    cog._enable_dj_news_fetch = False
    if tmp_path is not None:
        # ⚠️ 沒注入時 _dj_topic_store() 是 lazy singleton，讀寫真正的
        # records/dj_topic_cooldown.json——斷言 fallback 落在特定 mode
        # （atmosphere）的測試沒隔離會被硬碟上殘留的 _last_fallback_mode 汙染，
        # 隨機測出不同結果（見 tests/test_dj_gps_environment.py 同款修法）。
        from dj_topic_selector import TopicCooldownStore
        cog._dj_topic_cooldown_store = TopicCooldownStore(path=str(tmp_path / "dj_topic_cooldown.json"))
    return cog


def _info(title="周杰倫 - 夜曲", requester="大肚"):
    return {
        "title": title,
        "uploader": "周杰倫",
        "requested_by": requester,
        "url": "https://example/x",
    }


def _ctx_str(cog):
    """取出傳給 LLM 的 context 字串。"""
    call = cog.bot.router.generate_dynamic_system_msg.call_args
    assert call is not None, "generate_dynamic_system_msg 應被呼叫"
    return call.kwargs.get("context", "") or (call.args[1] if len(call.args) > 1 else "")


# ── 1. 上一首 ↔ 下一首故事延伸 ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_context_includes_previous_song(monkeypatch):
    """stream_history 有上一首 → context 帶「上一首」+ 該歌名，讓 DJ 做故事延伸。"""
    _no_quick(monkeypatch)
    cog = _make_cog()
    cog.stream_history = [_info(title="陶喆 - 普通朋友", requester="狗與露")]
    await cog._fetch_dj_interjection_raw(_info(title="周杰倫 - 夜曲", requester="大肚"))
    ctx = _ctx_str(cog)
    assert "上一首" in ctx, f"context 應帶上一首資訊: {ctx!r}"
    assert "普通朋友" in ctx, f"context 應含上一首歌名: {ctx!r}"


@pytest.mark.asyncio
async def test_context_skips_previous_when_same_title(monkeypatch):
    """history 最後一首就是自己（Play-First 背景路徑）→ 不當上一首，往前找。"""
    _no_quick(monkeypatch)
    cog = _make_cog()
    cur = _info(title="周杰倫 - 夜曲", requester="大肚")
    cog.stream_history = [
        _info(title="陶喆 - 飛機場的 10:30", requester="Alice"),
        cur,  # 自己已在 history 尾端
    ]
    await cog._fetch_dj_interjection_raw(cur)
    ctx = _ctx_str(cog)
    assert "飛機場" in ctx, f"應跳過自己、取真正上一首: {ctx!r}"


@pytest.mark.asyncio
async def test_context_no_previous_song_when_history_empty(monkeypatch):
    """history 空 → 不硬塞上一首（第一首歌沒有故事延伸）。"""
    _no_quick(monkeypatch)
    cog = _make_cog()
    cog.stream_history = []
    await cog._fetch_dj_interjection_raw(_info())
    ctx = _ctx_str(cog)
    assert "上一首" not in ctx, f"history 空時不該有上一首行: {ctx!r}"


# ── 2. 環境沉浸（城市 + 季節）─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_context_includes_environment_city_and_season(tmp_path, monkeypatch):
    """context 帶環境行：城市（無 GPS 訊號時退回家裡預設台中）+ 季節（春/夏/秋/冬其一）。

    環境行現在只在本地扭蛋池選中 "atmosphere" 時才進 ctx（見
    dj_topic_selector.select_mode）。清空 life_cores + 隔離的 topic store + 固定
    MODE_WEIGHTS 讓扭蛋池必抽中 atmosphere（不隔離/不固定會被硬碟上殘留的
    _last_fallback_mode 或扭蛋隨機性影響，見 _make_cog 的 tmp_path 說明）。
    """
    _only(monkeypatch, "atmosphere")
    cog = _make_cog(tmp_path=tmp_path)
    cog._life_cores = MagicMock(return_value=[])
    await cog._fetch_dj_interjection_raw(_info())
    ctx = _ctx_str(cog)
    assert "台中" in ctx, f"context 應含城市: {ctx!r}"
    assert any(s in ctx for s in "春夏秋冬"), f"context 應含季節: {ctx!r}"


# ── 3. 長度 gate 放寬（human LLM 故事路徑）──────────────────────────────────

@pytest.mark.asyncio
async def test_human_story_not_truncated_to_short(monkeypatch):
    """human LLM 故事 ~70 字不該被砍成 16 字（放寬到 dj_story gate）。

    排除 quick（唯一會跳過 LLM、直接回本地模板的 mode），確保這條測試量的是 LLM
    輸出的截斷行為，不會被扭蛋池隨機落在 quick 干擾。
    """
    _exclude(monkeypatch, "quick")
    cog = _make_cog(est_per_char=0.3)  # 70字≈21s
    story = (
        "剛才那首老靈魂的餘溫還在，窗外的雨也還沒停，"
        "接下來這首夜曲一樣心事重重，很適合現在這種誰都不想睡的深夜，"
        "大肚點的，我們慢慢聽"
    )
    assert len(story) >= 60
    cog.bot.router.generate_dynamic_system_msg = AsyncMock(return_value=story)
    result = await cog._fetch_dj_interjection_raw(_info(requester="大肚"))
    assert result is not None
    # 舊 music_intro 5s gate 會砍到 ~16 字；放寬後應保留大部分故事
    assert len(result["text"]) >= 55, f"故事被過度截斷: {result['text']!r}"


@pytest.mark.asyncio
async def test_marvin_autopilot_phrase_not_cut_to_garbage(monkeypatch):
    """Marvin autopilot 短語（含長 YouTube 標題）不該被 5s 砍成殘句（如「狗與露」）——dj_story gate。

    排除 quick，避免扭蛋池隨機落在本地模板，蓋掉這條測試要驗的 LLM 空手→autopilot
    模板路徑。
    """
    _exclude(monkeypatch, "quick")
    cog = _make_cog(est_per_char=0.3)
    long_phrase = "狗與露，給你首新的《Jay Chou 周杰倫 Aurora in July 七月的極光》，接著剛才的氣氛慢慢聽"
    assert len(long_phrase) >= 40
    # autopilot 改走 LLM 雞湯後，模板退居 fallback：讓 LLM 空手以走到模板路徑。
    cog.bot.router.generate_dynamic_system_msg = AsyncMock(return_value="")
    cog._autopilot_dj_phrase = MagicMock(return_value=long_phrase)
    info = _info(title="Jay Chou 周杰倫 Aurora in July 七月的極光", requester="Marvin推薦（為狗與露）")
    result = await cog._fetch_dj_interjection_raw(info)
    assert result is not None
    # 舊 music_intro 5s → 砍成「狗與露」；dj_story gate 下應保留大部分
    assert len(result["text"]) >= 35, f"Marvin autopilot 被砍成殘句: {result['text']!r}"


# ── 4. 歌曲素材 guide mode 取代舊的「音樂賞析」（SongKnowledgeStore.get_or_extract_insight）──

@pytest.mark.asyncio
async def test_dj_interjection_uses_song_guide_mode_and_skips_old_insight(tmp_path, monkeypatch):
    """_dj_song_material 命中（有導聆可講）→ has_guide=True 進扭蛋池，固定權重讓池必
    抽中 guide，context 帶長版導聆原文；且舊的 SongKnowledgeStore.get_or_extract_insight
    音樂賞析路徑不該再被呼叫（歌曲導聆取代它）。"""
    from unittest.mock import patch

    _only(monkeypatch, "guide")
    cog = _make_cog(tmp_path=tmp_path)
    cog.stream_history = []
    cog._life_cores = MagicMock(return_value=[])
    canon = {"artist": "周杰倫", "title": "夜曲", "album": "十一月的蕭邦", "year": 2005}
    guide = ("破除印象：這首曲子聽起來哀傷但其實編曲很複雜。"
             "聽覺錨點：注意鋼琴與弦樂的對話。戴上耳機吧。")
    cog._dj_song_material = AsyncMock(return_value=(canon, guide))

    with patch(
        "song_knowledge_store.SongKnowledgeStore.get_or_extract_insight",
        new=AsyncMock(return_value="不該被呼叫的舊音樂賞析"),
    ) as mock_insight:
        result = await cog._fetch_dj_interjection_raw(_info(title="周杰倫 - 夜曲", requester="大肚"))
        mock_insight.assert_not_called()

    assert result is not None
    ctx = _ctx_str(cog)
    assert guide in ctx, f"context 應含長版導聆原文: {ctx!r}"
    assert "不該被呼叫的舊音樂賞析" not in ctx


@pytest.mark.asyncio
async def test_dj_interjection_song_guide_miss_falls_back_without_guide_mode(tmp_path, monkeypatch):
    """_dj_song_material 查不到導聆（guide=None）→ has_guide=False，不進 guide 候選，
    跟舊版行為一致（照樣落到其他 fallback，這裡不用 atmosphere/quick 特定斷言，只驗證
    context 不會出現導聆素材字樣、也不會拋例外）。"""
    _no_quick(monkeypatch)
    cog = _make_cog(tmp_path=tmp_path)
    cog.stream_history = []
    cog._life_cores = MagicMock(return_value=[])
    cog._dj_song_material = AsyncMock(return_value=(None, None))

    result = await cog._fetch_dj_interjection_raw(_info(title="周杰倫 - 夜曲", requester="大肚"))
    assert result is not None
    ctx = _ctx_str(cog)
    assert "導聆素材" not in ctx


@pytest.mark.parametrize("requester,lane,expect_human", [
    ("大肚", "", True),                          # 真人點歌 → 可走付費
    ("Marvin推薦（為大肚）", "", False),          # autopilot → 只走免費、受預算
    ("大肚", "personal", False),                 # 個人歌單自動墊歌：掛真人名但不是真人點歌
])
@pytest.mark.asyncio
async def test_dj_song_material_human_only_for_real_requests(tmp_path, monkeypatch, requester, lane, expect_human):
    """個人歌單（_lane='personal'）requested_by 是真人名字但實為自動播放，不准燒付費額度。"""
    import audiophile_fetcher
    cog = _make_cog(tmp_path=tmp_path)
    store = MagicMock()
    cog._audiophile_deps = MagicMock(return_value=(store, MagicMock(), cog.bot.router))
    monkeypatch.setattr(audiophile_fetcher, "resolve_canon", AsyncMock(return_value=None))
    guide_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(audiophile_fetcher, "song_guide_for_dj", guide_mock)

    info = {"title": "周杰倫 - 夜曲", "requested_by": requester,
            "webpage_url": "https://www.youtube.com/watch?v=abcdefghijk"}
    if lane:
        info["_lane"] = lane
    await cog._dj_song_material(info, "夜曲", "周杰倫")

    kw = guide_mock.await_args.kwargs
    assert kw["human"] is expect_human
    if not expect_human:
        assert kw["paid_client"] is None


@pytest.mark.asyncio
async def test_dj_song_material_passes_raw_title_and_uploader_as_artist_hay(tmp_path, monkeypatch):
    """歌手守門的比對來源要含原始 YouTube 標題 + 頻道名（乾淨歌手常是空的）。"""
    import audiophile_fetcher
    cog = _make_cog(tmp_path=tmp_path)
    cog._audiophile_deps = MagicMock(return_value=(MagicMock(), MagicMock(), cog.bot.router))
    canon_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(audiophile_fetcher, "resolve_canon", canon_mock)
    monkeypatch.setattr(audiophile_fetcher, "song_guide_for_dj", AsyncMock(return_value=None))

    info = {"title": "帶我去找夜生活", "uploader": "告五人Accusefive", "requested_by": "大肚",
            "webpage_url": "https://www.youtube.com/watch?v=abcdefghijk"}
    await cog._dj_song_material(info, "帶我去找夜生活", "")

    hay = canon_mock.await_args.kwargs["artist_hay"]
    assert "帶我去找夜生活" in hay and "告五人Accusefive" in hay


@pytest.mark.asyncio
async def test_dj_song_material_passes_stream_url_duration_and_shared_breaker(tmp_path, monkeypatch):
    """resolve_canon 要拿到 stream_url/duration 去切 Shazam 音訊，breaker 是
    ShazamBreaker 實例且跨兩次呼叫是同一個（斷路狀態要跨歌累積，不能每次新建）。"""
    import audiophile_fetcher
    from shazam_identify import ShazamBreaker

    cog = _make_cog(tmp_path=tmp_path)
    cog._audiophile_deps = MagicMock(return_value=(MagicMock(), MagicMock(), cog.bot.router))
    canon_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(audiophile_fetcher, "resolve_canon", canon_mock)
    monkeypatch.setattr(audiophile_fetcher, "song_guide_for_dj", AsyncMock(return_value=None))

    info = {"title": "夜曲", "requested_by": "大肚", "url": "https://stream/example.m4a",
            "duration": 245, "webpage_url": "https://www.youtube.com/watch?v=abcdefghijk"}
    await cog._dj_song_material(info, "夜曲", "周杰倫")
    await cog._dj_song_material(info, "夜曲", "周杰倫")

    first_kw = canon_mock.await_args_list[0].kwargs
    second_kw = canon_mock.await_args_list[1].kwargs
    assert first_kw["stream_url"] == "https://stream/example.m4a"
    assert first_kw["duration"] == 245
    assert isinstance(first_kw["breaker"], ShazamBreaker)
    assert first_kw["breaker"] is second_kw["breaker"]
