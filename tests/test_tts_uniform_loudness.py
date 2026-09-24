"""所有 Marvin TTS 統一響度（audio_mixing.TTS_LOUDNESS_AF）驗收測試。

涵蓋：
1. 真實 ffmpeg ebur128 量測（不套/有套濾鏡的響度差距）
2. _stream_tts_to_mixer ffmpeg 指令帶 -af
3. play_dj_on_tts_layer 預設（講話）走濾鏡、peak=<float>（SFX）走 peak_normalize
4. _ffmpeg_to_f32(af=...) 參數轉譯
5. ack 路徑（cogs/voice_controller.py）
6. _delayed_player_greeting 全程不再調整 _tts_gain

規則：只 import 被測程式碼，濾鏡字串一律從 audio_mixing.TTS_LOUDNESS_AF 讀，不手打複製。
"""
from __future__ import annotations

import re
import shutil
import subprocess
import wave
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

import audio_mixing

FFMPEG = shutil.which("ffmpeg")


# ── (1) 真實響度測試（ffmpeg ebur128） ─────────────────────────────────────────

def _synth_speech_like(seconds: float = 3.0, sr: int = 48000, peak: float = 0.5) -> np.ndarray:
    """合成一段「像講話」的訊號：200/400/800Hz 疊加 + 4Hz 音節包絡 + 0.3s 開關 gate。"""
    t = np.arange(int(seconds * sr), dtype=np.float64) / sr
    tone = (
        np.sin(2 * np.pi * 200 * t)
        + np.sin(2 * np.pi * 400 * t)
        + np.sin(2 * np.pi * 800 * t)
    )
    envelope = 0.5 * (1 + np.sin(2 * np.pi * 4 * t))
    # 每 0.3 秒開關一次的 gate（講話的斷續感）
    gate = (np.floor(t / 0.3).astype(np.int64) % 2 == 0).astype(np.float64)
    sig = tone * envelope * gate
    m = float(np.max(np.abs(sig)))
    if m > 0:
        sig = sig * (peak / m)
    return sig.astype(np.float64)


def _write_wav(path, sig: np.ndarray, sr: int = 48000):
    pcm = np.clip(sig * 32767.0, -32768, 32767).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())


def _run_ebur128(wav_path, af: str | None) -> dict:
    """跑 ffmpeg ebur128，回傳 {'I': float LUFS, 'Peak': float dBFS}（解析 Summary 區塊）。"""
    filt = f"{af},ebur128=peak=true" if af else "ebur128=peak=true"
    proc = subprocess.run(
        [FFMPEG, "-nostdin", "-i", str(wav_path), "-af", filt, "-f", "null", "-"],
        capture_output=True, text=True,
    )
    stderr = proc.stderr
    idx = stderr.rfind("Summary:")
    assert idx != -1, f"ffmpeg stderr 沒有 Summary 區塊：{stderr[-2000:]}"
    summary = stderr[idx:]
    i_match = re.search(r"\bI:\s*(-?\d+\.?\d*)\s*LUFS", summary)
    peak_match = re.search(r"Peak:\s*(-?\d+\.?\d*)\s*dBFS", summary)
    assert i_match and peak_match, f"解析不到 I/Peak：{summary}"
    return {"I": float(i_match.group(1)), "Peak": float(peak_match.group(1))}


@pytest.mark.skipif(FFMPEG is None, reason="需要 ffmpeg")
def test_tts_loudness_af_normalizes_quiet_and_loud_to_uniform_lufs(tmp_path):
    """套 TTS_LOUDNESS_AF 後，小聲（peak 0.1）與大聲（peak 0.6）版本響度應被拉平。"""
    quiet_wav = tmp_path / "quiet.wav"
    loud_wav = tmp_path / "loud.wav"
    _write_wav(quiet_wav, _synth_speech_like(peak=0.1))
    _write_wav(loud_wav, _synth_speech_like(peak=0.6))

    quiet_filtered = _run_ebur128(quiet_wav, audio_mixing.TTS_LOUDNESS_AF)
    loud_filtered = _run_ebur128(loud_wav, audio_mixing.TTS_LOUDNESS_AF)

    diff = abs(quiet_filtered["I"] - loud_filtered["I"])
    assert diff <= 3.0, (
        f"套濾鏡後小聲/大聲響度差距應 ≤3dB，實測 quiet I={quiet_filtered['I']} "
        f"loud I={loud_filtered['I']} diff={diff}"
    )
    for label, res in (("quiet", quiet_filtered), ("loud", loud_filtered)):
        assert -18.0 <= res["I"] <= -10.0, f"{label} 套濾鏡後 I={res['I']} 超出 [-18,-10] LUFS"
        assert res["Peak"] <= -1.0, f"{label} 套濾鏡後 Peak={res['Peak']} 超出 -1.0 dBFS 上限"

    # 反證：同一對 wav 不套濾鏡，響度差距應該遠大於套濾鏡後（證明濾鏡真的有拉平）
    quiet_raw = _run_ebur128(quiet_wav, None)
    loud_raw = _run_ebur128(loud_wav, None)
    raw_diff = abs(quiet_raw["I"] - loud_raw["I"])
    assert raw_diff > 10.0, (
        f"不套濾鏡時小聲/大聲響度差距應 >10dB（證明濾鏡有效），實測 diff={raw_diff}"
    )


