"""DJ 口白行為護欄（註冊表第 2 刀）——ctx/TTS 快照。

幫**現行** `_fetch_dj_interjection_raw`（cogs/music_cog_dj_lyrics.py）在固定的
mode/素材輸入下拍快照：這輪有沒有呼叫 LLM、餵給 LLM 的 context 內容、TTS 拿到的
emotion kwarg、最終文字。這些快照是現行程式碼「目前長怎樣」的事實紀錄，不是重新
設計的規格——第 3 刀把口白決策搬進註冊表時，要以「這份快照全部不變」為驗收標準。

golden 檔案在 tests/snapshots/dj_narration/<case>.txt，用
`DJ_SNAPSHOT_UPDATE=1 pytest tests/test_dj_narration_snapshots.py` 生成/更新。
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import cogs.music_cog_dj_lyrics as dj_lyrics_mod
import dj_narration_log
from cogs.music_cog import MusicCog
from dj_topic_selector import TopicCooldownStore

_SNAPSHOT_DIR = Path(__file__).parent / "snapshots" / "dj_narration"

# 長度需 ≥10 字且不含 dj_prompt_builder.FORBIDDEN_DJ_PHRASES 任何一句，否則
# `_is_qualified_dj_script` 會判不合格、整輪改走 autopilot_template/fixed_announcement
# fallback，蓋掉我們想拍的「LLM 文字直接被採用」這一段快照。
_LLM_TEXT = "這是一句測試用的口白內容。"


def _assert_snapshot(case: str, actual: str) -> None:
    path = _SNAPSHOT_DIR / f"{case}.txt"
    if os.environ.get("DJ_SNAPSHOT_UPDATE") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual, encoding="utf-8")
        return
    if not path.exists():
        pytest.fail(f"缺 golden：{path}；用 DJ_SNAPSHOT_UPDATE=1 生成")
    golden = path.read_text(encoding="utf-8")
    assert actual == golden


def _render_snapshot(mode_arg: str, bot, result: dict | None) -> str:
    llm_call = bot.router.generate_dynamic_system_msg.call_args
    llm_called = llm_call is not None
    context = llm_call.kwargs.get("context") if llm_called else "<none>"
    tts_call = bot.tts_engine.generate_audio.call_args
    tts_emotion = tts_call.kwargs.get("emotion") if tts_call is not None else None
    text = result.get("text") if result else None
    return (
        f"MODE_ARG={mode_arg}\n"
        f"LLM_CALLED={llm_called}\n"
        f"TTS_EMOTION={tts_emotion}\n"
        "---CONTEXT---\n"
        f"{context}\n"
        "---TEXT---\n"
        f"{text if text is not None else '<none>'}"
    )


def _make_fake_bank(*, take_return=(), is_consumed=False):
    bank = MagicMock()
    bank.take = MagicMock(return_value=list(take_return))
    bank.is_consumed = MagicMock(return_value=is_consumed)
    bank.snapshot = MagicMock()
    bank.mark_consumed = MagicMock()
    return bank


def _make_cog(tmp_path, monkeypatch):
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.tts_engine.generate_audio = AsyncMock(return_value=None)
    bot.tts_engine.get_estimated_duration = MagicMock(return_value=3.0)
    bot.router.generate_dynamic_system_msg = AsyncMock(return_value=_LLM_TEXT)
    bot.engine.conv_buffer.get_last_n_utterances = MagicMock(return_value=[])
    bot.engine.conv_buffer.get_history = MagicMock(return_value=[])
    bot.engine.post_summon_callback = None
    bot.music_memory._key = MagicMock(return_value="song_key_xyz")
    bot.music_memory._data = {"songs": {}}
    bot.music_memory.time_slot = MagicMock(return_value="午後")

    cog = MusicCog(bot)
    cog._enable_dj_news_fetch = False
    cog._dj_topic_cooldown_store = TopicCooldownStore(path=str(tmp_path / "cd.json"))

    monkeypatch.setattr(dj_lyrics_mod.random, "choice", lambda seq: list(seq)[0])
    monkeypatch.setattr(dj_lyrics_mod.random, "shuffle", lambda seq: None)

    monkeypatch.setattr(cog, "_current_season", lambda: "秋天")
    # format_temporal_atmosphere 用模組自己的 time.time() 算星期/時段 → 固定在 2026-10-08 15:00（台灣）
    import types
    import dj_social_affinity
    monkeypatch.setattr(dj_social_affinity, "time", types.SimpleNamespace(time=lambda: 1791443000.0))
    monkeypatch.setattr(cog, "_city_label", lambda: "台北")
    monkeypatch.setattr(cog, "_dj_clean_name", lambda info: ("測試歌", "測試歌手"))
    monkeypatch.setattr(cog, "_dj_song_material", AsyncMock(return_value=(None, None)))
    monkeypatch.setattr(cog, "_life_cores_async", AsyncMock(return_value=[]))
    monkeypatch.setattr(cog, "_present_interests", lambda: [])
    monkeypatch.setattr(cog, "_fetch_news_items_async", AsyncMock(return_value=[]))
    monkeypatch.setattr(cog, "_present_callbacks", lambda present_members: ([], {}))
    monkeypatch.setattr(cog, "_autopilot_pick_reason", lambda info: "")
    monkeypatch.setattr(cog, "_dj_heat_bank", lambda: _make_fake_bank())
    monkeypatch.setattr(dj_narration_log, "recent_narrations_for_song", lambda *a, **k: [])
    monkeypatch.setattr(dj_narration_log, "log_dj_narration", lambda record: None)
    monkeypatch.setattr(dj_narration_log, "log_song_play", lambda record: None)
    monkeypatch.setattr(dj_narration_log, "log_song_skip", lambda record: None)

    # 現行邏輯：_last_dj_joke_ts is None 時不走笑話分支（__init__ 預設是 time.time()）。
    cog._last_dj_joke_ts = None

    return bot, cog


def _make_info(**overrides) -> dict:
    info = {
        "title": "測試歌 - 測試歌手",
        "uploader": "測試歌手",
        "requested_by": "Alice",
        "url": "https://example/x",
        "webpage_url": "https://www.youtube.com/watch?v=abcdefghijk",
    }
    info.update(overrides)
    return info


@pytest.mark.asyncio
async def test_snapshot_life(tmp_path, monkeypatch):
    bot, cog = _make_cog(tmp_path, monkeypatch)
    info = _make_info()
    with patch("dj_narration_orchestrator.choose_mode",
               return_value=("Alice 最近在學日文", "life")):
        result = await cog._fetch_dj_interjection_raw(info)
    _assert_snapshot("life", _render_snapshot("life", bot, result))


@pytest.mark.asyncio
async def test_snapshot_interest(tmp_path, monkeypatch):
    bot, cog = _make_cog(tmp_path, monkeypatch)
    info = _make_info()
    with patch("dj_narration_orchestrator.choose_mode",
               return_value=("Alice 喜歡登山", "interest")):
        result = await cog._fetch_dj_interjection_raw(info)
    _assert_snapshot("interest", _render_snapshot("interest", bot, result))


@pytest.mark.asyncio
async def test_snapshot_news(tmp_path, monkeypatch):
    bot, cog = _make_cog(tmp_path, monkeypatch)
    info = _make_info()
    with patch("dj_narration_orchestrator.choose_mode",
               return_value=("颱風明天轉向", "news")):
        result = await cog._fetch_dj_interjection_raw(info)
    _assert_snapshot("news", _render_snapshot("news", bot, result))


@pytest.mark.asyncio
async def test_snapshot_callback(tmp_path, monkeypatch):
    bot, cog = _make_cog(tmp_path, monkeypatch)
    info = _make_info()
    cb_item = {"text": "買叉子"}
    monkeypatch.setattr(
        cog, "_present_callbacks",
        lambda present_members: (
            ["Alice 之前說要買叉子"],
            {"Alice 之前說要買叉子": ("Alice", cb_item)},
        ),
    )
    with patch("dj_narration_orchestrator.choose_mode",
               return_value=("Alice 之前說要買叉子", "callback")):
        result = await cog._fetch_dj_interjection_raw(info)
    _assert_snapshot("callback", _render_snapshot("callback", bot, result))
    bot.router.memory.consume_callback.assert_called_once_with("Alice", cb_item)


@pytest.mark.asyncio
async def test_snapshot_activity(tmp_path, monkeypatch):
    bot, cog = _make_cog(tmp_path, monkeypatch)
    info = _make_info()
    with patch("dj_narration_orchestrator.choose_mode",
               return_value=("Alice 正在玩《Ball X Pit》", "activity")):
        result = await cog._fetch_dj_interjection_raw(info)
    _assert_snapshot("activity", _render_snapshot("activity", bot, result))


@pytest.mark.asyncio
async def test_snapshot_memory_match(tmp_path, monkeypatch):
    bot, cog = _make_cog(tmp_path, monkeypatch)
    info = _make_info()
    with patch("dj_narration_orchestrator.choose_mode",
               return_value=("Alice 說過最愛這首", "memory_match")):
        result = await cog._fetch_dj_interjection_raw(info)
    _assert_snapshot("memory_match", _render_snapshot("memory_match", bot, result))


@pytest.mark.asyncio
async def test_snapshot_atmosphere(tmp_path, monkeypatch):
    bot, cog = _make_cog(tmp_path, monkeypatch)
    info = _make_info()
    with patch("dj_narration_orchestrator.choose_mode",
               return_value=(None, "atmosphere")):
        result = await cog._fetch_dj_interjection_raw(info)
    _assert_snapshot("atmosphere", _render_snapshot("atmosphere", bot, result))


@pytest.mark.asyncio
async def test_snapshot_quick(tmp_path, monkeypatch):
    bot, cog = _make_cog(tmp_path, monkeypatch)
    info = _make_info()
    with patch("dj_narration_orchestrator.choose_mode",
               return_value=(None, "quick")):
        result = await cog._fetch_dj_interjection_raw(info)
    _assert_snapshot("quick", _render_snapshot("quick", bot, result))


@pytest.mark.asyncio
async def test_snapshot_guide(tmp_path, monkeypatch):
    bot, cog = _make_cog(tmp_path, monkeypatch)
    info = _make_info()
    monkeypatch.setattr(
        cog, "_dj_song_material",
        AsyncMock(return_value=(None, "這首歌的導聆：前奏的鋼琴是重點。")),
    )
    with patch("dj_narration_orchestrator.choose_mode",
               return_value=(None, "guide")):
        result = await cog._fetch_dj_interjection_raw(info)
    _assert_snapshot("guide", _render_snapshot("guide", bot, result))


@pytest.mark.asyncio
async def test_snapshot_conversation(tmp_path, monkeypatch):
    bot, cog = _make_cog(tmp_path, monkeypatch)
    info = _make_info()
    bot.engine.conv_buffer.get_last_n_utterances = MagicMock(return_value=[
        {"speaker": "Bob", "text": "今天好累", "ts": 1},
        {"speaker": "Alice", "text": "我也是", "ts": 2},
    ])
    fake_bank = _make_fake_bank()
    monkeypatch.setattr(cog, "_dj_heat_bank", lambda: fake_bank)
    with patch("dj_narration_orchestrator.choose_mode",
               return_value=(None, "conversation")):
        result = await cog._fetch_dj_interjection_raw(info)
    _assert_snapshot("conversation", _render_snapshot("conversation", bot, result))
    fake_bank.mark_consumed.assert_called_once()  # 抽中 conversation 要同步標記原句已用


@pytest.mark.asyncio
async def test_snapshot_conversation_downgrade(tmp_path, monkeypatch):
    bot, cog = _make_cog(tmp_path, monkeypatch)
    info = _make_info()
    bot.engine.conv_buffer.get_last_n_utterances = MagicMock(return_value=[
        {"speaker": "Bob", "text": "今天好累", "ts": 1},
        {"speaker": "Alice", "text": "我也是", "ts": 2},
    ])
    state = {"after_select": False}
    fake_bank = MagicMock()
    fake_bank.take = MagicMock(return_value=[])
    fake_bank.is_consumed = MagicMock(side_effect=lambda entry: state["after_select"])
    fake_bank.snapshot = MagicMock()
    fake_bank.mark_consumed = MagicMock()
    monkeypatch.setattr(cog, "_dj_heat_bank", lambda: fake_bank)

    def _select_side_effect(*args, **kwargs):
        state["after_select"] = True
        return (None, "conversation")

    with patch("dj_narration_orchestrator.choose_mode",
               side_effect=_select_side_effect):
        result = await cog._fetch_dj_interjection_raw(info)
    _assert_snapshot("conversation_downgrade", _render_snapshot("conversation_downgrade", bot, result))
    fake_bank.mark_consumed.assert_not_called()


@pytest.mark.asyncio
async def test_snapshot_reason(tmp_path, monkeypatch):
    bot, cog = _make_cog(tmp_path, monkeypatch)
    info = _make_info(requested_by="Marvin推薦（點給大家）")
    monkeypatch.setattr(cog, "_autopilot_pick_reason", lambda info: "這首是 Alice 點過的歌")
    with patch("dj_narration_orchestrator.choose_mode",
               return_value=(None, "reason")):
        result = await cog._fetch_dj_interjection_raw(info)
    _assert_snapshot("reason", _render_snapshot("reason", bot, result))


@pytest.mark.asyncio
async def test_snapshot_song(tmp_path, monkeypatch):
    bot, cog = _make_cog(tmp_path, monkeypatch)
    info = _make_info(requested_by="Marvin推薦（點給大家）")
    info["_server_plays"] = 1
    # 給導聆稿，song 分支才有素材可抽（沒素材會降級成 quick，等於沒測到 song）
    monkeypatch.setattr(cog, "_dj_song_material",
                        AsyncMock(return_value=(None, "這首歌的導聆：前奏的鋼琴是重點。")))
    with patch("dj_narration_orchestrator.choose_mode",
               return_value=(None, "song")):
        result = await cog._fetch_dj_interjection_raw(info)
    _assert_snapshot("song", _render_snapshot("song", bot, result))


@pytest.mark.asyncio
async def test_snapshot_revival(tmp_path, monkeypatch):
    bot, cog = _make_cog(tmp_path, monkeypatch)
    info = _make_info()
    fake_bank = _make_fake_bank(take_return=["Bob：「今天好累」", "Alice：「我也是」"])
    monkeypatch.setattr(cog, "_dj_heat_bank", lambda: fake_bank)
    with patch(
        "dj_narration_orchestrator.select_mode",
        MagicMock(side_effect=AssertionError("revival 不該呼叫扭蛋")),
    ):
        result = await cog._fetch_dj_interjection_raw(info)
    _assert_snapshot("revival", _render_snapshot("revival", bot, result))
