"""
LatencyTracker — wake → llm → first_sentence → first_audio 分階段計時。

意圖：埋點實際 production 數據，量化 wake-to-TTS pipeline 各階段耗時。

Stage 切點：
  T0 wake          handle_stt_result is_fast=True 進 query_queue 那刻
  T1 llm_start     _process_queued_query 呼叫 stream_fast_response 之前
  T2 first_sent    sentence_splitter 吐出第一個非 SKIP sentence
  T3 first_audio   play_tts 內 voice_client.play() 之前

兩段 log（讓即時觀察友善）：
  Stage-1 (T0→T2)：wake→llm + llm→sentence
  Stage-2 (T2→T3)：sentence→audio + total

設計刻意 single-slot（last wake wins），不是 per-speaker 字典：
- 量測場景就是單人測試
- 真要 concurrent 量測再升級成 dict
"""
from __future__ import annotations


class LatencyMarks:
    """Single-slot wake-cycle timestamp recorder."""

    def __init__(self) -> None:
        self.speaker: str | None = None
        self.wake_ts: float | None = None
        self.llm_start_ts: float | None = None
        self.first_sentence_ts: float | None = None

    def mark_wake(self, speaker: str, now: float) -> None:
        """Wake 觸發瞬間 — 重置所有 state，開新一輪量測。"""
        self.speaker = speaker
        self.wake_ts = now
        self.llm_start_ts = None
        self.first_sentence_ts = None

    def mark_llm_start(self, now: float) -> None:
        """LLM stream 呼叫前。若無 wake，silently skip（不污染量測）。"""
        if self.wake_ts is not None:
            self.llm_start_ts = now

    def mark_first_sentence(self, now: float) -> dict | None:
        """第一個 sentence 到手。回傳 stage-1 latency dict，None=量測未完成。"""
        if self.wake_ts is None or self.llm_start_ts is None:
            return None
        self.first_sentence_ts = now
        return {
            "speaker": self.speaker,
            "wake_to_llm_ms": (self.llm_start_ts - self.wake_ts) * 1000,
            "llm_to_sentence_ms": (now - self.llm_start_ts) * 1000,
        }

    def mark_first_audio_and_consume(self, now: float) -> dict | None:
        """vc.play() 之前。回傳 stage-2 latency dict 並清空 state。"""
        if self.first_sentence_ts is None or self.wake_ts is None:
            return None
        result = {
            "speaker": self.speaker,
            "sentence_to_audio_ms": (now - self.first_sentence_ts) * 1000,
            "total_wake_to_audio_ms": (now - self.wake_ts) * 1000,
        }
        self._reset()
        return result

    def _reset(self) -> None:
        self.speaker = None
        self.wake_ts = None
        self.llm_start_ts = None
        self.first_sentence_ts = None
