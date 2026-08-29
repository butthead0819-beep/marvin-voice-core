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

API 金鑰：預設走**免費** key（router.google_client = GOOGLE_API_KEY），跟 STT
cleaner 共用同一把。免費全掛（429 / 逾時）才退到付費 key（google_paid_client），
且退付費前過 PaidUsageGuard cap。只有真的用到付費 client 才記帳。
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

# logger 名掛在 cogs.* 家族才會被 main_discord setup_early_logging 的 INFO allowlist 放行
# （裸頂層名會吃 root WARNING 全被吞——見 main_discord.py 註解裡同型坑三次中鏢）
logger = logging.getLogger("cogs.voice_controller.talk")

# ── 會話結束條件 ──────────────────────────────────────────────────────────────
# 沒有 wall-clock 硬上限——對話本來就沒有「講滿 N 秒」的道理。成本由既有 daily
# paid cap（_DAILY_CAP_USD）+ 「只在真的有人講話才打 LLM」（VAD 已 gate）守住。
# 結束靠：閒置逾時 / 說結束語 / 再打一次 /marvin_talk / bot 離開語音頻道。
IDLE_TIMEOUT_S = 180.0     # 這麼久沒有任何一句 → 自動收（沒人講話就沒成本，寬一點無妨）
MAX_HISTORY_TURNS = 8      # 送進 prompt 的最近對話輪數
GEMINI_TIMEOUT_S = 6.0     # 單次呼叫上限（實測 flash-lite 平時 ~2.6s；卡住就換下一家別乾等）
MIN_SPEECH_RMS = 180       # 切片整體 RMS 低於這值＝沒人真的在講話（房間噪音/呼吸），不送 LLM
                           # （正常語音 RMS ~1000-3000；14:52 實測噪音被當「00:00 00:01」瘋狂 hallucinate）
MAX_REPEAT_REPLIES = 2     # 連續回同一句這麼多次 → 判定對方沒在講、自動收會話