# ── (2) _stream_tts_to_mixer 帶 -af ────────────────────────────────────────────

def _make_cog_real_stream_tts():
    """VoiceController *不* mock _stream_tts_to_mixer（用來測 ffmpeg 指令組裝）。

    對齊 tests/test_playback_tts_path.py 的同名建構器。
    """
    bot = MagicMock()
    bot.guilds = []
    bot.cogs.get.return_value = None
    bot.tts_engine = MagicMock()

    with patch("discord_voice_engine.faster_whisper", None, create=True):
        from discord_voice_engine import DiscordVoiceEngine
        engine = DiscordVoiceEngine(bot)
    bot.engine = engine

    with patch("discord.ext.tasks.loop", lambda *a, **kw: lambda f: f), \
         patch("cogs.voice_controller.DepartureStats", MagicMock), \
         patch("cogs.voice_controller.ConsentManager", MagicMock):
        from cogs.voice_controller import VoiceController
        cog = VoiceController(bot)

    cog._tts_interrupted = False
    cog._mixer = MagicMock()
    cog._mixer.push_tts = MagicMock()
    cog._mixer.push_tts2 = MagicMock()
    return cog


def _fake_stream_audio_empty():
    """空 async generator 讓 _feed 立即完成。"""
    async def _gen(*args, **kwargs):
        return
        yield  # noqa: unreachable — makes this an async generator function
    return MagicMock(side_effect=_gen)


def _fake_ffmpeg_proc():
    """Fake ffmpeg proc：_drain 遇到 IncompleteReadError(b'', 0) 立即退出。"""
    import asyncio
    proc = MagicMock()
    proc.stdin = MagicMock()
    proc.stdout = MagicMock()
    proc.stdout.readexactly = AsyncMock(
        side_effect=asyncio.IncompleteReadError(b"", 0)
    )
    return proc


@pytest.mark.asyncio
async def test_stream_tts_to_mixer_ffmpeg_cmd_includes_af():
    """_stream_tts_to_mixer 的 ffmpeg 指令要帶 -af <TTS_LOUDNESS_AF>，且緊接在 pipe:0 之後。"""
    cog = _make_cog_real_stream_tts()
    stream_audio_mock = _fake_stream_audio_empty()
    cog.bot.tts_engine.stream_audio = stream_audio_mock

    create_subprocess_mock = AsyncMock(return_value=_fake_ffmpeg_proc())
    with patch("asyncio.create_subprocess_exec", create_subprocess_mock):
        await cog._stream_tts_to_mixer(
            "測試", force_macos=False, emotion_tag="neutral", voice=None
        )

    create_subprocess_mock.assert_called_once()
    args = create_subprocess_mock.call_args.args
    assert "pipe:0" in args
    idx = args.index("pipe:0")
    assert args[idx + 1] == "-af", f"ffmpeg 指令參數：{args}"
    assert args[idx + 2] == audio_mixing.TTS_LOUDNESS_AF, f"ffmpeg 指令參數：{args}"


# ── (3) play_dj_on_tts_layer：預設走濾鏡 / peak=<float> 走 peak_normalize ──────

