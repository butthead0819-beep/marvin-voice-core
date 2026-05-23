import re
import json
import time
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
# Cleaner gate 丟棄記錄：每筆 = 一個被略過(未送 cleaner)的句子。drop-rate 數行數即可；
# review 此檔確認被丟的都是碎念/雜訊（不該有真指令）。
_GATE_DROP_LOG = Path("records/cleaner_gate_drops.jsonl")

def _append_stt_correction(raw: str, cleaned: str, spk: str):
    """非同步安全：直接寫入（呼叫在單執行緒事件循環內）。"""
    try:
        _CORRECTIONS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _CORRECTIONS_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), "speaker": spk, "raw": raw, "clean": cleaned}, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _append_gate_drop(raw: str, sig: dict):
    """記錄一個被 cleaner gate 略過的句子（供 drop-rate 統計 + false-neg review）。"""
    try:
        _GATE_DROP_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _GATE_DROP_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), "raw": (raw or "")[:50], **sig}, ensure_ascii=False) + "\n")
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


# ── Cleaner pre-gate（2026-05-21）──────────────────────────────────────────────
# 每句 STT 都打 cleaner 是 TPD 大宗。一句話只在「可能對 Marvin 有意圖」時才值得送：
#   有指令訊號（喚醒音/音樂詞/龍蝦）OR 正在對話中（ctx_active / Marvin 剛說）。
# 兩者皆無 → 長碎念/短 filler/雜訊，無意圖 → 丟（略過 cleaner）。
# 安全性：無喚醒詞的真指令只能經對話窗到 cleaner（Injection Guard 擋 Track-B 無詞 wake），
# 故 ctx/spoke 會放行；歷史 26156 句驗證被丟的長句全是雜訊（true false-neg≈0）。
_GATE_WAKE_RE = re.compile(
    "|".join(["馬文", "媽文", "麻文", "瑪文", "罵文", "马文", "馬汶", "馬問", "馬紋",
              "嗎文", "marvin", "marvy", "marvgin", "龍蝦", "龙虾"]), re.IGNORECASE)
try:
    from intent_agents.constants import (
        MUSIC_PLAY_KW as _GP, MUSIC_DIRECT_SKIP_KW as _GS,
        MUSIC_DIRECT_STOP_KW as _GT, MUSIC_DIRECT_PAUSE_KW as _GU,
        MUSIC_DIRECT_RESUME_KW as _GV,
    )
    _GATE_MUSIC_KW = tuple(_GP) + tuple(_GS) + tuple(_GT) + tuple(_GU) + tuple(_GV)
except Exception:
    _GATE_MUSIC_KW = ()
# gate-only 寬鬆 token：STT 常把「播放」截成「播」（如「播蕭煌奇」=「播放蕭煌奇」）。
# gate 在 raw 上判定，但 no-wake 点歌的 keyword match 跑在 cleaned 上——gate 漏接這句，
# cleaner 就沒機會把「播」修回「播放」。補「播」讓 gate ≥ no-wake 点歌的覆蓋。誤檢只多
# 花一次 cleaner call（無害），實測 drop-log 377 句僅 1 句含「播」。「放」太常見（放假/放心）故不補。
_GATE_MUSIC_KW = _GATE_MUSIC_KW + ("播",)


