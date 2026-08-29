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
import logging
import time
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

# 用 gemini-2.5-flash（非 lite）——flash-lite 的音訊理解太差：實測會截斷轉錄
# （「台北101有幾層樓」只聽到「台北101有幾」）、把語音聽成「00:00 00:01 00:02」、
# 動不動「沒聽清楚」（16:10-16:14 整場如此）。flash 明顯準、也肯 grounding 查證。
# 代價：grounded turn ~5s（vs lite ~2.6s）——寧可慢一點也要答對。
_MODEL_CHAIN = ("gemini-2.5-flash", "gemini-flash-latest")

# 付費守門：比照 audio_rescue，偶發低頻、單次成本極低，放寬到跟 GCP spending cap 同量級
_DAILY_CAP_USD = 2.0
_MONTHLY_CAP_USD = 10.0

# 粗估：Gemini 音訊輸入約 32 token/秒；人格 prompt + 歷史約 900 token；輸出短。
# wav_bytes 是 48kHz stereo 16-bit（既有 pipeline 產出），約 192000 B/s。
_WAV_BYTES_PER_SECOND = 48000 * 2 * 2
_AUDIO_TOKENS_PER_SECOND = 32
_FIXED_PROMPT_TOKENS = 900
_ESTIMATED_OUTPUT_TOKENS = 200

_VOICE_SUFFIX = (
    "\n\n【語音對話補充規則】這是即時雙向語音通話（不是文字訊息），回覆用自然口語，"
    "講完被問到的那件事就停，不補評論、不加感嘆、不鋪陳；但別為了短而把話講半截、"
    "或砍掉日期數字這種關鍵資訊。\n"
    "有 Google 搜尋工具可用：碰到你不確定或可能過時的事實（人事時地、數字、近況），"
    "先查證再回，別硬掰、也別動不動就說「我沒有這筆資料」。只有真的查不到才說不知道。\n"
    "如果聽不清楚使用者在問什麼，直接說「你剛剛說什麼，沒聽清楚」，別自己腦補一個問題來答。\n"
    "如果使用者在道別或說要結束對話（掰掰、不聊了、先這樣…），回覆的最前面加上 <bye> 這個標記。"
)

_EXIT_MARKER = "<bye>"

MAX_REPLY_CHARS = 140     # 回覆超過就在句尾切——語音對話不聽長篇；也擋 mixer TTS 佇列爆掉
_MAX_OUTPUT_TOKENS = 220  # ≈ 130-150 中文字；配 persona 的「兩三句」一起收斂長度
PULSE_INTERVAL_S = 1.3    # 等待提示音的呼吸間隔


def _trim_reply(text: str) -> str:
    """回覆太長 → 在 MAX_REPLY_CHARS 前最後一個句末標點切；找不到就硬切。"""
    text = text.strip()
    if len(text) <= MAX_REPLY_CHARS:
        return text
    head = text[:MAX_REPLY_CHARS]
    for i in range(len(head) - 1, 0, -1):
        if head[i] in "。！？!?…":
            return head[: i + 1]
    return head + "…"


