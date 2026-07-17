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


def _make_cog(est_per_char: float = 0.0):
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
async def test_context_includes_previous_song():
    """stream_history 有上一首 → context 帶「上一首」+ 該歌名，讓 DJ 做故事延伸。"""
    cog = _make_cog()
    cog.stream_history = [_info(title="陶喆 - 普通朋友", requester="狗與露")]
    await cog._fetch_dj_interjection_raw(_info(title="周杰倫 - 夜曲", requester="大肚"))
    ctx = _ctx_str(cog)
    assert "上一首" in ctx, f"context 應帶上一首資訊: {ctx!r}"
    assert "普通朋友" in ctx, f"context 應含上一首歌名: {ctx!r}"


@pytest.mark.asyncio
async def test_context_skips_previous_when_same_title():
    """history 最後一首就是自己（Play-First 背景路徑）→ 不當上一首，往前找。"""
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
async def test_context_no_previous_song_when_history_empty():
    """history 空 → 不硬塞上一首（第一首歌沒有故事延伸）。"""
    cog = _make_cog()
    cog.stream_history = []
    await cog._fetch_dj_interjection_raw(_info())
    ctx = _ctx_str(cog)
    assert "上一首" not in ctx, f"history 空時不該有上一首行: {ctx!r}"


# ── 2. 環境沉浸（城市 + 季節）─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_context_includes_environment_city_and_season():
    """context 帶環境行：城市（台北）+ 季節（春/夏/秋/冬其一）。"""
    cog = _make_cog()
    await cog._fetch_dj_interjection_raw(_info())
    ctx = _ctx_str(cog)
    assert "台北" in ctx, f"context 應含城市: {ctx!r}"
    assert any(s in ctx for s in "春夏秋冬"), f"context 應含季節: {ctx!r}"


# ── 3. 長度 gate 放寬（human LLM 故事路徑）──────────────────────────────────

@pytest.mark.asyncio
async def test_human_story_not_truncated_to_short():
    """human LLM 故事 ~70 字不該被砍成 16 字（放寬到 dj_story gate）。"""
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
async def test_marvin_themed_reason_plays_full_dj_story_gate():
    """Marvin themed 選歌理由（LLM 策展的故事）走 dj_story gate、完整播出，不被 5s 砍。"""
    cog = _make_cog(est_per_char=0.3)
    long_reason = "這首歌是今晚主題的核心，把大家剛剛聊的疲憊都收進了旋律裡，慢慢帶你們降落到夜的最底"
    assert len(long_reason) >= 30
    info = _info(title="周杰倫 - 夜曲", requester="Marvin推薦")
    info["_lane"] = "themed"
    info["_pick_reason"] = long_reason
    result = await cog._fetch_dj_interjection_raw(info)
    assert result is not None
    assert len(result["text"]) >= 30, f"themed 故事應完整播出、不被 5s 砍: {result['text']!r}"


@pytest.mark.asyncio
async def test_marvin_autopilot_phrase_not_cut_to_garbage():
    """Marvin autopilot 短語（含長 YouTube 標題）不該被 5s 砍成殘句（如「狗與露」）——dj_story gate。"""
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
