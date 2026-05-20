import re
import json
import time
import os
import asyncio
import logging
from pathlib import Path
import google.genai as genai
from utils import check_cleaned_text_for_wake

logger = logging.getLogger(__name__)

_XML_TAG_RE = re.compile(r"</?(?:Target|Background)[^>]*>", re.IGNORECASE)
_RETRY_AFTER_RE = re.compile(r'try again in (\d+(?:\.\d+)?)\s*s', re.IGNORECASE)

# ── STT 修正對記錄 ────────────────────────────────────────────────────────────
_CORRECTIONS_LOG = Path("records/stt_corrections.jsonl")
# Aggregated map（read fast-path）：daily-review 把 jsonl 整理成 json key→cleaned。
# Tests should monkeypatch this to a non-existent path to bypass fast-path 早退。
_LOCAL_CORRECTIONS_PATH = Path("records/stt_corrections.json")

def _append_stt_correction(raw: str, cleaned: str, spk: str):
    """非同步安全：直接寫入（呼叫在單執行緒事件循環內）。"""
    try:
        _CORRECTIONS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _CORRECTIONS_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), "speaker": spk, "raw": raw, "clean": cleaned}, ensure_ascii=False) + "\n")
    except Exception:
        pass

WAKE_THRESHOLD = 0.70


def _strip_xml_artifacts(text: str) -> str:
    """剝掉模型可能原樣回吐的 <Target>/<Background> 標籤。"""
    return _XML_TAG_RE.sub("", text).strip()


def _parse_retry_after(err_str: str, default: float = 12.0) -> float:
    """從 Groq 429 錯誤訊息解析 retry-after 秒數，加 1s buffer。"""
    m = _RETRY_AFTER_RE.search(str(err_str))
    return float(m.group(1)) + 1.0 if m else default


def _verify_wake_against_raw(
    is_wake: bool,
    wake_intent: float | None,
    raw_text: str | None,
) -> tuple[bool, float | None]:
    """Wake Injection Guard — LLM 判 wake 但 raw 無喚醒詞 → 雙清。

    5/18 P2-a fix：原本只清 is_wake 不清 wake_intent，下游 IBA fusion 仍
    把 wake_intent 當 voice score truth source（_voice_score: track="B" +
    wake_intent != None → return wake_intent），導致 "3F D呀每天都去點"
    這類 raw 無 marvin 但 LLM 判 intent=1.0 的 case 還是進到 Track B wake。

    保險：raw_text=None 視為「無資料無法判」不 reject；空字串視為注入照清。
    """
    if not is_wake:
        return is_wake, wake_intent
    if raw_text is None:
        return is_wake, wake_intent
    if not check_cleaned_text_for_wake(raw_text):
        return False, None
    return is_wake, wake_intent