PlayTTS = Callable[[str], Awaitable[Any]]
SendText = Callable[[str], Awaitable[Any]]


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
        self.history: list[str] = []  # 馬文自己上幾句 reply
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
    async def handle_turn(self, wav_bytes: bytes, rms: int) -> None:
        """wav_bytes：既有 pipeline 已 normalize_rms + 抗混疊處理好的 WAV（見
        discord_voice_engine._flush_audio_to_stt）。rms：正規化前的原始 RMS，當
        「有沒有人真的在講話」的訊號。"""
        if not self.active or not wav_bytes:
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
            if rms < MIN_SPEECH_RMS:
                logger.info(f"[MarvinTalk] 切片 RMS={rms} < {MIN_SPEECH_RMS}，沒在講話，跳過")
                return

            self._last_activity = self._clock()
            logger.warning(f"[MarvinTalk] 收到 {self.owner_name} 一句（{len(wav_bytes)} bytes, rms={rms}），處理中")

            # 等伺服器回應期間持續出輕柔提示音（呼吸節奏），到回覆出聲才停——不留死寂
            pulse = asyncio.ensure_future(self._pulse_loop()) if self._on_heard else None
            try:
                reply = await self._call_gemini(wav_bytes)
            finally:
                if pulse is not None:
                    pulse.cancel()

            if reply is None:
                await self._safe_tts("抱歉，我剛剛恍神了，你再說一次。")
                return

            said_bye = reply.startswith(_EXIT_MARKER)
            reply = reply[len(_EXIT_MARKER):].strip() if said_bye else reply
            reply = _trim_reply(reply)
            logger.warning(f"[MarvinTalk] 回合 {self.turn_count + 1}｜回「{reply[:50]}」{' [bye]' if said_bye else ''}")
            self.turn_count += 1
            self.history.append(reply)
            del self.history[:-MAX_HISTORY_TURNS]
            self._last_activity = self._clock()

            # 開頭雷同 = 送進去的音訊每次都一樣（噪音）、對方沒在講 → 收會話。
            # 比開頭 16 字而非整句：grounding 回覆常帶點隨機尾巴，整句 == 抓不到。
            _key = "".join(reply.split())[:16]
            if _key and _key == self._last_reply:
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
            self._last_reply = _key

            await self._safe_tts(reply)

            if said_bye:
                logger.info("[MarvinTalk] 使用者道別 → 收會話")
                self.close()
                if self._on_exit_phrase is not None:
                    await self._on_exit_phrase()

    async def _call_gemini(self, wav_bytes: bytes) -> str | None:
        from google.genai import types

        system = self._persona_provider() + _VOICE_SUFFIX
        # role=model 存馬文自己上幾句（帶足夠上下文接續追問）；不存 user 側逐字稿——
        # 沒可靠來源（STT 已跳過），硬塞會被模型抄進回覆。
        contents: list[Any] = [
            types.Content(role="model", parts=[types.Part.from_text(text=r)])
            for r in self.history[-MAX_HISTORY_TURNS:]
        ]
        # 只放音訊、不加說明文字——實測加「（這是我這句話的音訊…）」這種 text part，
        # 模型會把它當旁白照抄成「（播放音訊）」而不去聽內容（16:10 整場回覆都是這個）。
        # 該做什麼 system_instruction 已經講清楚。
        contents.append(types.Content(role="user", parts=[
            types.Part.from_bytes(data=wav_bytes, mime_type="audio/wav"),
        ]))
        # google_search grounding：碰不確定的事實去查，別亂說也別動不動說沒資料。
        # 注意：grounding 與 response_schema(JSON) 不能並用 → 純文字輸出。
        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=0.8,
            max_output_tokens=_MAX_OUTPUT_TOKENS,
            tools=[types.Tool(google_search=types.GoogleSearch())],
        )

        # 免費 key 只試最快那顆（flash-lite）——free tier 的其他 model 常整批
        # 逾時 6s 純浪費（14:34 實測）。免費不通就直接退付費（過 paid cap），
        # 付費才跑整條 chain 求穩。
        est_in = _FIXED_PROMPT_TOKENS + max(1, len(wav_bytes) // _WAV_BYTES_PER_SECOND) * _AUDIO_TOKENS_PER_SECOND
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

            reply = (getattr(response, "text", None) or "").strip()
            if reply:
                return reply

        return None

    async def _pulse_loop(self) -> None:
        """等回應期間循環出提示音，第一下立刻、之後每 PULSE_INTERVAL_S 一下。"""
        try:
            while True:
                try:
                    await self._on_heard()
                except Exception:
                    pass
                await asyncio.sleep(PULSE_INTERVAL_S)
        except asyncio.CancelledError:
            pass

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

    async def feed(self, user_id: int, wav_bytes: bytes, rms: int) -> None:
        """engine 在 STT 之前把觸發者這句已處理好的 WAV 轉進來（含正規化前 RMS）。"""
        sess = self.session
        if sess is None or not sess.active or user_id != sess.owner_id:
            return
        await sess.handle_turn(wav_bytes, rms)

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
