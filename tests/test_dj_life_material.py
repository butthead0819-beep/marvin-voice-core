"""TDD: DJ 生活素材管線擴充（9/30 使用者定）。

漏斗：①日記 recent_life_cores_with_speakers 預設只留最新 3 條 → 放寬成全量隨機
②每日亮點（highlight_of_the_day）完全沒被 DJ 用到 → 接上（醫療健康句濾掉）
③自選歌情緒記憶查 requester，但 autopilot 的 requester 不是真人查不到 → 改查在場的人
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from dj_daily_highlight import highlight_life_cores
from dj_life_context import LifeCore


# ── 1〜3. dj_daily_highlight.highlight_life_cores（純函式）──────────────────

def test_highlight_life_cores_splits_filters_medical_and_short():
    highlight = (
        "在Grounded遊戲中打狼蛛。今天還在醫院裡，超級難睡。"
        "抱怨早上遛狗很忙，還得帶兒子去屏東。好"
    )
    cores = highlight_life_cores("狗與露", highlight)
    assert [c.text for c in cores] == [
        "狗與露：在Grounded遊戲中打狼蛛",
        "狗與露：抱怨早上遛狗很忙，還得帶兒子去屏東",
    ]
    assert all(c.speakers == ("狗與露",) for c in cores)


def test_highlight_life_cores_replaces_today_with_recently():
    cores = highlight_life_cores("大肚", "今天跑步跑了十公里")
    assert cores[0].text == "大肚：最近跑步跑了十公里"


def test_highlight_life_cores_empty_inputs():
    assert highlight_life_cores("", "隨便什麼") == []
    assert highlight_life_cores("大肚", None) == []
    assert highlight_life_cores("大肚", "") == []


def test_highlight_life_cores_truncates_long_sentence():
    long_sentence = "狗與露的" + "超長生活細節" * 20
    cores = highlight_life_cores("狗與露", long_sentence)
    assert len(cores[0].text) <= len("狗與露：") + 40


# ── 4. MusicDJLyricsMixin._present_highlight_cores ──────────────────────────

def _make_cog():
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.tts_engine = MagicMock()
    bot.tts_engine.generate_audio = AsyncMock(return_value="/tmp/dj_audio.opus")
    bot.tts_engine.get_estimated_duration = MagicMock(return_value=3.0)
    bot.router = MagicMock()
    bot.router.generate_dynamic_system_msg = AsyncMock(return_value="接得剛好")
    bot.engine = MagicMock()
    bot.engine.conv_buffer = MagicMock()
    bot.engine.conv_buffer.get_last_n_utterances = MagicMock(return_value=[])
    bot.engine.post_summon_callback = None
    bot.music_memory = MagicMock()
    bot.music_memory._key = MagicMock(return_value="song_key_xyz")
    bot.music_memory._data = {"songs": {}}
    bot.music_memory.time_slot = MagicMock(return_value="深夜")

    from cogs.music_cog import MusicCog
    from dj_topic_selector import TopicCooldownStore
    import tempfile
    cog = MusicCog(bot)
    cog._enable_dj_news_fetch = False
    cog._dj_topic_cooldown_store = TopicCooldownStore(tempfile.mktemp(suffix=".json"))
    return cog


def _info(title="周杰倫 - 夜曲", requester="大肚", **kw):
    d = {"title": title, "uploader": "周杰倫", "requested_by": requester,
         "url": "https://example/x"}
    d.update(kw)
    return d


def _ctx_str(cog):
    call = cog.bot.router.generate_dynamic_system_msg.call_args
    assert call is not None, "generate_dynamic_system_msg 應被呼叫"
    return call.kwargs.get("context", "") or (call.args[1] if len(call.args) > 1 else "")


def test_present_highlight_cores_returns_cores_for_present_members():
    cog = _make_cog()
    cog.bot.router.memory.get_player_memory = MagicMock(
        return_value={"highlight_of_the_day": "抱怨早上遛狗很忙，還得帶兒子去屏東"}
    )
    cores = cog._present_highlight_cores({"狗與露"})
    assert cores
    assert all(c.speakers == ("狗與露",) for c in cores)


def test_present_highlight_cores_none_when_present_speakers_unknown():
    cog = _make_cog()
    assert cog._present_highlight_cores(None) == []
    assert cog._present_highlight_cores(set()) == []


def test_present_highlight_cores_skips_member_on_error():
    cog = _make_cog()

    def _get_player_memory(m):
        if m == "大肚":
            raise RuntimeError("boom")
        return {"highlight_of_the_day": "抱怨早上遛狗很忙，還得帶兒子去屏東"}

    cog.bot.router.memory.get_player_memory = MagicMock(side_effect=_get_player_memory)
    cores = cog._present_highlight_cores({"大肚", "狗與露"})
    assert cores
    assert all(c.speakers == ("狗與露",) for c in cores)


# ── 5. _life_cores_async：日記全量 + 每日亮點合併 + 打亂 ─────────────────────

def _entry(ts_str: str, core: str, salience: str = "中"):
    e = MagicMock()
    e.ts_str = ts_str
    e.core = core
    e.salience = salience
    e.is_sensitive = False
    e.participants = None
    return e


def _ts(now: float, days_ago: float) -> str:
    import datetime as _dt
    return _dt.datetime.fromtimestamp(now - days_ago * 86400.0).strftime("%Y-%m-%d %H:%M:%S")


@pytest.mark.asyncio
async def test_life_cores_async_merges_full_diary_and_highlight_then_shuffles(monkeypatch):
    now = time.time()
    entries = [_entry(_ts(now, 1.0), f"事件{i}") for i in range(10)]
    cog = _make_cog()
    cog._load_summary_entries = MagicMock(return_value=entries)
    cog._present_highlight_cores = MagicMock(
        return_value=[LifeCore(text="狗與露：抱怨早上遛狗很忙", meme_id="", speakers=("狗與露",))]
    )

    shuffle_calls = []
    import cogs.music_cog_dj_lyrics as dj_lyrics_mod
    monkeypatch.setattr(
        dj_lyrics_mod.random, "shuffle",
        lambda seq: shuffle_calls.append(list(seq)),
    )

    result = await cog._life_cores_async()
    assert len(result) == 11
    assert shuffle_calls, "應呼叫 random.shuffle 打亂候選"


# ── 歌詞槽品質：段落標記、純哼唱不當副歌（9/30 實測挑到「[CHORUS]」「Oh-oh-oh」）────

def test_chorus_pick_skips_section_tags():
    from dj_lyric_pick import pick_chorus_line
    lyr = "[CHORUS]\n我想要的其實很簡單\n[CHORUS]\n我想要的其實很簡單"
    assert pick_chorus_line(lyr) == "我想要的其實很簡單"


def test_chorus_pick_skips_pure_vocables():
    from dj_lyric_pick import pick_chorus_line
    lyr = "Oh-oh-oh-oh-oh, oh-oh\n愛的痛了痛的哭了\nOh-oh-oh-oh-oh, oh-oh\nOh-oh-oh-oh-oh, oh-oh\n愛的痛了痛的哭了"
    assert pick_chorus_line(lyr) == "愛的痛了痛的哭了"


def test_chorus_pick_keeps_real_english_chorus():
    from dj_lyric_pick import pick_chorus_line
    assert pick_chorus_line("No doubt anymore\nverse line here\nNo doubt anymore") == "No doubt anymore"