class GeminiRouterSTTMixin:
    """STT 文本校正（Wake Injection Guard 雙重防禦）。"""

    def _ensure_groq_state(self):
        """懶初始化 Groq 冷卻狀態（避免修改 GeminiRouter.__init__ 順序）。"""
        if not hasattr(self, '_groq_tpm_lock'):
            self._groq_tpm_lock = asyncio.Lock()
        if not hasattr(self, '_groq_8b_cooldown_until'):
            self._groq_8b_cooldown_until = 0.0
        if not hasattr(self, '_groq_70b_cooldown_until'):
            self._groq_70b_cooldown_until = 0.0
        if not hasattr(self, '_cerebras_cooldown_until'):
            self._cerebras_cooldown_until = 0.0

    async def clean_stt_text(self, raw_text: str, context: list = None, speaker: str = None,
                              context_active: bool = False, marvin_just_spoke: bool = False) -> dict:
        """
        [Operation Clean STT] Phase 1: Fused Intent Scorer.
        Returns {"text": str, "is_wake": bool, "wake_intent": float|None, "wake_threshold": float}
        Priority: Groq 8b (cooldown-aware) -> Groq 70b (cooldown-aware) -> raw fallback
        """
        self._ensure_groq_state()
        stripped_text = raw_text.strip()

        def _build_res(text, original=None, wake_intent=None, calling=None):
            threshold = WAKE_THRESHOLD
            if wake_intent is not None:
                fusion = getattr(self, 'wake_fusion', None)
                if fusion and speaker:
                    is_wake, threshold = fusion.decide(wake_intent, speaker, context_active, marvin_just_spoke)
                else:
                    if wake_intent >= 0.75:
                        is_wake = True
                    elif wake_intent >= 0.65:
                        is_wake = calling is True
                    else:
                        is_wake = False
            else:
                is_wake = check_cleaned_text_for_wake(text)

            # 🛡️ [Wake Injection Guard] LLM 過矯正：原始文本無喚醒詞 → 雙清
            # is_wake + wake_intent 兩個都要清，避免下游 IBA fusion 用 wake_intent
            # 推 is_fast=True（5/18 #10/#20/#23 false positive 根因）
            verified_wake, verified_intent = _verify_wake_against_raw(is_wake, wake_intent, original)
            if is_wake and not verified_wake:
                logger.warning(f"⚠️ [STT Clean] LLM 注入喚醒詞 (過矯正)：'{original}' -> '{text}'，已拒絕。")
                return {"text": original, "is_wake": False, "wake_intent": verified_intent, "wake_threshold": threshold}

            # 📝 [STT Correction Log] 有意義的修正才記錄（排除純空白差異）
            if original is not None and text.strip() != original.strip() and speaker:
                _append_stt_correction(original, text, speaker)

            return {"text": text, "is_wake": is_wake, "wake_intent": wake_intent, "wake_threshold": threshold}

        # 🔤 [Local Corrections] 優先查本地累積修正字典（零 LLM 成本）
        _corr_path = _LOCAL_CORRECTIONS_PATH
        if _corr_path.exists():
            try:
                _corr_data = json.loads(_corr_path.read_text(encoding="utf-8"))
                _local_corrections: dict = _corr_data.get("corrections", {})
                if stripped_text in _local_corrections:
                    _fixed = _local_corrections[stripped_text]
                    logger.debug(f"[STT Clean] 本地修正：'{stripped_text}' → '{_fixed}'")
                    return _build_res(_fixed, original=stripped_text)
            except Exception:
                pass

        # 🛡️ [Rate Limit Saver] 過濾太短的字句
        if not stripped_text or len(stripped_text) < 3:
            return _build_res(raw_text)

        # 🛡️ [Rate Limit Saver] 過濾完全疊字的無意義發音
        if len(set(stripped_text.replace(" ", ""))) == 1:
            return _build_res(raw_text)

        # 🔒 [TPM Guard — Atomic] 在鎖內讀取+檢查，防止多個 coroutine 同時通過
        async with self._groq_tpm_lock:
            now = time.time()
            self.groq_cleaner_usage = [u for u in self.groq_cleaner_usage if now - u[0] <= 60]
            current_tpm = sum(u[1] for u in self.groq_cleaner_usage)
            if current_tpm > 4500:
                logger.warning(f"⚠️ [TPM Guard] Groq 清洗額度接近上限 ({current_tpm}/6000 TPM)，跳過本次清洗。")
                return _build_res(raw_text)

        system_prompt = self.prompt_manager.get_instruction("stt_cleaner", vision_enabled=False)

        if context:
            background = "\n".join(
                f"{u['speaker']}：{_strip_xml_artifacts(u['text'])}" for u in context
            )
            user_message = f"<Background>\n{background}\n</Background>\n\n<Target>{raw_text}</Target>"
        else:
            user_message = f"<Target>{raw_text}</Target>"

        def _validate_cleaned(text: str, original: str) -> tuple[str, float | None, bool | None, bool] | None:
            """Parse and validate LLM output. Returns (text, wake_intent, calling, is_complete) or None."""
            stripped = text.strip()
            if stripped.startswith('{'):
                try:
                    data = json.loads(stripped)
                    cleaned = str(data.get("cleaned", "")).strip()
                    intent = float(data.get("intent", 0.0))
                    calling = bool(data.get("calling", False))
                    is_complete = bool(data.get("is_complete", True))
                    if not cleaned:
                        logger.warning(f"⚠️ [STT Clean] JSON 缺少 cleaned 欄位，降級純文字: {stripped[:60]}")
                        return (original, None, None, True)
                    intent = max(0.0, min(1.0, intent))
                    return (cleaned, intent, calling, is_complete)
                except (json.JSONDecodeError, ValueError) as e:
                    logger.warning(f"⚠️ [STT Clean] JSON 解析失敗，降級純文字: {e} | raw='{stripped[:40]}'")
                    return (original, None, None, True)

            if '\n' in text:
                logger.warning(f"⚠️ [STT Clean] 輸出含換行 (吐出脈絡)，拒絕使用: '{text[:40]}...'")
                return None
            if len(text) > len(original) * 2.5 + 10:
                logger.warning(f"⚠️ [STT Clean] 輸出過長 ({len(text)} vs {len(original)})，拒絕使用: '{text[:40]}...'")
                return None
            return (text, None, None, True)

        # ── 1. Groq 8b (極速路徑) ────────────────────────────────────────────
        if self.groq_dedicated_client:
            now = time.time()
            if now < self._groq_8b_cooldown_until:
                remaining = self._groq_8b_cooldown_until - now
                logger.info(f"⏳ [STT Clean] Groq 8b 冷卻中，剩餘 {remaining:.1f}s，跳過。")
            else:
                try:
                    cleaner_model = os.getenv("GROQ_CLEANER_MODEL", "llama-3.1-8b-instant")
                    response = await self.groq_dedicated_client.chat.completions.create(
                        model=cleaner_model,
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_message}
                        ],
                        temperature=0.0,
                        max_tokens=200,
                        response_format={"type": "json_object"}
                    )
                    raw_output = _strip_xml_artifacts(response.choices[0].message.content)
                    usage = getattr(response, "usage", None)
                    if usage:
                        async with self._groq_tpm_lock:
                            self.groq_cleaner_usage.append((time.time(), usage.total_tokens))

                    result = _validate_cleaned(raw_output, raw_text)
                    if result is None:
                        return _build_res(raw_text)
                    validated_text, wake_intent, calling, is_complete = result
                    res = _build_res(validated_text, original=raw_text, wake_intent=wake_intent, calling=calling)
                    res["is_complete"] = is_complete
                    _spk = speaker or "unknown"
                    _decision = "WAKE" if res["is_wake"] else "PASS"
                    _intent_str = f"{wake_intent:.2f}" if wake_intent is not None else "regex"
                    logger.debug(
                        f"[WAKE_INTENT] ts={time.time():.3f} speaker={_spk} raw='{raw_text[:30]}' "
                        f"intent={_intent_str} threshold={WAKE_THRESHOLD:.2f} decision={_decision} track=B tpm={current_tpm} complete={is_complete}"
                    )
                    return res
                except Exception as e:
                    err_str = str(e)
                    if "429" in err_str or "rate_limit_exceeded" in err_str:
                        cooldown = _parse_retry_after(err_str)
                        self._groq_8b_cooldown_until = time.time() + cooldown
                        logger.warning(f"⚠️ [STT Clean] Groq 8b 429 — 進入冷卻 {cooldown:.1f}s，直接跳備援。")
                    else:
                        logger.warning(f"⚠️ [STT Clean] Groq 8b 失敗，嘗試 70b 備援: {e}")

        # ── 1.5. Cerebras llama-3.1-8b (TPM 救援，~100ms 延遲) ─────────────
        # Groq 8b 429 / 失敗時優先嘗試 Cerebras，比 Groq 70b 快且不擠 Groq bucket。
        cerebras_client = getattr(self, 'cerebras_client', None)
        cerebras_model = getattr(self, 'cerebras_model', None)
        if cerebras_client and cerebras_model:
            now = time.time()
            if now < self._cerebras_cooldown_until:
                remaining = self._cerebras_cooldown_until - now
                logger.info(f"⏳ [STT Clean] Cerebras 冷卻中，剩餘 {remaining:.1f}s，跳至 Groq 70b。")
            else:
                try:
                    response = await cerebras_client.chat.completions.create(
                        model=cerebras_model,
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_message}
                        ],
                        temperature=0.0,
                        max_tokens=200,
                        response_format={"type": "json_object"},
                    )
                    raw_output = _strip_xml_artifacts(response.choices[0].message.content)
                    result = _validate_cleaned(raw_output, raw_text)
                    if result is None:
                        return _build_res(raw_text)
                    validated_text, wake_intent, calling, is_complete = result
                    res = _build_res(validated_text, original=raw_text, wake_intent=wake_intent, calling=calling)
                    res["is_complete"] = is_complete
                    _spk = speaker or "unknown"
                    _decision = "WAKE" if res["is_wake"] else "PASS"
                    _intent_str = f"{wake_intent:.2f}" if wake_intent is not None else "regex"
                    logger.debug(
                        f"[WAKE_INTENT] ts={time.time():.3f} speaker={_spk} raw='{raw_text[:30]}' "
                        f"intent={_intent_str} threshold={WAKE_THRESHOLD:.2f} decision={_decision} track=B (cerebras) complete={is_complete}"
                    )
                    return res
                except Exception as e:
                    err_str = str(e)
                    if "429" in err_str or "rate_limit_exceeded" in err_str:
                        cooldown = _parse_retry_after(err_str)
                        self._cerebras_cooldown_until = time.time() + cooldown
                        logger.warning(f"⚠️ [STT Clean] Cerebras 429 — 進入冷卻 {cooldown:.1f}s，跳至 Groq 70b。")
                    else:
                        logger.warning(f"⚠️ [STT Clean] Cerebras 失敗，嘗試 Groq 70b: {e}")

        # ── 2. Groq 70b 備援 (不同 TPM bucket) ──────────────────────────────
        backup_model = getattr(self, 'groq_fallback_model', None) or "llama-3.3-70b-versatile"
        if self.groq_dedicated_client and backup_model:
            now = time.time()
            if now < self._groq_70b_cooldown_until:
                remaining = self._groq_70b_cooldown_until - now
                logger.info(f"⏳ [STT Clean] Groq 70b 冷卻中，剩餘 {remaining:.1f}s，降級原始文本。")
            else:
                try:
                    response = await self.groq_dedicated_client.chat.completions.create(
                        model=backup_model,
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_message}
                        ],
                        temperature=0.0,
                        max_tokens=200,
                        response_format={"type": "json_object"}
                    )
                    raw_output = _strip_xml_artifacts(response.choices[0].message.content)
                    result = _validate_cleaned(raw_output, raw_text)
                    if result is None:
                        return _build_res(raw_text)
                    validated_text, wake_intent, calling, is_complete = result
                    res = _build_res(validated_text, original=raw_text, wake_intent=wake_intent, calling=calling)
                    res["is_complete"] = is_complete
                    _spk = speaker or "unknown"
                    _decision = "WAKE" if res["is_wake"] else "PASS"
                    _intent_str = f"{wake_intent:.2f}" if wake_intent is not None else "regex"
                    logger.debug(
                        f"[WAKE_INTENT] ts={time.time():.3f} speaker={_spk} raw='{raw_text[:30]}' "
                        f"intent={_intent_str} threshold={WAKE_THRESHOLD:.2f} decision={_decision} track=B (70b) complete={is_complete}"
                    )
                    return res
                except Exception as e:
                    err_str = str(e)
                    if "429" in err_str or "rate_limit_exceeded" in err_str:
                        cooldown = _parse_retry_after(err_str)
                        self._groq_70b_cooldown_until = time.time() + cooldown
                        logger.warning(f"⚠️ [STT Clean] Groq 70b 429 — 進入冷卻 {cooldown:.1f}s，降級原始文本。")
                    else:
                        logger.error(f"❌ [STT Clean] 所有 Groq 方案皆失敗: {e}")

        # ── 3. Gemini flash-lite 備援 (獨立 RPM bucket，非阻塞) ─────────────
        cleaner_client = getattr(self, 'google_cleaner_client', None)
        cleaner_model = getattr(self, 'cleaner_model', "gemini-2.0-flash-lite")
        if cleaner_client and getattr(self, '_try_acquire_cleaner_rpm_slot', None) and self._try_acquire_cleaner_rpm_slot():
            try:
                config = genai.types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    response_mime_type="application/json",
                    temperature=0.0,
                )
                response = await asyncio.wait_for(
                    cleaner_client.aio.models.generate_content(
                        model=cleaner_model,
                        contents=user_message,
                        config=config,
                    ),
                    timeout=8.0,
                )
                raw_output = _strip_xml_artifacts(response.text.strip())
                result = _validate_cleaned(raw_output, raw_text)
                if result is None:
                    return _build_res(raw_text)
                validated_text, wake_intent, calling, is_complete = result
                res = _build_res(validated_text, original=raw_text, wake_intent=wake_intent, calling=calling)
                res["is_complete"] = is_complete
                logger.debug(
                    f"[WAKE_INTENT] ts={time.time():.3f} speaker={speaker or 'unknown'} "
                    f"raw='{raw_text[:30]}' decision={'WAKE' if res['is_wake'] else 'PASS'} track=B (gemini-fallback)"
                )
                return res
            except Exception as e:
                logger.error(f"❌ [STT Clean] Gemini flash-lite 備援也失敗: {e}")

        # ── 4. 降級：返回原始文本 ────────────────────────────────────────────
        return _build_res(raw_text)