# 依序嘗試——flash-lite 平時最快（~2.6s），但偶爾整批「high demand」503/逾時
# （11:58 實測 5 回合掛 2 回）。flash-latest 是獨立容量、延遲相近，當即時 fallback。
_MODEL_CHAIN = ("gemini-2.5-flash-lite", "gemini-flash-latest")

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
        free_client: Any,
        paid_client: Any | None,
        play_tts: PlayTTS,
        persona_provider: Callable[[], str],
        paid_guard: PaidUsageGuard,
        model_chain: tuple[str, ...] = _MODEL_CHAIN,
        clock: Callable[[], float] = time.time,
        on_exit_phrase: Callable[[], Awaitable[Any]] | None = None,
        on_heard: Callable[[], Awaitable[Any]] | None = None,
    ):
        self.owner_id = owner_id
        self.owner_name = owner_name
        self._free_client = free_client
        self._paid_client = paid_client
        self._play_tts = play_tts
        self._persona_provider = persona_provider
        self._guard = paid_guard
        self._model_chain = model_chain
        self._clock = clock
        self._on_exit_phrase = on_exit_phrase
        self._on_heard = on_heard

        self.active = True
        self.started_at = clock()
        self._last_activity = self.started_at
        self.history: list[dict] = []  # [{"heard": str, "reply": str}]
        self.turn_count = 0
        self._last_reply = ""
        self._repeat_count = 0
        self._free_exhausted = False  # 免費 key 撞每日配額 → 本場不再試免費
        self._turn_lock = asyncio.Lock()

    # ── 生命週期 ──────────────────────────────────────────────────────────────
    def deadline_reason(self) -> str | None:
        if self._clock() - self._last_activity >= IDLE_TIMEOUT_S:
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

            # 沒人真的在講話（房間噪音/呼吸被 VAD 誤切）→ 不燒 LLM。VAD 不是萬能，
            # 14:52 實測整段噪音被 Gemini 當「00:00 00:01」瘋狂 hallucinate、連回 9 次同一句。
            from marvin_voice_core.audio_utils import calculate_rms
            rms = calculate_rms(pcm48k_stereo)
            if rms < MIN_SPEECH_RMS:
                logger.info(f"[MarvinTalk] 切片 RMS={rms} < {MIN_SPEECH_RMS}，沒在講話，跳過")
                return

            self._last_activity = self._clock()
            logger.warning(f"[MarvinTalk] 收到 {self.owner_name} 一句（{len(pcm48k_stereo)} bytes, rms={rms}），處理中")

            # 出個短音讓使用者知道「聽到了、正在想」——後面 LLM+TTS 還要等幾秒
            if self._on_heard is not None:
                try:
                    await self._on_heard()
                except Exception as exc:
                    logger.warning(f"[MarvinTalk] 收到提示音失敗（不擋）：{exc}")

            try:
                wav_bytes = _pcm48k_stereo_to_wav16k_mono(pcm48k_stereo)
            except Exception as exc:
                logger.warning(f"[MarvinTalk] 音訊轉檔失敗，跳過本回合：{exc}")
                return

            result = await self._call_gemini(wav_bytes)
            if result is None:
                await self._safe_tts("抱歉，我剛剛恍神了，你再說一次。")
                return

            heard, reply = result
            logger.warning(f"[MarvinTalk] 回合 {self.turn_count + 1}｜聽到「{heard[:30]}」→ 回「{reply[:40]}」")
            self.turn_count += 1
            self.history.append({"heard": heard, "reply": reply})
            del self.history[:-MAX_HISTORY_TURNS]
            self._last_activity = self._clock()

            # 連續同一句 = 送進去的音訊每次都一樣（噪音）、對方沒在講 → 收會話
            if reply == self._last_reply:
                self._repeat_count += 1
                if self._repeat_count >= MAX_REPEAT_REPLIES:
                    logger.warning(f"[MarvinTalk] 連續 {self._repeat_count + 1} 次回同一句，判定沒在對話 → 收")
                    self.close()
                    await self._safe_tts("你好像沒在講話，我先閉嘴。")
                    if self._on_exit_phrase is not None:
                        await self._on_exit_phrase()
                    return
            else:
                self._repeat_count = 0
            self._last_reply = reply

            await self._safe_tts(reply)

            if heard and any(p in heard for p in _EXIT_PHRASES):
                logger.info(f"[MarvinTalk] 聽到結束語「{heard}」→ 收會話")
                self.close()
                if self._on_exit_phrase is not None:
                    await self._on_exit_phrase()

    async def _call_gemini(self, wav_bytes: bytes) -> tuple[str, str] | None:
        from google.genai import types

        system = self._persona_provider() + _VOICE_SUFFIX
        # 用正式 role 對話串，別用「（我剛說）…（你回）…」文字前綴——實測會被模型
        # 原樣抄進回覆的逐字稿欄位（14:52 heard 出現「…（你回）嗯，你說得對」）。
        contents: list[Any] = []
        for turn in self.history[-MAX_HISTORY_TURNS:]:
            if turn.get("heard"):
                contents.append(types.Content(role="user", parts=[types.Part.from_text(text=turn["heard"])]))
            contents.append(types.Content(role="model", parts=[types.Part.from_text(text=turn["reply"])]))
        contents.append(types.Content(role="user", parts=[
            types.Part.from_text(text="（這是我這句話的音訊，聽完用馬文的口吻回我）"),
            types.Part.from_bytes(data=wav_bytes, mime_type="audio/wav"),
        ]))
        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=0.8,
            response_mime_type="application/json",
            response_schema=_RESPONSE_SCHEMA,
        )

        # 免費 key 只試最快那顆（flash-lite）——free tier 的其他 model 常整批
        # 逾時 6s 純浪費（14:34 實測）。免費不通就直接退付費（過 paid cap），
        # 付費才跑整條 chain 求穩。
        est_in = _FIXED_PROMPT_TOKENS + max(1, len(wav_bytes) // _PCM16_BYTES_PER_SECOND) * _AUDIO_TOKENS_PER_SECOND
        attempts: list[tuple[Any, str, bool]] = []
        if self._free_client is not None and not self._free_exhausted:
            attempts.append((self._free_client, self._model_chain[0], False))
        if self._paid_client is not None and self._guard.allow(
            estimate_cost(self._model_chain[0], est_in, _ESTIMATED_OUTPUT_TOKENS)
        ):
            attempts += [(self._paid_client, m, True) for m in self._model_chain]

        for client, model, is_paid in attempts:
            tier = "付費" if is_paid else "免費"
            try:
                response = await asyncio.wait_for(
                    client.aio.models.generate_content(
                        model=model, contents=contents, config=config,
                    ),
                    timeout=GEMINI_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                logger.warning(f"[MarvinTalk] {tier} {model} 逾時（{GEMINI_TIMEOUT_S}s），換下一個")
                continue
            except Exception as exc:
                # 免費 key 撞每日配額（RESOURCE_EXHAUSTED）→ 本場後續都別再試免費，直接付費
                if not is_paid and "RESOURCE_EXHAUSTED" in str(exc):
                    self._free_exhausted = True
                    logger.warning("[MarvinTalk] 免費 key 今日配額用盡，本場改走付費")
                else:
                    logger.warning(f"[MarvinTalk] {tier} {model} 失敗：{str(exc)[:120]}，換下一個")
                continue

            usage = getattr(response, "usage_metadata", None)
            in_tokens = int(getattr(usage, "prompt_token_count", 0) or 0)
            out_tokens = int(getattr(usage, "candidates_token_count", 0) or _ESTIMATED_OUTPUT_TOKENS)
            if is_paid:  # 只有真的打到付費 client 才記帳（免費 key 不進帳本）
                self._guard.record(
                    caller="marvin_talk", model=model, tokens=in_tokens + out_tokens,
                    est_usd=estimate_cost(model, in_tokens or _FIXED_PROMPT_TOKENS, out_tokens),
                    in_tokens=in_tokens, out_tokens=out_tokens,
                )

            raw = (getattr(response, "text", None) or "").strip()
            if not raw:
                continue
            try:
                data = json.loads(raw)
                reply = str(data.get("reply", "")).strip()
                heard = str(data.get("heard", "")).strip()
            except (json.JSONDecodeError, ValueError, AttributeError):
                reply, heard = raw, ""
            if reply:
                return heard, reply

        return None

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
        free_client_provider: Callable[[], Any],
        paid_client_provider: Callable[[], Any] | None = None,
        play_tts: PlayTTS,
        send_text: SendText,
        pause_music: Callable[[], Any],
        resume_music: Callable[[], Any],
        persona_provider: Callable[[], str],
        is_voice_connected: Callable[[], bool] | None = None,
        heard_cue: Callable[[], Awaitable[Any]] | None = None,
        paid_guard: PaidUsageGuard | None = None,
        model_chain: tuple[str, ...] = _MODEL_CHAIN,
        clock: Callable[[], float] = time.time,
    ):
        self._free_client_provider = free_client_provider
        self._paid_client_provider = paid_client_provider or (lambda: None)
        self._play_tts = play_tts
        self._send_text = send_text
        self._heard_cue = heard_cue
        self._pause_music = pause_music
        self._resume_music = resume_music
        self._persona_provider = persona_provider
        self._is_voice_connected = is_voice_connected or (lambda: True)
        self._guard = paid_guard if paid_guard is not None else PaidUsageGuard(
            daily_cap_usd=_DAILY_CAP_USD, monthly_cap_usd=_MONTHLY_CAP_USD,
        )
        self._model_chain = model_chain
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

        free_client = self._free_client_provider()
        paid_client = self._paid_client_provider()
        if free_client is None and paid_client is None:
            return "😑 對話功能沒接上（缺 Gemini client）。"

        try:
            self._pause_music()
        except Exception as exc:
            logger.warning(f"[MarvinTalk] 暫停音樂失敗（不擋開場）：{exc}")

        self.session = TalkSession(
            owner_id=user_id, owner_name=user_name,
            free_client=free_client, paid_client=paid_client, play_tts=self._play_tts,
            persona_provider=self._persona_provider, paid_guard=self._guard,
            model_chain=self._model_chain, clock=self._clock,
            on_exit_phrase=lambda: self.stop(reason="使用者道別"),
            on_heard=self._heard_cue,
        )
        self._watchdog = asyncio.ensure_future(self._watch())
        logger.warning(f"[MarvinTalk] 會話開始：{user_name}({user_id})")
        # 出聲確認模式已開，使用者才知道可以講（純文字訊息容易被頻道洗掉沒看到）
        try:
            await self._play_tts("嗯，我在聽，你說。")
        except Exception as exc:
            logger.warning(f"[MarvinTalk] 開場語音失敗（不擋）：{exc}")
        return (
            f"🎙️ 好，{user_name}，直接說話。說「掰掰馬文」或再打一次 /marvin_talk 結束"
            f"（{int(IDLE_TIMEOUT_S // 60)} 分鐘沒聲音會自動收）。"
        )

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
        logger.warning(f"[MarvinTalk] 會話結束：{reason}")
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
                sess = self.session
                if sess is None:
                    return
                if not self._is_voice_connected():
                    await self.stop(reason="離開語音頻道")
                    return
                reason = sess.deadline_reason()
                if reason is not None:
                    await self.stop(reason=reason)
                    return
                await asyncio.sleep(5.0)
        except asyncio.CancelledError:
            pass