def _make_light_dj_cog():
    """輕量 cog：只裝 play_dj_on_tts_layer 需要的接縫（_ffmpeg_to_f32 / _ensure_mixer_playing
    / _resolve_playback_device / _mixer），方法照原 class 綁定，避免整套 VoiceController 建構。
    """
    from cogs.voice_controller_playback import PlaybackMixin

    class _Cog:
        play_dj_on_tts_layer = PlaybackMixin.play_dj_on_tts_layer

    cog = _Cog()
    cog._ffmpeg_to_f32 = AsyncMock(return_value=np.array([0.1, 0.2], dtype=np.float32))
    cog._ensure_mixer_playing = MagicMock()
    cog._resolve_playback_device = MagicMock(return_value=MagicMock())
    cog._mixer = MagicMock()
    cog._mixer.push_tts = MagicMock(return_value=True)
    return cog


@pytest.mark.asyncio
async def test_play_dj_on_tts_layer_default_uses_loudness_filter_not_peak_normalize():
    cog = _make_light_dj_cog()

    with patch("os.path.exists", return_value=True), \
         patch("audio_mixing.peak_normalize_f32") as mock_peak_norm:
        result = await cog.play_dj_on_tts_layer("fake.mp3")

    assert result is True
    cog._ffmpeg_to_f32.assert_awaited_once_with(
        input_path="fake.mp3", af=audio_mixing.TTS_LOUDNESS_AF
    )
    mock_peak_norm.assert_not_called()


@pytest.mark.asyncio
async def test_play_dj_on_tts_layer_with_peak_uses_peak_normalize_not_af():
    cog = _make_light_dj_cog()

    with patch("os.path.exists", return_value=True), \
         patch("audio_mixing.peak_normalize_f32") as mock_peak_norm:
        mock_peak_norm.return_value = np.array([0.1, 0.2], dtype=np.float32)
        result = await cog.play_dj_on_tts_layer("fake.mp3", peak=0.1)

    assert result is True
    cog._ffmpeg_to_f32.assert_awaited_once_with(input_path="fake.mp3")
    call_kwargs = cog._ffmpeg_to_f32.await_args.kwargs
    assert "af" not in call_kwargs
    mock_peak_norm.assert_called_once()
    assert mock_peak_norm.call_args.kwargs.get("target_peak") == 0.1


# ── (4) _ffmpeg_to_f32(af=...) 參數轉譯 ────────────────────────────────────────

def _make_light_ffmpeg_cog():
    from cogs.voice_controller_playback import PlaybackMixin

    class _Cog:
        _ffmpeg_to_f32 = PlaybackMixin._ffmpeg_to_f32

    return _Cog()


@pytest.mark.asyncio
async def test_ffmpeg_to_f32_with_af_inserts_af_after_input():
    cog = _make_light_ffmpeg_cog()
    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(b"", None))
    create_subprocess_mock = AsyncMock(return_value=proc)

    with patch("asyncio.create_subprocess_exec", create_subprocess_mock):
        out = await cog._ffmpeg_to_f32(input_path="/tmp/x.mp3", af="some_filter_string")

    assert out is None  # 空輸出 → None（不影響本測試重點：參數組裝）
    args = create_subprocess_mock.call_args.args
    assert "-i" in args
    idx = args.index("-i")
    assert args[idx + 1] == "/tmp/x.mp3"
    assert args[idx + 2] == "-af"
    assert args[idx + 3] == "some_filter_string"


@pytest.mark.asyncio
async def test_ffmpeg_to_f32_without_af_omits_af_flag():
    cog = _make_light_ffmpeg_cog()
    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(b"", None))
    create_subprocess_mock = AsyncMock(return_value=proc)

    with patch("asyncio.create_subprocess_exec", create_subprocess_mock):
        await cog._ffmpeg_to_f32(input_path="/tmp/x.mp3")

    args = create_subprocess_mock.call_args.args
    assert "-af" not in args


# ── (5) ack 路徑 ────────────────────────────────────────────────────────────
#
# ack 程式碼在 cogs/voice_controller.py 的 _play_ack() 內，是 wake/music/
# nemoclaw/status/filler 五種 category 共用的一段收尾程式碼，沒辦法單獨切開呼叫。
# 但 tests/test_play_ack_unified.py 已有現成的 _make_cog() + _idle_vc() fixture
# 可以完整跑一次 _play_ack("wake", ...)，成本不高，故不用「跳過」，直接沿用該
# 檔案的建構模式走一次真實路徑，驗證 _ffmpeg_to_f32 呼叫參數與
# audio_mixing.peak_normalize_f32 有沒有被呼叫。

