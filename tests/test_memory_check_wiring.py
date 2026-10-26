"""換歌空檔記憶確認（cogs/voice_controller_memory_check.py）接線測試。期望值一律寫死。"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import cogs.voice_controller_memory_check as vcmc
from tests.test_playback_tts_path import _make_cog


@pytest.fixture(autouse=True)
def _reset_log_path(tmp_path, monkeypatch):
    monkeypatch.setattr(vcmc, "_LOG_PATH", str(tmp_path / "memory_checks.jsonl"))
    monkeypatch.setattr(vcmc, "_CATEGORY_PATH", str(tmp_path / "taste_categories.json"))
    yield


# ── maybe_ask_memory_check ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_maybe_ask_memory_check_asks_and_opens_window():
    cog, _ = _make_cog()
    cog._memory_check_window = None
    cog.last_player_speech_time = time.time() - 300
    cog.get_online_members = MagicMock(return_value=["狗與露"])
    cog.bot.router.memory.has_player = MagicMock(return_value=True)
    cog.bot.router.memory.get_player_memory = MagicMock(
        return_value={"taste": {"張雨生": {"score": 5.0, "last_update": time.time() - 100}}, "taboos": []})
    with patch.object(vcmc, "_load_artist_names", return_value={"張雨生"}):
        cog.play_tts = AsyncMock(return_value=True)
        result = await cog.maybe_ask_memory_check()

    assert result is True
    assert cog._memory_check_window["speaker"] == "狗與露"
    assert cog._memory_check_window["item"] == "張雨生"
    rows = vcmc._load_memory_check_log()
    assert len(rows) == 1
    assert rows[0]["event"] == "asked" and rows[0]["speaker"] == "狗與露" and rows[0]["item"] == "張雨生"


@pytest.mark.asyncio
async def test_maybe_ask_memory_check_play_tts_none_does_not_write_or_open_window():
    cog, _ = _make_cog()
    cog._memory_check_window = None
    cog.last_player_speech_time = time.time() - 300
    cog.get_online_members = MagicMock(return_value=["狗與露"])
    cog.bot.router.memory.has_player = MagicMock(return_value=True)
    cog.bot.router.memory.get_player_memory = MagicMock(
        return_value={"taste": {"張雨生": {"score": 5.0, "last_update": time.time() - 100}}, "taboos": []})
    with patch.object(vcmc, "_load_artist_names", return_value={"張雨生"}):
        cog.play_tts = AsyncMock(return_value=None)
        result = await cog.maybe_ask_memory_check()

    assert result is False
    assert cog._memory_check_window is None
    assert vcmc._load_memory_check_log() == []


# ── _memory_check_intercept ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_intercept_speaker_says_no_clears_window_and_schedules_apply():
    cog, _ = _make_cog()
    cog._memory_check_window = {"speaker": "狗與露", "item": "張雨生", "deadline": time.time() + 10}
    cog._memory_check_apply = AsyncMock()

    result = cog._memory_check_intercept("狗與露", "不喜歡了", False)
    await asyncio.sleep(0)

    assert result is True
    assert cog._memory_check_window is None
    cog._memory_check_apply.assert_called_once_with("狗與露", "張雨生", False)
    rows = vcmc._load_memory_check_log()
    assert rows == [{"ts": rows[0]["ts"], "event": "answer", "speaker": "狗與露",
                     "item": "張雨生", "answer": "no"}]


def test_intercept_other_speaker_ignored_window_stays():
    cog, _ = _make_cog()
    window = {"speaker": "狗與露", "item": "張雨生", "deadline": time.time() + 10}
    cog._memory_check_window = window

    result = cog._memory_check_intercept("大肚", "不喜歡了", False)

    assert result is False
    assert cog._memory_check_window is window


@pytest.mark.asyncio
async def test_intercept_unclear_returns_false_and_logs_unclear():
    cog, _ = _make_cog()
    cog._memory_check_window = {"speaker": "狗與露", "item": "張雨生", "deadline": time.time() + 10}
    cog._memory_check_apply = AsyncMock()

    result = cog._memory_check_intercept("狗與露", "嗯", False)

    assert result is False
    assert cog._memory_check_window is None
    cog._memory_check_apply.assert_not_called()
    rows = vcmc._load_memory_check_log()
    assert rows[0]["answer"] == "unclear"


def test_intercept_is_wake_check_true_returns_false():
    cog, _ = _make_cog()
    cog._memory_check_window = {"speaker": "狗與露", "item": "張雨生", "deadline": time.time() + 10}

    result = cog._memory_check_intercept("狗與露", "不喜歡了", True)

    assert result is False
    assert cog._memory_check_window is not None


# ── _memory_check_apply ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_apply_confirmed_records_taste_signal():
    cog, _ = _make_cog()
    cog.play_tts = AsyncMock(return_value=True)
    cog.bot.router.memory.record_taste_signal = MagicMock()

    await cog._memory_check_apply("狗與露", "張雨生", True)

    cog.bot.router.memory.record_taste_signal.assert_called_once_with(
        "狗與露", "張雨生", 1.5, reason="memory_check_confirm")


@pytest.mark.asyncio
async def test_apply_not_confirmed_removes_taste_item():
    cog, _ = _make_cog()
    cog.play_tts = AsyncMock(return_value=True)
    cog.bot.router.memory.remove_taste_item = MagicMock()

    await cog._memory_check_apply("狗與露", "張雨生", False)

    cog.bot.router.memory.remove_taste_item.assert_called_once_with("狗與露", "張雨生")


# ── _memory_check_expire ──────────────────────────────────────────────────────

def test_expire_still_matching_window_logs_none_and_clears():
    cog, _ = _make_cog()
    cog._memory_check_window = {"speaker": "狗與露", "item": "張雨生", "deadline": time.time() + 10}

    cog._memory_check_expire("狗與露", "張雨生")

    assert cog._memory_check_window is None
    rows = vcmc._load_memory_check_log()
    assert rows[0]["answer"] == "none" and rows[0]["speaker"] == "狗與露"


def test_expire_window_already_cleared_does_not_log():
    cog, _ = _make_cog()
    cog._memory_check_window = None

    cog._memory_check_expire("狗與露", "張雨生")

    assert vcmc._load_memory_check_log() == []


# ── play_tts 回傳值 ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_play_tts_returns_true_on_successful_push():
    cog, _ = _make_cog()
    from marvin_voice_core.playback_device import DiscordPlaybackDevice
    device = MagicMock(spec=DiscordPlaybackDevice)

    with patch.object(cog, "_resolve_playback_device", return_value=device):
        result = await cog.play_tts("測試")

    assert result is True


@pytest.mark.asyncio
async def test_play_tts_returns_false_when_pushed_zero():
    cog, _ = _make_cog()
    cog._stream_tts_to_mixer = AsyncMock(return_value=0)
    from marvin_voice_core.playback_device import DiscordPlaybackDevice
    device = MagicMock(spec=DiscordPlaybackDevice)

    with patch.object(cog, "_resolve_playback_device", return_value=device):
        result = await cog.play_tts("測試")

    assert result is False


# ── tail DJ 接點 ───────────────────────────────────────────────────────────────

def _make_music_cog():
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.tts_engine = MagicMock()
    bot.tts_engine.generate_audio = AsyncMock(return_value="/tmp/dj.opus")
    bot.tts_engine.get_estimated_duration = MagicMock(return_value=3.0)
    bot.router = MagicMock()
    bot.router.generate_dynamic_system_msg = AsyncMock(return_value="唉...")
    bot.engine = MagicMock()
    bot.engine.conv_buffer = MagicMock()
    bot.engine.conv_buffer.get_last_n_utterances = MagicMock(return_value=[])
    bot.engine.conv_buffer.get_history = MagicMock(return_value=[])
    bot.engine.post_summon_callback = None
    bot.music_memory = MagicMock()
    bot.music_memory._key = MagicMock(return_value="key")
    bot.music_memory._data = {"songs": {}}
    bot.music_memory.time_slot = MagicMock(return_value="深夜")

    from cogs.music_cog import MusicCog
    return MusicCog(bot)


def _make_vc(members):
    vc = MagicMock()
    vc.play_tts = AsyncMock(return_value=True)
    vc.play_local_file = AsyncMock()
    vc.play_dj_on_tts_layer = AsyncMock(return_value=True)
    vc.maybe_ask_memory_check = AsyncMock(return_value=False)
    vc._tts_protected = False
    vc._intimate_mode = False
    vc.get_online_members = MagicMock(return_value=list(members))
    return vc


@pytest.mark.asyncio
async def test_tail_dj_memory_check_true_skips_narration():
    cog = _make_music_cog()
    vc = _make_vc(["大肚"])  # 只 1 人在線，不算熱
    vc.maybe_ask_memory_check = AsyncMock(return_value=True)
    cog._vc = MagicMock(return_value=vc)

    dj = {"text": "這是原本的長口白", "audio_path": "/tmp/original_dj.opus"}
    result = await cog._maybe_play_dj_interjection(dj)

    assert result is None
    vc.play_dj_on_tts_layer.assert_not_awaited()
    vc.play_tts.assert_not_awaited()


@pytest.mark.asyncio
async def test_tail_dj_memory_check_false_plays_narration_as_usual(tmp_path):
    cog = _make_music_cog()
    vc = _make_vc(["大肚"])
    vc.maybe_ask_memory_check = AsyncMock(return_value=False)
    cog._vc = MagicMock(return_value=vc)
    original = tmp_path / "original_dj.opus"
    original.write_bytes(b"x")

    dj = {"text": "這是原本的長口白", "audio_path": str(original)}
    result = await cog._maybe_play_dj_interjection(dj)

    assert result == "full"
    vc.play_dj_on_tts_layer.assert_awaited_once_with(str(original), text=dj["text"])


@pytest.mark.asyncio
async def test_tail_dj_memory_check_error_still_plays_narration(tmp_path):
    """記憶確認拋例外 → 口白照常播（單一服務失敗不中斷）。"""
    from cogs.music_cog_tail_dj import MusicTailDJMixin
    audio = tmp_path / "dj.mp3"
    audio.write_bytes(b"x")
    vc = MagicMock()
    vc._intimate_mode = False
    vc.maybe_ask_memory_check = AsyncMock(side_effect=RuntimeError("boom"))
    vc.play_dj_on_tts_layer = AsyncMock(return_value=True)
    host = MagicMock()
    host._vc.return_value = vc
    host._dj_channel_is_hot.return_value = False
    result = await MusicTailDJMixin._maybe_play_dj_interjection(
        host, {"text": "口白", "audio_path": str(audio)})
    assert result == "full"
    vc.play_dj_on_tts_layer.assert_awaited_once_with(str(audio), text="口白")



# ── 10/26 第二版：喜好分類（遊戲/食物可問） ──────────────────────────────────

def _quiet_cog(taste):
    cog, _ = _make_cog()
    cog._memory_check_window = None
    cog.last_player_speech_time = time.time() - 300
    cog.get_online_members = MagicMock(return_value=["showay"])
    cog.bot.router.memory.has_player = MagicMock(return_value=True)
    cog.bot.router.memory.get_player_memory = MagicMock(return_value={"taste": taste, "taboos": []})
    cog.play_tts = AsyncMock(return_value=True)
    return cog


@pytest.mark.asyncio
async def test_uncategorized_item_gets_classified_cached_and_asked():
    cog = _quiet_cog({"魔物獵人": {"score": 5.0, "last_update": time.time() - 100}})
    cog._shared_tier_router = MagicMock()
    cog._shared_tier_router.quick = AsyncMock(return_value='{"魔物獵人": "game"}')
    with patch.object(vcmc, "_load_artist_names", return_value=set()):
        assert await cog.maybe_ask_memory_check() is True
    cog._shared_tier_router.quick.assert_awaited_once()
    assert cog._shared_tier_router.quick.await_args.kwargs["caller"] == "memory_check_category"
    assert vcmc._load_categories() == {"魔物獵人": "game"}
    assert cog._memory_check_window["item"] == "魔物獵人"


@pytest.mark.asyncio
async def test_classifier_error_does_not_raise_and_artist_still_asked():
    cog = _quiet_cog({"張雨生": {"score": 5.0, "last_update": time.time() - 100},
                      "魔物獵人": {"score": 5.0, "last_update": time.time() - 9000}})
    cog._shared_tier_router = MagicMock()
    cog._shared_tier_router.quick = AsyncMock(side_effect=RuntimeError("429"))
    with patch.object(vcmc, "_load_artist_names", return_value={"張雨生"}):
        assert await cog.maybe_ask_memory_check() is True
    assert vcmc._load_categories() == {}
    assert cog._memory_check_window["item"] == "張雨生"


@pytest.mark.asyncio
async def test_room_not_ready_never_calls_classifier():
    cog = _quiet_cog({"魔物獵人": {"score": 5.0, "last_update": time.time() - 100}})
    cog.last_player_speech_time = time.time() - 10
    cog._shared_tier_router = MagicMock()
    cog._shared_tier_router.quick = AsyncMock(return_value='{"魔物獵人": "game"}')
    with patch.object(vcmc, "_load_artist_names", return_value=set()):
        assert await cog.maybe_ask_memory_check() is False
    cog._shared_tier_router.quick.assert_not_awaited()


@pytest.mark.asyncio
async def test_all_items_known_never_calls_classifier():
    """歌手或已分類的項目不再送分類（不浪費免費池額度）。"""
    vcmc._save_categories({"魔物獵人": "game"})
    cog = _quiet_cog({"張雨生": {"score": 5.0, "last_update": time.time() - 100},
                      "魔物獵人": {"score": 5.0, "last_update": time.time() - 50}})
    cog._shared_tier_router = MagicMock()
    cog._shared_tier_router.quick = AsyncMock(return_value="{}")
    with patch.object(vcmc, "_load_artist_names", return_value={"張雨生"}):
        assert await cog.maybe_ask_memory_check() is True
    cog._shared_tier_router.quick.assert_not_awaited()
