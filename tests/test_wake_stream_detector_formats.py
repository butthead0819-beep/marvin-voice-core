"""WakeStreamDetector 多格式輸入支援 — 16k mono puck 路徑不可影響 Discord 48k stereo 路徑。

Discord 路徑（預設參數）必須完全不變；16k mono 路徑是新增的建構參數。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from wake_stream_detector import _STRIDE_BYTES, _WAKE_PROMPT, _WINDOW_BYTES, WakeStreamDetector


class _FakeLoop:
    def __init__(self, auto_close: bool = True):
        self.created = []
        self.auto_close = auto_close

    def create_task(self, coro):
        self.created.append(coro)
        if self.auto_close:
            coro.close()  # 避免 "coroutine was never awaited" 警告


def test_default_params_match_discord_constants():
    wake = WakeStreamDetector(whisper_model=None, on_wake_callback=None, loop=_FakeLoop())
    # 0.5s × 48000 × 2ch × 2B；跟模組常數一致＝Discord 路徑不變
    assert wake._window_bytes == 96000 == _WINDOW_BYTES
    assert wake._stride_bytes == 48000 == _STRIDE_BYTES


def test_16k_mono_params():
    wake = WakeStreamDetector(
        whisper_model=None, on_wake_callback=None, loop=_FakeLoop(),
        input_rate=16000, input_channels=1,
    )
    assert wake._window_bytes == 16000
    assert wake._stride_bytes == 8000


def test_non_multiple_of_16000_raises():
    with pytest.raises(ValueError):
        WakeStreamDetector(
            whisper_model=None, on_wake_callback=None, loop=_FakeLoop(),
            input_rate=22050, input_channels=1,
        )


def test_16k_mono_push_pcm_creates_task_at_window_size():
    loop = _FakeLoop()
    wake = WakeStreamDetector(
        whisper_model=None, on_wake_callback=None, loop=loop,
        input_rate=16000, input_channels=1,
    )
    wake.on_speech_start(user_id=1, first_audio_time=0.0, pre_roll=b"")

    # window_bytes = 16000（500ms @16k mono，16-bit）
    wake.push_pcm(1, b"\x00\x00" * 7999)
    assert len(loop.created) == 0

    wake.push_pcm(1, b"\x00\x00")  # 補到剛好 16000 bytes
    assert len(loop.created) == 1


class _FakeModel:
    def __init__(self, text: str):
        self.text = text
        self.calls: list[dict] = []

    def transcribe(self, audio, **kw):
        self.calls.append(kw)
        return [SimpleNamespace(text=self.text)], None


@pytest.mark.asyncio
async def test_discord_default_transcribe_kwargs_unchanged():
    """Discord 路徑（預設參數）transcribe 呼叫參數必須一個 byte 都不變，沒有 temperature key。"""
    model = _FakeModel("下一首")
    wake = WakeStreamDetector(
        whisper_model=model, on_wake_callback=None, loop=_FakeLoop(),
    )
    pcm = b"\x00\x00\x00\x00" * 100
    await wake._infer(1, pcm, 0.0)
    assert model.calls == [{
        "beam_size": 1,
        "language": "zh",
        "vad_filter": False,
        "initial_prompt": _WAKE_PROMPT,
    }]


@pytest.mark.asyncio
async def test_pinyin_config_passes_temperature_and_none_prompt_and_skips_hallucination_filter():
    model = _FakeModel("嗨馬文,Hi Marvin")
    called = []

    async def on_wake(user_id, first_audio_time, text):
        called.append((user_id, first_audio_time, text))

    from puck_wake import pinyin_wake_match

    wake = WakeStreamDetector(
        whisper_model=model, on_wake_callback=on_wake, loop=_FakeLoop(),
        input_rate=16000, input_channels=1,
        initial_prompt=None, temperature=0.0,
        wake_matcher=lambda t: pinyin_wake_match(t) is not None,
    )
    wake.on_speech_start(user_id=1, first_audio_time=0.0, pre_roll=b"")
    pcm = b"\x00\x00" * 100
    await wake._infer(1, pcm, 0.0)

    assert model.calls == [{
        "beam_size": 1,
        "language": "zh",
        "vad_filter": False,
        "initial_prompt": None,
        "temperature": 0.0,
    }]
    assert called == [(1, 0.0, "嗨馬文,Hi Marvin")]


@pytest.mark.asyncio
async def test_default_config_hallucination_filter_blocks_prompt_echo():
    model = _FakeModel("嗨馬文,Hi Marvin")
    called = []

    async def on_wake(user_id, first_audio_time, text):
        called.append((user_id, first_audio_time, text))

    wake = WakeStreamDetector(
        whisper_model=model, on_wake_callback=on_wake, loop=_FakeLoop(),
    )
    wake.on_speech_start(user_id=1, first_audio_time=0.0, pre_roll=b"")
    pcm = b"\x00\x00\x00\x00" * 100
    await wake._infer(1, pcm, 0.0)

    assert called == []


def test_window_s_param():
    wake = WakeStreamDetector(
        whisper_model=None, on_wake_callback=None, loop=_FakeLoop(),
        input_rate=16000, input_channels=1, window_s=1.0,
    )
    assert wake._window_bytes == 32000
    assert wake._stride_bytes == 8000


@pytest.mark.asyncio
async def test_rearm_after_real_fire_waits_for_full_stride():
    """觸發一次 wake 後 push_pcm 不建 task；rearm 後要再累積滿一個 stride 才建 task。"""
    loop = _FakeLoop(auto_close=False)
    model = _FakeModel("馬尾")
    from puck_wake import pinyin_wake_match

    async def on_wake(user_id, first_audio_time, text):
        pass

    wake = WakeStreamDetector(
        whisper_model=model, on_wake_callback=on_wake, loop=loop,
        input_rate=16000, input_channels=1,
        wake_matcher=lambda t: pinyin_wake_match(t) is not None,
    )
    wake.on_speech_start(user_id=1, first_audio_time=0.0, pre_roll=b"")

    # 累積到 window_bytes（16000）觸發一次推理，實際執行讓 _fired 真的被設成 True
    wake.push_pcm(1, b"\x00\x00" * 8000)
    assert len(loop.created) == 1
    await loop.created[0]
    assert wake._fired[1] is True
    assert wake._bytes_at_last_infer[1] == 16000

    # fired 期間 push 不建 task，buffer 也不累積
    wake.push_pcm(1, b"\x00\x00" * 8000)
    assert len(loop.created) == 1

    wake.rearm(1)
    assert wake._fired[1] is False
    assert wake._bytes_at_last_infer[1] == 16000  # 跟目前 buffer 長度一致，下一次要再等滿一個 stride

    # 累積不到一個 stride（8000 bytes）→ 不建 task
    wake.push_pcm(1, b"\x00\x00" * 3999)
    assert len(loop.created) == 1

    # 補滿一個 stride → 建 task
    wake.push_pcm(1, b"\x00\x00" * 1)
    assert len(loop.created) == 2
    loop.created[1].close()  # 不需要跑第二次推理，避免 "never awaited" 警告


def test_rearm_without_prior_fire_also_waits_for_full_stride():
    """rearm 不限於 fired 狀態；對帶大 pre_roll 的 buffer 呼叫 rearm，也要吃掉已累積的位元組數，
    不能讓下一次 push 立刻觸發（這正是 rearm 要設定 _bytes_at_last_infer 而非留给 on_speech_start
    的 0 的原因）。"""
    loop = _FakeLoop()
    wake = WakeStreamDetector(
        whisper_model=None, on_wake_callback=None, loop=loop,
        input_rate=16000, input_channels=1,
    )
    wake.on_speech_start(user_id=1, first_audio_time=0.0, pre_roll=b"\x00\x00" * 8000)

    wake.rearm(1)
    assert wake._bytes_at_last_infer[1] == 16000

    wake.push_pcm(1, b"\x00\x00" * 100)
    assert len(loop.created) == 0


def _inflight_scenario(contiguous):
    """觸發一次推理（inflight=True 不釋放）後再推 0.5s，再釋放 inflight 推一個 stride。"""
    loop = _FakeLoop()
    kw = dict(input_rate=16000, input_channels=1)
    if contiguous is not None:
        kw["contiguous"] = contiguous
    wake = WakeStreamDetector(whisper_model=None, on_wake_callback=None, loop=loop, **kw)
    wake.on_speech_start(user_id=1, first_audio_time=0.0, pre_roll=b"")
    wake.push_pcm(1, b"\x01\x00" * 8000)          # 0.5s → 第一次推理，inflight
    assert len(loop.created) == 1
    wake.push_pcm(1, b"\x02\x00" * 8000)          # 推理中又來 0.5s
    return wake, loop


def test_default_drops_pcm_while_inflight():
    """Discord 預設行為鎖住：推理中進來的 PCM 不進 buffer、也不建新 task。"""
    wake, loop = _inflight_scenario(None)
    assert len(wake._buffers[1]) == 16000
    assert len(loop.created) == 1


def test_contiguous_keeps_pcm_while_inflight_without_new_task():
    """contiguous=True：推理中照樣累積（窗口才是連續音訊），但不另建 task；釋放後下一幀就能推。"""
    wake, loop = _inflight_scenario(True)
    assert len(wake._buffers[1]) == 32000
    assert len(loop.created) == 1
    wake._inflight[1] = False
    wake.push_pcm(1, b"\x03\x00" * 2)
    assert len(loop.created) == 1 + 1
    assert len(wake._buffers[1]) == 32004
