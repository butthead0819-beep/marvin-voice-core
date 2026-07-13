from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)

# persistent 泵 idle 時寫的預設靜音幀（20ms @ 48k stereo int16 = 3840 bytes）；
# 首個真實幀進來後改用其大小（見 _pump）。
_DEFAULT_SILENCE_FRAME = b"\x00" * 3840


class DiscordPlaybackDevice:
    """Thin wrapper around a discord VoiceClient that satisfies PlaybackDevice.

    Every method is a 1:1 delegation — no extra logic, no state, no timing
    changes.  stop() delegates to voice_client.stop_playing() (a project-local
    method) to maintain byte-equivalence with the original play_music path.
    """

    def __init__(self, voice_client) -> None:
        self._vc = voice_client

    def play(self, source, *, after=None) -> None:
        self._vc.play(source, after=after)

    def is_playing(self) -> bool:
        return self._vc.is_playing()

    def stop(self) -> None:
        # byte-equivalence: play_music originally called vc.stop_playing(),
        # not vc.stop() — delegate to the same underlying method.
        self._vc.stop_playing()

    def is_connected(self) -> bool:
        return self._vc.is_connected()

    def arm_mixer(self, source) -> None:
        """啟動持續性 mixer 播放：bitrate 公式對齊頻道允許上限 + vc.play(application='audio')。"""
        ch_bps = getattr(getattr(self._vc, "channel", None), "bitrate", None)
        kbps = 128
        if isinstance(ch_bps, int) and ch_bps > 0:
            kbps = max(16, min(512, ch_bps // 1000))
        self._vc.play(source, application="audio", bitrate=kbps)
        print(f"[Plan12_Bitrate] 頻道={ch_bps} bps → opus 編碼設 {kbps} kbps（application=audio）", flush=True)
        print("[Plan12_Mixer] adapter armed（mixer 開始驅動 vc 輸出）", flush=True)


# ── Local speaker output ──────────────────────────────────────────────────────

class LocalSpeakerDevice:
    """Local speaker output satisfying PlaybackDevice.

    Mirrors the read()/after contract of discord voice_client.play(): a pump
    thread calls source.read() in a tight loop (sleeping frame_duration seconds
    between frames), writes each non-empty frame to the output, and calls
    after(None) exactly once when the source is naturally exhausted.

    Thread safety of ``after``:
        ``after`` is called from the pump thread on natural exhaustion only.
        Callers that need the callback dispatched to an asyncio event loop must
        wrap it: ``loop.call_soon_threadsafe(after, err)``.

    Output injection:
        Pass ``output`` for tests or custom sinks.  Accepts any object with a
        ``write(frame: bytes)`` method (and optional ``close()``), or a plain
        callable ``output(frame: bytes)``.  When omitted, lazy-imports
        sounddevice and opens an OutputStream — direct module import never
        requires sounddevice to be installed.
    """

    def __init__(
        self,
        *,
        output=None,
        frame_duration: float = 0.02,
        persistent: bool = True,
    ) -> None:
        # persistent=True（預設，Pi 常駐喇叭）：arm_mixer 的泵遇 mixer idle b"" 不退出、
        # 寫靜音持位置等下一段（避免 re-arm race 丟 TTS）。
        # persistent=False（瀏覽器衛星 PTT）：source 耗盡即退出，idle 不空轉→CPU 0。
        self._output = output          # None ⇒ lazy sounddevice; else injected
        self._frame_duration = frame_duration
        self._persistent = persistent
        self._thread: threading.Thread | None = None
        self._stop_flag: threading.Event = threading.Event()
        self._playing: bool = False

    # ── PlaybackDevice Protocol ──────────────────────────────────────────────

    def play(self, source, *, after=None, persistent: bool = False) -> None:
        if self._playing:
            logger.warning("[Core_LocalSpk] play() 忽略：泵已在執行中")
            return
        output = self._resolve_output()
        self._stop_flag.clear()
        self._playing = True
        self._thread = threading.Thread(
            target=self._pump,
            args=(source, after, output, persistent),
            daemon=True,
            name="LocalSpk-pump",
        )
        self._thread.start()
        logger.debug("[Core_LocalSpk] 泵已啟動 (persistent=%s)", persistent)

    def is_playing(self) -> bool:
        return self._playing

    def stop(self) -> None:
        self._stop_flag.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        self._playing = False
        logger.debug("[Core_LocalSpk] 泵已中止")

    def is_connected(self) -> bool:
        return True

    def arm_mixer(self, source) -> None:
        """委派 play()（persistent）：idempotent 由既有 _playing 守門保證。

        mixer 是 on-demand 來源，閒置超過 grace 會回 b""（給 Discord 的「停送」訊號）。
        本機喇叭是常駐輸出，那個 b"" 不該殺泵→persistent=True 讓泵持位置寫靜音、等下一段
        TTS/music，只有 stop() 才真中止。否則泵一退出、再 arm 又讀到 stale idle b"" 立刻
        退出→push 進來的 TTS 沒人排→無聲且 tts_load 累積（本機沉默 bug 根因）。

        瀏覽器衛星（PTT）以 persistent=False 建構→泵播完即停、idle 不空轉（省 CPU）。"""
        self.play(source, persistent=self._persistent)

    # ── Internal ─────────────────────────────────────────────────────────────

    def _resolve_output(self):
        if self._output is not None:
            return self._output
        try:
            import sounddevice as sd  # noqa: PLC0415
        except ImportError as exc:
            raise RuntimeError(
                "[Core_LocalSpk] sounddevice 未安裝；"
                "請 pip install sounddevice 或以 output= 注入測試輸出。"
            ) from exc
        stream = sd.OutputStream(samplerate=48000, channels=2, dtype="int16")
        stream.start()
        return _SounddeviceOutputAdapter(stream)

    def _pump(self, source, after, output, persistent: bool = False) -> None:
        exhausted = False
        start = time.perf_counter()
        frames_played = 0
        silence = _DEFAULT_SILENCE_FRAME  # persistent idle 寫的靜音幀（跟到首個真實幀大小）
        try:
            while not self._stop_flag.is_set():
                frame = source.read()
                if not frame:
                    if not persistent:
                        exhausted = True
                        break
                    # 常駐本機喇叭：mixer on-demand idle 回 b"" ≠ 耗盡。寫靜音持位置、
                    # 等下一段 TTS/music，只有 stop() 才中止（避免 re-arm race 丟 TTS）。
                    frame = silence
                elif len(frame) != len(silence):
                    silence = b"\x00" * len(frame)
                _write_frame(output, frame)
                frames_played += 1
                if self._frame_duration > 0:
                    deadline = start + frames_played * self._frame_duration
                    remaining = deadline - time.perf_counter()
                    if remaining > 0:
                        time.sleep(remaining)
        finally:
            _close_output(output)
            if exhausted and after is not None:
                after(None)
            self._playing = False
            logger.debug("[Core_LocalSpk] 泵執行緒結束 (exhausted=%s persistent=%s)", exhausted, persistent)


class _SounddeviceOutputAdapter:
    """Wraps a sounddevice OutputStream to accept raw PCM bytes."""

    def __init__(self, stream) -> None:
        self._stream = stream

    def write(self, frame: bytes) -> None:
        import numpy as np  # noqa: PLC0415
        data = np.frombuffer(frame, dtype="int16").reshape(-1, 2)
        self._stream.write(data)

    def close(self) -> None:
        try:
            self._stream.stop()
            self._stream.close()
        except Exception as exc:
            logger.warning("[Core_LocalSpk] 關閉 sounddevice stream 失敗: %s", exc)


def _write_frame(output, frame: bytes) -> None:
    if callable(output) and not hasattr(output, "write"):
        output(frame)
    else:
        output.write(frame)


def _close_output(output) -> None:
    if hasattr(output, "close"):
        try:
            output.close()
        except Exception as exc:
            logger.warning("[Core_LocalSpk] 關閉 output 失敗: %s", exc)
