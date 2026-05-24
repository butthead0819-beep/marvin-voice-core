"""TDD — live bot 用的 discord_voice_engine.DiscordVoiceEngine 必須接 pipeline_timing。

之所以放這支獨立 test：marvin_voice_core/pipeline.py 跟 discord_voice_engine.py
有兩個平行的 process_audio_slice 實作。live bot 走的是後者（前者只在
test_concurrent_load.py 用到）。接點接到前者 → STAGE_TIMING 永遠出不來。

直接用 source-level inspection 而不是動態跑，因為 DiscordVoiceEngine 的
__init__ 拖一堆 dep (LLMPool / NeMo 模型 / Whisper / Swift)，TDD 重點是
hooks 存在不存在，不是模擬整條 pipeline。
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_discord_voice_engine_imports_pipeline_timing():
    """頂部該 import pipeline_timing — 不然 hook call 直接 NameError。"""
    import discord_voice_engine
    assert hasattr(discord_voice_engine, "pipeline_timing"), \
        "discord_voice_engine.py 沒 import pipeline_timing"


def test_process_audio_slice_calls_pipeline_timing_start():
    """process_audio_slice 入口必須 start() — 第一個 async frame 才能讓 ContextVar 傳到 create_task 後代。"""
    import discord_voice_engine
    src = inspect.getsource(discord_voice_engine.DiscordVoiceEngine.process_audio_slice)
    assert "pipeline_timing.start()" in src, \
        "process_audio_slice 內沒 pipeline_timing.start()"


def test_stt_hybrid_marks_stt_start_and_done():
    """_process_stt_hybrid 包住 Swift/Whisper STT call — 前後該有 mark。"""
    import discord_voice_engine
    src = inspect.getsource(discord_voice_engine.DiscordVoiceEngine._process_stt_hybrid)
    assert 'pipeline_timing.mark("stt_start")' in src, \
        "_process_stt_hybrid 內沒 mark('stt_start')"
    assert 'pipeline_timing.mark("stt_done")' in src, \
        "_process_stt_hybrid 內沒 mark('stt_done')"

    # stt_start 必須在 stt_done 之前（source 順序）
    start_idx = src.index('pipeline_timing.mark("stt_start")')
    done_idx = src.index('pipeline_timing.mark("stt_done")')
    assert start_idx < done_idx, "stt_start 該在 stt_done 之前"