def _make_ack_cog():
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.tts_engine = MagicMock()
    bot.tts_engine.prewarm = AsyncMock()
    bot.router = MagicMock()
    bot.router._llm_bus = None

    with patch("discord_voice_engine.faster_whisper", None, create=True):
        from discord_voice_engine import DiscordVoiceEngine
        engine = DiscordVoiceEngine(bot)
    bot.engine = engine

    with patch("discord.ext.tasks.loop", lambda *a, **kw: lambda f: f), \
         patch("cogs.voice_controller.DepartureStats", MagicMock), \
         patch("cogs.voice_controller.ConsentManager", MagicMock):
        from cogs.voice_controller import VoiceController
        cog = VoiceController(bot)
    cog._speaker_lang = {}
    cog._ffmpeg_to_f32 = AsyncMock(return_value=np.full(100, 0.1, dtype=np.float32))
    cog._mixer = MagicMock()
    cog._ensure_mixer_playing = MagicMock()
    return cog


def _idle_vc():
    vc = MagicMock()
    vc.is_connected.return_value = True
    vc.is_playing.return_value = False

    def _play(src, after=None):
        if after:
            after(None)
    vc.play = MagicMock(side_effect=_play)
    return vc


@pytest.mark.asyncio
async def test_play_ack_uses_loudness_filter_not_peak_normalize(tmp_path):
    cog = _make_ack_cog()
    vc = _idle_vc()
    cog.voice_client = vc
    f = tmp_path / "ack_1.mp3"
    f.write_bytes(b"x")

    with patch("glob.glob", return_value=[str(f)]), \
         patch("discord.FFmpegPCMAudio", return_value=MagicMock()), \
         patch("audio_mixing.peak_normalize_f32") as mock_peak_norm:
        await cog._play_ack("wake", speaker="阿狗")

    cog._ffmpeg_to_f32.assert_awaited_once_with(input_path=str(f), af=audio_mixing.TTS_LOUDNESS_AF)
    mock_peak_norm.assert_not_called()
    assert cog._mixer.push_tts.called


# ── (6) _delayed_player_greeting 音量不再被動 ──────────────────────────────────

@pytest.mark.asyncio
async def test_delayed_player_greeting_never_touches_tts_gain():
    """招呼期間與招呼後 _tts_gain 全程維持原值（不再有「拉到 0.5 再還原」的邏輯）。

    對齊 tests/test_delayed_player_greeting.py 的 dummy_vc_setup fixture pattern。
    """
    from cogs.voice_controller import VoiceController

    cog = MagicMock(spec=VoiceController)
    cog.bot = MagicMock()
    cog.bot.user = MagicMock()
    cog.bot.user.id = 99999
    cog.consent = MagicMock()
    cog.consent.has_seen_notice.return_value = True
    cog._nudges = MagicMock()
    cog.greeting_cooldown = {}
    cog.stream_mode = False
    cog.stt_logger = MagicMock()
    cog.departure_stats = MagicMock()
    cog.departure_stats.record_departure = AsyncMock()
    cog.bot.router = MagicMock()
    cog.bot.router.generate_player_greeting = AsyncMock(return_value="測試點名台詞")
    cog._maybe_speak_join_callback = AsyncMock(return_value=False)
    cog._send_mood_sticker = AsyncMock()
    cog.handle_dismiss = AsyncMock()
    cog.active_text_channel = MagicMock()
    cog.active_text_channel.send = AsyncMock()

    cog._mixer = MagicMock()
    cog._mixer._tts_gain = 0.2

    channel = MagicMock()
    member = MagicMock()
    member.id = 12345
    member.display_name = "測試玩家"
    member.guild = MagicMock()
    channel.members = [member]

    voice_client = MagicMock()
    voice_client.guild = member.guild
    voice_client.channel = channel
    voice_client.is_connected.return_value = True
    cog.bot.voice_clients = [voice_client]

    gains_during_speak = []

    async def fake_speak(*args, **kwargs):
        gains_during_speak.append(cog._mixer._tts_gain)

    cog.speak = AsyncMock(side_effect=fake_speak)
    cog._delayed_player_greeting = VoiceController._delayed_player_greeting.__get__(cog)

    with patch("asyncio.sleep", AsyncMock()):
        await cog._delayed_player_greeting(member, channel, delay_sec=0.0)

    assert gains_during_speak == [0.2], "speak 當下 _tts_gain 應維持原值 0.2"
    assert cog._mixer._tts_gain == 0.2, "招呼結束後 _tts_gain 應維持原值 0.2"
