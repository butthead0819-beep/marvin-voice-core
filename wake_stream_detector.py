"""
WakeStreamDetector — P3 聲學層實時喚醒詞偵測器。

原理：VAD 確認說話後立即啟動，每 250ms 對最近 500ms Discord PCM
跑一次 Whisper tiny 推理（不占 STT lock，不走 LLM，完全本地）。

最快偵測路徑：
  VAD 確認 ~60ms + pre-roll 400ms → 第一次推理在 100ms 後啟動
  Whisper tiny 推理 ~80ms（Apple Silicon）
  → 端到端 ~640ms（比原始 2000ms 快 3x）

整合點（由 DiscordVoiceEngine 呼叫）：
  on_speech_start(user_id, first_audio_time, pre_roll)
  push_pcm(user_id, pcm_48k_stereo)          ← 每幀 20ms
  on_speech_end(user_id)                      ← VAD 切斷 / flush
  cleanup()                                   ← Sink 清理
"""

import asyncio
import numpy as np
import logging
from utils import check_cleaned_text_for_wake, is_whisper_hallucination

logger = logging.getLogger(__name__)

# Discord 格式：48kHz, stereo, 16-bit
_DISCORD_RATE = 48000
_DOWNSAMPLE = 3  # 48000 / 16000 = 3（整數比，直接抽取）

# 偵測窗口 500ms，每 250ms 新音訊觸發一次推理
_WINDOW_S = 0.5
_STRIDE_S = 0.25
_WINDOW_BYTES = int(_WINDOW_S * _DISCORD_RATE * 2 * 2)   # 2ch × 2B = 192000 bytes
_STRIDE_BYTES = int(_STRIDE_S * _DISCORD_RATE * 2 * 2)   # = 96000 bytes


class WakeStreamDetector:
    """
    聲學層實時喚醒詞偵測器（P3）。

    每 250ms 對最近 500ms Discord PCM 做一次 Whisper tiny 推理，
    偵測到喚醒詞立刻回呼，完全不等 VAD 靜音。
    """

    def __init__(self, whisper_model, on_wake_callback, loop: asyncio.AbstractEventLoop):
        self.model = whisper_model
        # async (user_id: int, first_audio_time: float, text: str) -> None
        self.on_wake_callback = on_wake_callback
        self.loop = loop

        self._buffers: dict[int, bytearray] = {}
        self._first_audio_time: dict[int, float] = {}
        self._bytes_at_last_infer: dict[int, int] = {}
        self._fired: dict[int, bool] = {}
        self._inflight: dict[int, bool] = {}

    # ── Public API ──────────────────────────────────────────────────────────────

    def on_speech_start(self, user_id: int, first_audio_time: float, pre_roll: bytes = b"") -> None:
        """VAD 確認說話時呼叫。pre_roll 帶入 user_buffers 當前內容，同步起始狀態。"""
        self._buffers[user_id] = bytearray(pre_roll)
        self._first_audio_time[user_id] = first_audio_time
        self._bytes_at_last_infer[user_id] = 0
        self._fired[user_id] = False
        self._inflight[user_id] = False

    def push_pcm(self, user_id: int, pcm_48k_stereo: bytes) -> None:
        """每幀 PCM（20ms）進來時呼叫，負責觸發推理窗口。"""
        if (user_id not in self._buffers
                or self._fired.get(user_id)
                or self._inflight.get(user_id)):
            return

        buf = self._buffers[user_id]
        buf.extend(pcm_48k_stereo)
        n = len(buf)

        # 等到緩衝達 window 大小，且新累積了 stride 的量才觸發
        if n >= _WINDOW_BYTES and (n - self._bytes_at_last_infer.get(user_id, 0)) >= _STRIDE_BYTES:
            self._bytes_at_last_infer[user_id] = n
            self._inflight[user_id] = True
            snapshot = bytes(buf[-_WINDOW_BYTES:])
            first_t = self._first_audio_time[user_id]
            self.loop.create_task(self._infer(user_id, snapshot, first_t))

    def on_speech_end(self, user_id: int) -> None:
        """VAD 切斷（靜音 or flush）時呼叫，清除該使用者的推理狀態。"""
        for d in (self._buffers, self._first_audio_time,
                  self._bytes_at_last_infer, self._fired, self._inflight):
            d.pop(user_id, None)

    def cleanup(self) -> None:
        """Sink 完整清理時呼叫。"""
        for uid in list(self._buffers):
            self.on_speech_end(uid)

    # ── Internal ────────────────────────────────────────────────────────────────

    async def _infer(self, user_id: int, pcm_48k_stereo: bytes, first_audio_time: float) -> None:
        try:
            # 48k stereo int16 → 16k mono float32
            # reshape → mean across channels → 每 3 個取 1（降採樣 3:1）→ 正規化
            arr = np.frombuffer(pcm_48k_stereo, dtype=np.int16).reshape(-1, 2)
            mono_16k = arr.mean(axis=1)[::_DOWNSAMPLE].astype(np.float32) / 32768.0

            # faster-whisper.transcribe() 回傳 lazy generator，必須在 thread 內完成
            # iteration，否則 generate_segments() 會在事件迴圈主執行緒跑，阻塞心跳。
            _model = self.model
            def _transcribe_eager():
                segs, _ = _model.transcribe(
                    mono_16k,
                    beam_size=1,
                    language="zh",
                    vad_filter=False,
                    initial_prompt="嗨馬文,馬文,艾馬文,Marvin,Hi Marvin",
                )
                return "".join(s.text for s in segs).strip()

            text = await asyncio.to_thread(_transcribe_eager)

            _wake_prompt = "嗨馬文,馬文,艾馬文,Marvin,Hi Marvin"
            if text and is_whisper_hallucination(text, _wake_prompt):
                logger.warning(f"⚠️ [WakeStream] User_{user_id} 幻覺丟棄: '{text[:60]}'")
                text = ""

            if text and check_cleaned_text_for_wake(text) and not self._fired.get(user_id):
                self._fired[user_id] = True
                logger.info(f"🎯 [WakeStream] User_{user_id} → '{text}'")
                await self.on_wake_callback(user_id, first_audio_time, text)

        except Exception as e:
            logger.warning(f"⚠️ [WakeStream] User_{user_id} infer error: {e}")
        finally:
            if user_id in self._inflight:
                self._inflight[user_id] = False