def cleaner_gate_decision(raw_text, *, context_active=False, marvin_just_spoke=False):
    """raw 是否值得送 cleaner LLM。回 (would_send: bool, signals: dict)。

    would_send=False → gate 略過此句（不打 cleaner）。has_wake 同時吃 _GATE_WAKE_RE 與
    check_cleaned_text_for_wake，確保「gate 放行的 wake 判定」≥「_build_res 的 wake 判定」，
    被 drop 的句子 _build_res 必定 is_wake=False（一致）。
    """
    raw = raw_text or ""
    has_wake = bool(_GATE_WAKE_RE.search(raw)) or check_cleaned_text_for_wake(raw)
    low = raw.lower()
    has_music = any(kw.lower() in low for kw in _GATE_MUSIC_KW)
    would_send = bool(has_wake or has_music or context_active or marvin_just_spoke)
    return would_send, {"wake": has_wake, "music": has_music,
                        "ctx": bool(context_active), "spoke": bool(marvin_just_spoke)}


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

    def _ensure_stt_router(self):
        """懶建 cleaner 用的 TieredLLMRouter（多家 free-tier 算力池，cooldown-aware）。

        取代舊的 Groq8b→Cerebras→Groq70b 硬編 tier chain：quick pool 自動在 Groq/
        Cerebras/SambaNova… 間分流 + 429 cooldown 記憶；缺 quota 才升 analyze(70b)。
        測試預先設 self._stt_router 注入 fake 即跳過 build。
        """
        if getattr(self, '_stt_router', None) is None:
            from llm_pool import build_tiered_router
            self._stt_router = build_tiered_router()

    async def clean_stt_text(self, raw_text: str, context: list = None, speaker: str = None,
                              context_active: bool = False, marvin_just_spoke: bool = False,
                              marvin_in_echo_window: bool = False,
                              apply_gate: bool = False) -> dict:
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
                    is_wake, threshold = fusion.decide(
                        wake_intent, speaker, context_active, marvin_just_spoke,
                        marvin_in_echo_window=marvin_in_echo_window)
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

        # 🚪 [Cleaner Gate] 僅 wake-check 路徑 opt-in（apply_gate=True）。無指令訊號(喚醒/
        # 音樂/龍蝦) + 非對話中 → 無對 Marvin 意圖 → 略過 cleaner（省 TPD）。純文字清洗
        # caller（apply_gate=False，如遊戲答案清洗）不 gate，照常 LLM 清。被丟的記 jsonl 供 review。
        if apply_gate:
            _gate_send, _gate_sig = cleaner_gate_decision(
                raw_text, context_active=context_active, marvin_just_spoke=marvin_just_spoke)
            if not _gate_send:
                _append_gate_drop(raw_text, _gate_sig)
                return _build_res(raw_text)

        # TPM guard 改由 CooldownAwarePool per-endpoint 處理（每家自己的 tpm_budget +
        # 429 cooldown），不再手動守 Groq 單一 bucket。

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

        def _finalize(raw_output, track_label):
            """raw_output(str) → res dict 並 log；None（pool 全冷卻）→ None 讓 caller 換下一層。
            validate 失敗（換行/過長/JSON 壞）→ 直接降級 raw（不換層，與舊行為一致）。"""
            if raw_output is None:
                return None
            cleaned_out = _strip_xml_artifacts(raw_output)
            result = _validate_cleaned(cleaned_out, raw_text)
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
                f"intent={_intent_str} threshold={WAKE_THRESHOLD:.2f} decision={_decision} {track_label} complete={is_complete}"
            )
            return res

        # ── 算力池：quick(8b 多家自動分流) → analyze(70b 升級)，cooldown-aware ──
        # 取代舊 Groq8b→Cerebras→Groq70b 硬編 chain。dispatch 內部對每家 429 記 cooldown
        # 並跳到下一家；全冷卻才回 None → 升 analyze；再全空 → 落 Gemini/raw（不變）。
        # validate 失敗仍直接回 raw（語意與舊一致）。
        self._ensure_stt_router()
        _router = self._stt_router
        content = await _router.quick(user_message, caller="stt_cleaner",
                                      system=system_prompt, max_tokens=200,
                                      temperature=0.0, json=True)
        res = _finalize(content, "track=B (pool-quick)")
        if res is not None:
            return res
        content = await _router.analyze(user_message, caller="stt_cleaner",
                                        system=system_prompt, max_tokens=200,
                                        temperature=0.0, json=True)
        res = _finalize(content, "track=B (pool-analyze)")
        if res is not None:
            return res

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
