"""marvin_talk.py — 回合制語音對談（turn-based），現有 STT / cleaner / IntentBus 全繞過。

`/marvin_talk` 開啟後：
  - 觸發者 90 秒獨佔語音頻道（音樂暫停、其他人語音丟棄）
  - 每個語音回合：VAD 切出的 PCM → 16k mono WAV → Gemini Flash-Lite
    （帶馬文人格 + 最近數輪對話文字）→ 文字回覆 → 現有 TTS 播出
  - 付費鐵則（feedback_paid_calls_must_record）：每回合 guard.allow() 估價守門
    + guard.record() 記帳，比照 intent_agents/audio_rescue_agent.py 的 pattern

為什麼不用 Gemini Live（見 2026-08-29 對話）：Live 是端到端音訊串流，計費按
全程雙向音訊、且原生音訊輸出 token 單價是文字的十幾倍。回合制只在使用者實際
講話那幾十秒算 audio-in、輸出是文字、TTS 走 edge-tts，成本低 1~2 個數量級，
且重用現有 VAD / mixer / TTS，新增程式量小。代價：回合延遲 ~2~4s、不能打斷、
TTS 平板無情感。真的不夠用再補 Live（兩者觸發入口 / 記帳 / 獨佔 guard 可共用）。
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import time
import wave
from typing import Any, Awaitable, Callable

from llm_paid import PaidUsageGuard, estimate_cost

logger = logging.getLogger("marvin_talk")

# ── 會話上限 ───────────────────────────────────────────────────────────────────
HARD_CAP_S = 90.0          # 單場硬上限（使用者訂）
SILENCE_TIMEOUT_S = 25.0   # 這麼久沒有新回合 → 自動收
MAX_HISTORY_TURNS = 6      # 送進 prompt 的最近對話輪數
GEMINI_TIMEOUT_S = 8.0

# 付費守門：比照 audio_rescue，偶發低頻、單次成本極低，放寬到跟 GCP spending cap 同量級
_DAILY_CAP_USD = 2.0
_MONTHLY_CAP_USD = 10.0

# 粗估：Gemini 音訊輸入約 32 token/秒；人格 prompt + 歷史約 900 token；輸出短。
_PCM16_BYTES_PER_SECOND = 16000 * 2
_AUDIO_TOKENS_PER_SECOND = 32
_FIXED_PROMPT_TOKENS = 900
_ESTIMATED_OUTPUT_TOKENS = 200

_VOICE_SUFFIX = (
    "\n\n【語音對話補充規則】這是即時雙向語音通話（不是文字訊息），回覆要用自然口語、"
    "正常講話的長度和節奏，不必條列、不必湊字數上限，講完一個念頭就停。厭世是底色不是"
    "表演——用平淡的語氣講出喪氣的話，比誇張哀嚎更像馬文。"
)

_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "heard": {"type": "string", "description": "使用者這句話的逐字內容"},
        "reply": {"type": "string", "description": "馬文的口語回覆"},
    },
    "required": ["heard", "reply"],
}

# 使用者說出這些詞 → 主動結束會話（對 Gemini 回傳的 heard 做子字串比對，零額外 LLM）
_EXIT_PHRASES = ("掰掰馬文", "再見馬文", "結束對話", "結束聊天", "不聊了", "先這樣")

PlayTTS = Callable[[str], Awaitable[Any]]
SendText = Callable[[str], Awaitable[Any]]


def _pcm48k_stereo_to_wav16k_mono(pcm48k_stereo: bytes) -> bytes:
    """48kHz stereo int16 PCM → 16kHz mono 16-bit WAV bytes（Gemini audio 輸入格式）。"""
    from marvin_voice_core.audio_utils import pcm48k_stereo_to_16k_mono

    mono_f32 = pcm48k_stereo_to_16k_mono(pcm48k_stereo)  # float32 [-1, 1]
    pcm16 = (mono_f32 * 32767.0).clip(-32768, 32767).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(pcm16)
    return buf.getvalue()


class TalkSession:
    """單一使用者的回合制對談會話。狀態機 + 每回合 Gemini 呼叫。"""

    def __init__(
        self,
        *,
        owner_id: int,
        owner_name: str,
        google_client: Any,
        play_tts: PlayTTS,
        persona_provider: Callable[[], str],
        paid_guard: PaidUsageGuard,
        model: str,
        clock: Callable[[], float] = time.time,
        on_exit_phrase: Callable[[], Awaitable[Any]] | None = None,
    ):
        self.owner_id = owner_id
        self.owner_name = owner_name
        self._client = google_client
        self._play_tts = play_tts
        self._persona_provider = persona_provider
        self._guard = paid_guard
        self._model = model
        self._clock = clock
        self._on_exit_phrase = on_exit_phrase

        self.active = True
        self.started_at = clock()
        self._last_activity = self.started_at
        self.history: list[dict] = []  # [{"heard": str, "reply": str}]
        self.turn_count = 0
        self._turn_lock = asyncio.Lock()

    # ── 生命週期 ──────────────────────────────────────────────────────────────
    def deadline_reason(self) -> str | None:
        now = self._clock()
        if now - self.started_at >= HARD_CAP_S:
            return "時間到"
        if now - self._last_activity >= SILENCE_TIMEOUT_S:
            return "太久沒聲音"
        return None

    def close(self) -> None:
        self.active = False

    # ── 每回合 ───────────────────────────────────────────────────────────────
    async def handle_turn(self, pcm48k_stereo: bytes) -> None:
        if not self.active or not pcm48k_stereo:
            return
        # 上一回合還在跑（Gemini 或 TTS）→ 丟棄本次，避免堆積
        if self._turn_lock.locked():
            logger.info("[MarvinTalk] 上一回合處理中，丟棄本次語音")
            return
        async with self._turn_lock:
            if not self.active:
                return
            self._last_activity = self._clock()

            try:
                wav_bytes = _pcm48k_stereo_to_wav16k_mono(pcm48k_stereo)
            except Exception as exc:
                logger.warning(f"[MarvinTalk] 音訊轉檔失敗，跳過本回合：{exc}")
                return

            secs = max(1, len(wav_bytes) // _PCM16_BYTES_PER_SECOND)
            est_in = _FIXED_PROMPT_TOKENS + secs * _AUDIO_TOKENS_PER_SECOND
            if not self._guard.allow(estimate_cost(self._model, est_in, _ESTIMATED_OUTPUT_TOKENS)):
                logger.warning("[MarvinTalk] 超 daily/monthly paid cap，結束會話")
                self.close()
                await self._safe_tts("我這個月的對話額度用完了，改天吧。")
                return

            result = await self._call_gemini(wav_bytes)
            if result is None:
                await self._safe_tts("抱歉，我剛剛恍神了，你再說一次。")
                return

            heard, reply = result
            self.turn_count += 1
            self.history.append({"heard": heard, "reply": reply})
            del self.history[:-MAX_HISTORY_TURNS]
            self._last_activity = self._clock()

            await self._safe_tts(reply)

            if heard and any(p in heard for p in _EXIT_PHRASES):
                logger.info(f"[MarvinTalk] 聽到結束語「{heard}」→ 收會話")
                self.close()
                if self._on_exit_phrase is not None:
                    await self._on_exit_phrase()

    async def _call_gemini(self, wav_bytes: bytes) -> tuple[str, str] | None:
        from google.genai import types

        system = self._persona_provider() + _VOICE_SUFFIX
        parts: list[Any] = []
        for turn in self.history[-MAX_HISTORY_TURNS:]:
            parts.append(types.Part.from_text(text=f"（我剛說）{turn['heard']}"))
            parts.append(types.Part.from_text(text=f"（你回）{turn['reply']}"))
        parts.append(types.Part.from_text(text="（以下是我這句話的音訊，聽完用馬文的口吻回我）"))
        parts.append(types.Part.from_bytes(data=wav_bytes, mime_type="audio/wav"))

        try:
            response = await asyncio.wait_for(
                self._client.aio.models.generate_content(
                    model=self._model,
                    contents=parts,
                    config=types.GenerateContentConfig(
                        system_instruction=system,
                        temperature=0.8,
                        response_mime_type="application/json",
                        response_schema=_RESPONSE_SCHEMA,
                    ),
                ),
                timeout=GEMINI_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            logger.warning("[MarvinTalk] Gemini 逾時")
            return None
        except Exception as exc:
            logger.warning(f"[MarvinTalk] Gemini 呼叫失敗：{exc}")
            return None

        usage = getattr(response, "usage_metadata", None)
        in_tokens = int(getattr(usage, "prompt_token_count", 0) or 0)
        out_tokens = int(getattr(usage, "candidates_token_count", 0) or _ESTIMATED_OUTPUT_TOKENS)
        self._guard.record(
            caller="marvin_talk", model=self._model, tokens=in_tokens + out_tokens,
            est_usd=estimate_cost(self._model, in_tokens or _FIXED_PROMPT_TOKENS, out_tokens),
            in_tokens=in_tokens, out_tokens=out_tokens,
        )

        raw = (getattr(response, "text", None) or "").strip()
        if not raw:
            return None
        try:
            data = json.loads(raw)
            reply = str(data.get("reply", "")).strip()
            heard = str(data.get("heard", "")).strip()
        except (json.JSONDecodeError, ValueError, AttributeError):
            reply, heard = raw, ""
        if not reply:
            return None
        return heard, reply

    async def _safe_tts(self, text: str) -> None:
        try:
            await self._play_tts(text)
        except Exception as exc:
            logger.warning(f"[MarvinTalk] play_tts 失敗：{exc}")


class TalkSessionManager:
    """全域單一會話的仲裁 + 生命週期。voice_controller 在 __init__ 建一個。"""

    def __init__(
        self,
        *,
        google_client_provider: Callable[[], Any],
        play_tts: PlayTTS,
        send_text: SendText,
        pause_music: Callable[[], Any],
        resume_music: Callable[[], Any],
        persona_provider: Callable[[], str],
        paid_guard: PaidUsageGuard | None = None,
        model: str = "gemini-2.5-flash-lite",
        clock: Callable[[], float] = time.time,
    ):
        self._google_client_provider = google_client_provider
        self._play_tts = play_tts
        self._send_text = send_text
        self._pause_music = pause_music
        self._resume_music = resume_music
        self._persona_provider = persona_provider
        self._guard = paid_guard if paid_guard is not None else PaidUsageGuard(
            daily_cap_usd=_DAILY_CAP_USD, monthly_cap_usd=_MONTHLY_CAP_USD,
        )
        self._model = model
        self._clock = clock

        self.session: TalkSession | None = None
        self._watchdog: asyncio.Task | None = None

    @property
    def active(self) -> bool:
        return self.session is not None and self.session.active

    @property
    def owner_id(self) -> int | None:
        return self.session.owner_id if self.session else None

    async def toggle(self, user_id: int, user_name: str) -> str:
        """/marvin_talk：沒開就開、已開就關（任何人都能關）。回傳給使用者的訊息。"""
        if self.active:
            owner = self.session.owner_name if self.session else "某人"
            await self.stop(reason="手動結束")
            return f"🔇 結束跟{owner}的對話。"
        return await self.start(user_id, user_name)

    async def start(self, user_id: int, user_name: str) -> str:
        if self.active:
            return f"🗣️ 我正在跟 {self.session.owner_name} 說話，等一下。"

        client = self._google_client_provider()
        if client is None:
            return "😑 對話功能沒接上（缺 Gemini client）。"

        try:
            self._pause_music()
        except Exception as exc:
            logger.warning(f"[MarvinTalk] 暫停音樂失敗（不擋開場）：{exc}")

        self.session = TalkSession(
            owner_id=user_id, owner_name=user_name,
            google_client=client, play_tts=self._play_tts,
            persona_provider=self._persona_provider, paid_guard=self._guard,
            model=self._model, clock=self._clock,
            on_exit_phrase=lambda: self.stop(reason="使用者道別"),
        )
        self._watchdog = asyncio.ensure_future(self._watch())
        logger.info(f"[MarvinTalk] 會話開始：{user_name}({user_id})")
        return f"🎙️ 好，{user_name}，直接說話。{int(HARD_CAP_S)} 秒後自動收，或再打一次 /marvin_talk。"

    async def stop(self, reason: str = "") -> None:
        sess = self.session
        self.session = None
        if self._watchdog is not None:
            self._watchdog.cancel()
            self._watchdog = None
        if sess is not None:
            sess.close()
        try:
            self._resume_music()
        except Exception as exc:
            logger.warning(f"[MarvinTalk] 恢復音樂失敗：{exc}")
        logger.info(f"[MarvinTalk] 會話結束：{reason}")
        try:
            await self._send_text(f"🔇 對話結束（{reason}）。")
        except Exception:
            pass

    async def feed(self, user_id: int, pcm48k_stereo: bytes) -> None:
        """engine 在獨佔期間把觸發者的語音切片轉進來。"""
        sess = self.session
        if sess is None or not sess.active or user_id != sess.owner_id:
            return
        await sess.handle_turn(pcm48k_stereo)

    async def _watch(self) -> None:
        try:
            while self.active:
                await asyncio.sleep(1.0)
                sess = self.session
                if sess is None:
                    return
                reason = sess.deadline_reason()
                if reason is not None:
                    await self.stop(reason=reason)
                    return
        except asyncio.CancelledError:
            pass
