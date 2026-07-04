"""
WakeDetector — single entry point for all wake-word logic.

Merges:
  • utils.py  → pre_filter_speech, check_cleaned_text_for_wake, WAKE_WORDS_LIST
  • wake_signal_fusion.py → WakeSignalFusion (4-channel confidence accumulator)

Callers use WakeDetector directly; wake_signal_fusion.py stays as a thin alias
for backward compatibility.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# ── File paths ────────────────────────────────────────────────────────────────

_STATS_FILE       = os.path.join(os.path.dirname(__file__), "wake_stats.json")
_CALIBRATION_FILE = Path(__file__).parent / "records" / "iba_calibration.json"
_CUSTOM_FILE      = Path(__file__).parent / "records" / "iba_custom_patterns.json"
_OVERRIDE_FILE    = Path(__file__).parent / "records" / "wake_words_override.json"

# ── Wake word lists ───────────────────────────────────────────────────────────

WAKE_WORDS_LIST: list[str] = [
    # 3-syllable (lowest false-trigger rate — match longest first)
    "嗨馬文", "艾馬文", "艾瑪文", "阿姨文", "馬文同學",
    # English in Chinese context (highly distinctive)
    "hey marvin", "oh marvin", "marvin", "marv", "marwen", "mavin",
    # 2-syllable main term
    "馬文",
    # STT near-misses
    "馬聞", "馬溫", "麻文", "馬問", "馬穩", "馬門", "馬萌",
    "毛文",  # 2026-06-13 SwiftV2 實測聲學混淆（「馬文這首誰唱的」→「毛文…」喚醒漏接）
]

# Sentence-start only — too ambiguous mid-sentence
FAST_ONLY_WAKE_WORDS: list[str] = ["馬哥", "老馬", "杜比"]


def _load_wake_override() -> None:
    """Expand / prune WAKE_WORDS_LIST from records/wake_words_override.json."""
    try:
        if not _OVERRIDE_FILE.exists():
            return
        data = json.loads(_OVERRIDE_FILE.read_text(encoding="utf-8"))
        for w in data.get("additions", []):
            if w and w not in WAKE_WORDS_LIST:
                WAKE_WORDS_LIST.append(w)
        for w in data.get("removals", []):
            if w in WAKE_WORDS_LIST:
                WAKE_WORDS_LIST.remove(w)
    except Exception:
        pass


_load_wake_override()

_ALL_WAKE_WORDS   = WAKE_WORDS_LIST + FAST_ONLY_WAKE_WORDS
WAKE_PATTERN      = "|".join(_ALL_WAKE_WORDS)
_FORCE_WAKE_PAT   = "|".join(WAKE_WORDS_LIST)   # excludes high-ambiguity fast-only words

# ── Follow-up listening: question-marker detection ───────────────────────────
# D4: 吧 excluded (highly polysemous suggestion/agreement particle; revisit in v2 with session data)
_QUESTION_MARKER_RE = re.compile(r'[?？嗎呢]\s*$')


def _has_question_marker(text: str) -> bool:
    """Return True when text ends with a question marker (?, ？, 嗎, 呢)."""
    return bool(_QUESTION_MARKER_RE.search(text.strip()))


# ── 4-channel scoring regexes ─────────────────────────────────────────────────

_TASK_HARD_RE = re.compile(
    r'幫我|幫[你他她]?(?:查|播|找|搜|推|告|解|分|帶|念|翻|算)'
    r'|播放|搜尋|推薦|解釋|分析',
    re.IGNORECASE,
)
_TASK_SOFT_RE = re.compile(
    r'(?:^|[\s，,])(?:我想|我要|我需要|可以|能不能|可不可以|你可以|你能|你知道)'
    r'|[嗎呢？?]\s*$',
    re.IGNORECASE,
)
_INFO_GENERAL_RE = re.compile(
    r'(?:什麼意思|怎麼回事|為什麼會|哪裡可以|是什麼東西|誰知道)',
    re.IGNORECASE,
)
_INFO_MUSIC_RE = re.compile(
    r'這首(?:歌|曲)?(?:叫什麼|是什麼|是誰|叫做|的名字|哪首|叫|叫啥)'
    r'|(?:現在|剛才|正在)(?:播|放|唱)的(?:是|叫)?'
    r'|(?:歌名|歌手|藝人|誰唱|誰寫)(?:是什麼|叫什麼|是誰|叫)',
    re.IGNORECASE,
)
_CTRL_SILENCE_RE = re.compile(
    r'閉嘴|不要說話|別說了|安靜|暫時靜音|停止說話|先別說',
    re.IGNORECASE,
)
_CTRL_MUSIC_RE = re.compile(
    r'換一首|下一首|跳過|換歌|不要這首'
    r'|停止播放|音樂停|不要播了|關掉音樂|停音樂|音樂關掉'
    r'|暫停音樂|繼續播|繼續音樂|播回來'
    r'|音量大[一點些]?|音量小[一點些]?|大聲一點|小聲一點',
    re.IGNORECASE,
)
_CTRL_MARMO_RE = re.compile(
    r'(?:問|叫|用|讓|請|call|問問|叫一下)?(?:marmo|馬某|馬摸|馬墨)',
    re.IGNORECASE,
)
# 點歌句型（2026-07-04）：句首 4 字內的點歌動詞（容納「馬文/把我們/幫我」等前綴），
# 動詞後須帶內容。動詞在句中（>4 字）＝聊天引用（「他昨天播放了影片」），不中。
# 背景：喚醒詞糊掉（馬文→把我們，v=0.3）時 control 拿 0 分 → total 0.346 差
# 0.004 落榜 → 7/3-7/4 實測 75% 點歌（4:12）掉進慢 ~2s 的 wakeless 救援路。
_CTRL_MUSIC_REQUEST_RE = re.compile(
    r'^.{0,4}(播放|點播|放一首|來一首|點一首|想聽)\S',
)


def _score_task(text: str) -> float:
    if _TASK_HARD_RE.search(text): return 0.85
    if _TASK_SOFT_RE.search(text): return 0.50
    return 0.0


def _score_info(text: str, stream_active: bool = False) -> float:
    if stream_active and _INFO_MUSIC_RE.search(text): return 0.80
    if _INFO_GENERAL_RE.search(text):                 return 0.40
    return 0.0


def _score_control(text: str) -> float:
    if _CTRL_SILENCE_RE.search(text): return 1.00
    if _CTRL_MUSIC_RE.search(text):   return 0.90
    if _CTRL_MUSIC_REQUEST_RE.search(text): return 0.85  # 點歌句型（句首錨定）
    if _CTRL_MARMO_RE.search(text):   return 0.75
    return 0.0


# ── Custom channels ───────────────────────────────────────────────────────────

_custom_channels: list[dict] = []


def _load_custom_channels() -> None:
    global _custom_channels
    _custom_channels = []
    if not _CUSTOM_FILE.exists():
        return
    try:
        data = json.loads(_CUSTOM_FILE.read_text(encoding="utf-8"))
        for name, cfg in data.get("channels", {}).items():
            patterns = cfg.get("patterns", [])
            if not patterns:
                continue
            pat = re.compile("|".join(re.escape(p) for p in patterns), re.IGNORECASE)
            _custom_channels.append({
                "name":   name,
                "re":     pat,
                "score":  float(cfg.get("score", 0.80)),
                "weight": float(cfg.get("weight", 0.10)),
            })
        if _custom_channels:
            logger.info(f"📐 [IBA] Loaded {len(_custom_channels)} custom channel(s): "
                        f"{[c['name'] for c in _custom_channels]}")
    except Exception as exc:
        logger.warning(f"⚠️ [IBA] Custom patterns load failed: {exc}")


_load_custom_channels()


def _score_custom(text: str) -> list[tuple[str, float, float]]:
    return [(c["name"], c["score"] if c["re"].search(text) else 0.0, c["weight"])
            for c in _custom_channels]


# ── Module-level helpers (backward compat for utils.py re-export) ─────────────

_FAST_RE = re.compile(rf'^({WAKE_PATTERN})', re.IGNORECASE)
_VOCATIVE = (
    r'[，,、\s]*'
    r'(?:你|幫|來|去|說|告訴|可以|能不能|快|給|接|查|播|開|關'
    r'|怎麼|多少|要|什麼|幫我|告我|解釋|唱|找|停|繼續|重複)'
)
_FORCE_RE   = re.compile(rf'({_FORCE_WAKE_PAT}){_VOCATIVE}', re.IGNORECASE)
_ANY_POS_RE = re.compile(rf'({WAKE_PATTERN})', re.IGNORECASE)
_CTX_TRIGGERS = ["完了", "死定", "救我", "救命", "怎麼辦", "找不到", "迷路",
                 "好無聊", "好累", "炸了", "完蛋"]

# STT 常見的前綴噪音 token（招呼語 / filler）：剝離後再打 _FAST_RE
# 真實 log 證據：「你好, 馬文」「On, 馬文」「Yeah, 馬文」「M. 馬文」「Ai, 馬文」
# 使用者明確在叫 Marvin，只是 STT 把 filler 黏前面，不該被 demote 到 llm_verify。
_LEADING_NOISE_TOKEN_RE = re.compile(
    r'^(?:'
    r'你好|哈囉|哈嘍|喂|哈嘍'
    r'|hi|hey|hello|on|ai|yeah|yo|ok|okay|um|er|ah|oh|no'
    r'|M\.|N\.'
    r')',
    re.IGNORECASE,
)
# 中文單字 filler + 中文/英文標點 + 空白：可連續多個
_LEADING_FILLER_CHARS_RE = re.compile(r'^[嗯啊哦喔呃唉嘿哼欸誒嗨\s.,，、!！?？]+')


def _strip_leading_noise(text: str) -> str:
    """Iteratively strip leading STT noise/filler so embedded wake words surface.

    交替剝離 noise tokens 與 filler chars 直到不再改變，覆蓋「Yeah, hey, 馬文」
    這種多層前綴。不會吃到實際的喚醒詞（noise token list 不含 Marvin / 馬文）。
    """
    prev = None
    while prev != text:
        prev = text
        text = _LEADING_NOISE_TOKEN_RE.sub('', text)
        text = _LEADING_FILLER_CHARS_RE.sub('', text)
    return text


# ── English Marvin STT-hallucination guard (2026-05-20) ──────────────────────
# v4 prompt 把英文 Marvin 視為喚醒，但 STT 在中文語音前會 hallucinate「Marvin,」
# 前綴造成 false wake。對策：fast_intervene 命中英文 Marvin 變體時，若後續
# 無 ≥3 letters 真英文內容，降到 llm_verify 讓 LLM 判 intent。
_ENGLISH_MARVIN_VARIANTS = frozenset({"hey marvin", "oh marvin", "marvin", "marv", "marwen", "mavin"})
_REAL_ENGLISH_WORD_RE = re.compile(r'[a-zA-Z]{3,}')


def _is_english_marvin_match(matched_text: str) -> bool:
    """matched wake word 是英文 Marvin 變體？"""
    return matched_text.lower() in _ENGLISH_MARVIN_VARIANTS


def _looks_like_stt_marvin_hallucination(text_stripped: str, fast_match) -> bool:
    """fast_match 命中英文 Marvin + 後續無真英文內容 → 疑似 STT 幻覺。

    判準：matched 是英文 Marvin 變體 AND rest（後續）無 ≥3 連續英文字母。
    純中文 / 短 token 都不足以證明真英文呼叫，降到 llm_verify。
    """
    matched = fast_match.group(0)
    if not _is_english_marvin_match(matched):
        return False
    rest = text_stripped[fast_match.end():].strip(",. !?？，、 ")
    return not bool(_REAL_ENGLISH_WORD_RE.search(rest))


def pre_filter_speech(raw_text: str) -> dict:
    """Regex fast-path wake detection. Returns {action, text}.

    action values:
      fast_intervene  — sentence-start wake word (lowest false-trigger rate)
      force_intervene — low-ambiguity word ≤2 chars in + vocative suffix
      llm_verify      — wake word elsewhere; send to Track B LLM
      process         — repeated context-trigger keyword
      drop            — no signal
    """
    text = raw_text.strip()
    # P1: 剝離前綴 noise 再打 _FAST_RE，避免「嗯馬文」「你好,馬文」「On, 馬文」
    # 等被 STT noise 黏住前綴的 case 被 demote 到 llm_verify。
    text_stripped = _strip_leading_noise(text)
    fast_match = _FAST_RE.search(text_stripped)
    if fast_match:
        # 2026-05-20: 英文 Marvin STT 幻覺防護 — Marvin 後若無真英文內容
        # （STT 常在純中文前亂插 Marvin），降到 llm_verify 讓 LLM 判 intent
        if _looks_like_stt_marvin_hallucination(text_stripped, fast_match):
            return {"action": "llm_verify", "text": raw_text}
        return {"action": "fast_intervene", "text": raw_text}
    m = _FORCE_RE.search(text)
    if m and m.start() <= 2:
        return {"action": "force_intervene", "text": raw_text}
    if _ANY_POS_RE.search(text):
        return {"action": "llm_verify", "text": raw_text}
    if any(re.search(rf"({re.escape(t)})\s*\1", text) for t in _CTX_TRIGGERS):
        return {"action": "process", "text": raw_text}
    return {"action": "drop"}


def check_cleaned_text_for_wake(cleaned_text: str) -> bool:
    """Track B: re-match wake words on LLM-cleaned text."""
    return bool(_ANY_POS_RE.search(cleaned_text))


# ── WakeDetector (merged WakeSignalFusion + pre_filter logic) ─────────────────

class WakeDetector:
    """
    4-channel + N-custom-channel confidence accumulator with integrated pre-filter.

    Replaces the separate wake_signal_fusion.WakeSignalFusion class.
    Channel weights (voice is anchor; non-voice sum = 0.50):
      voice   0.50 — name detection (pre_filter action + LLM intent)
      task    0.22 — command/task intent (regex)
      info    0.04 — knowledge-gap markers (calibrated down)
      control 0.24 — behaviour / music control (regex)
    """

    VOICE_WEIGHT    = 0.50
    MULTI_THRESHOLD = 0.35

    _DEFAULT_NON_VOICE = {"task": 0.22, "info": 0.04, "control": 0.24}

    # Legacy single-signal constants (kept for stt_cleaner backward compat)
    BASE_THRESHOLD   = 0.70
    CONTEXT_PENALTY  = 0.05
    SPEAKER_PENALTY  = 0.10
    JUST_SPOKE_BONUS = 0.05
    # Echo window：Marvin 剛說完 0-2s 內提高 threshold 擋 TTS 尾音/麥克回授；
    # 蓋過 JUST_SPOKE_BONUS（防 echo 比 follow-up assist 重要）。
    ECHO_PENALTY     = 0.10

    # Expose module-level helpers as static/class attributes for convenience
    pre_filter    = staticmethod(pre_filter_speech)
    check_cleaned = staticmethod(check_cleaned_text_for_wake)

    def __init__(self):
        self.speaker_stats: dict[str, dict] = {}
        self._non_voice = dict(self._DEFAULT_NON_VOICE)
        # D3: follow-up window state
        self._open_until: float = 0.0
        self._open_reason: str = ""
        # D7: self-echo chain guard (max 2 activations per 60 s)
        self._followup_count: int = 0
        self._followup_count_reset_at: float = 0.0
        self._load_stats()
        self._load_calibration()

    # ── Follow-up window API ──────────────────────────────────────────────────

    def temporary_open_window(self, duration: float, reason: str = "followup") -> None:
        """Open the wake gate for N seconds without requiring the wake word.

        D7 guard: >2 activations within 60 s → suppressed (self-echo chain protection).
        """
        now = time.time()
        if now - self._followup_count_reset_at > 60.0:
            self._followup_count = 0
            self._followup_count_reset_at = now
        if self._followup_count >= 2:
            logger.info("🛑 [Follow-Up] Self-echo guard: >2 windows in 60 s — suppressing")
            return
        self._open_until = now + duration
        self._open_reason = reason
        self._followup_count += 1
        logger.info(f"🎧 [Follow-Up] Wake gate open for {duration:.1f}s (reason={reason})")

    def is_open(self) -> bool:
        """Return True while the follow-up window is active."""
        return time.time() < self._open_until

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load_stats(self):
        try:
            if os.path.exists(_STATS_FILE):
                with open(_STATS_FILE, "r", encoding="utf-8") as f:
                    self.speaker_stats = json.load(f)
                logger.info(f"📊 [WakeDetector] Loaded stats for {len(self.speaker_stats)} speakers")
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(f"⚠️ [WakeDetector] Could not load wake_stats.json: {exc}")
            self.speaker_stats = {}

    def _save_stats(self):
        try:
            tmp = _STATS_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.speaker_stats, f, ensure_ascii=False, indent=2)
            os.replace(tmp, _STATS_FILE)
        except OSError as exc:
            logger.error(f"❌ [WakeDetector] Failed to save wake_stats.json: {exc}")

    def _load_calibration(self):
        if not _CALIBRATION_FILE.exists():
            return
        try:
            data = json.loads(_CALIBRATION_FILE.read_text(encoding="utf-8"))
            for k in self._non_voice:
                if k in data.get("non_voice_weights", {}):
                    self._non_voice[k] = float(data["non_voice_weights"][k])
            logger.info(
                f"📐 [IBA] Loaded weights: "
                f"task={self._non_voice['task']:.3f} "
                f"info={self._non_voice['info']:.3f} "
                f"control={self._non_voice['control']:.3f} "
                f"(saved {data.get('saved_at', '?')})"
            )
        except Exception as exc:
            logger.warning(f"⚠️ [IBA] Calibration load failed: {exc}")

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _threshold_delta(self, speaker: str, context_active: bool, marvin_just_spoke: bool) -> float:
        delta = -0.03 if marvin_just_spoke else (0.02 if context_active else 0.0)
        stats = self.speaker_stats.get(speaker, {})
        total = stats.get("false_wakes", 0) + stats.get("true_wakes", 0)
        if total >= 5 and stats.get("false_wakes", 0) / total > 0.4:
            delta += 0.05
        return delta

    @staticmethod
    def _voice_score(action: str, wake_intent: float | None, track: str | None) -> float:
        # Track B = LLM 已被叫來判定意圖；LLM 的 verdict 應優先於 regex 結構訊號。
        # regex 只說「結構上像 wake」，LLM 說「這話是不是真的在叫 Marvin」。
        # 原本只在 wake_intent < 0.65 時用 wake_intent，≥ 0.65 fall through 到
        # 硬編碼 1.0/0.95，造成 LLM mid-range verdict 被吞掉，意圖傳遞失真。
        if track == "B" and wake_intent is not None:
            return wake_intent
        # 2026-05-20 fix: Track=B + wake_intent=None 表示 cleaner LLM 被叫但無 verdict
        # （JSON parse 失敗 / intent=null）→ 若 fall-through 到 fast_intervene 會回 1.0，
        # 等於 regex 唯一證據自動觸發 wake。實測 prod：STT 把「馬文」黏在「李宗盛」前，
        # cleaner JSON parse fail，wake_intent=None → voice=1.0 → false wake 連連。
        # 回低分 0.30（< MULTI_THRESHOLD 0.35），無多 channel 證據時不觸發 wake。
        if track == "B" and wake_intent is None:
            return 0.30
        if action == "fast_intervene":   return 1.0
        if action == "force_intervene":  return 0.95
        if action == "llm_verify" and wake_intent is not None:
            return wake_intent
        if wake_intent is not None:      return wake_intent * 0.6
        return 0.0

    # ── Primary API ───────────────────────────────────────────────────────────

    def multi_channel_decide(
        self,
        action: str,
        wake_intent: float | None,
        text: str,
        speaker: str,
        context_active: bool,
        marvin_just_spoke: bool = False,
        stream_active: bool = False,
        track: str | None = None,
    ) -> tuple[bool, float, dict]:
        """4-channel + custom weighted confidence accumulation.

        Returns (should_wake, total_confidence, channel_scores_dict)
        """
        voice   = self._voice_score(action, wake_intent, track)
        task    = _score_task(text)
        info    = _score_info(text, stream_active)
        control = _score_control(text)
        w       = self._non_voice

        # Track B LLM veto: low intent overrides regex boosts
        if track == "B" and wake_intent is not None and wake_intent < 0.65:
            task = info = control = 0.0

        threshold = round(
            max(0.25, min(0.60,
                self.MULTI_THRESHOLD + self._threshold_delta(speaker, context_active, marvin_just_spoke)
            )), 3
        )
        total = (
            self.VOICE_WEIGHT * voice +
            w["task"]         * task  +
            w["info"]         * info  +
            w["control"]      * control
        )
        scores: dict = {
            "voice":   round(voice,   2),
            "task":    round(task,    2),
            "info":    round(info,    2),
            "control": round(control, 2),
        }
        for name, raw_score, ch_weight in _score_custom(text):
            total += ch_weight * raw_score
            scores[name] = round(raw_score, 2)

        scores["total"]     = round(total, 3)
        scores["threshold"] = threshold
        return total >= threshold, round(total, 3), scores

    def record_outcome(self, speaker: str, was_true_wake: bool):
        s = self.speaker_stats.setdefault(
            speaker, {"false_wakes": 0, "true_wakes": 0, "last_updated": 0.0}
        )
        if was_true_wake:
            s["true_wakes"] += 1
        else:
            s["false_wakes"] += 1
        s["last_updated"] = time.time()
        self._save_stats()
        total = s["true_wakes"] + s["false_wakes"]
        rate  = s["false_wakes"] / total if total else 0.0
        logger.debug(
            f"📊 [WakeDetector] {speaker}: {'✅' if was_true_wake else '❌'} wake "
            f"(total={total}, false_rate={rate:.0%})"
        )

    # ── Calibration ───────────────────────────────────────────────────────────

    def calibrate_from_logs(self, log_dir: str | Path | None = None) -> dict:
        """Learn non-voice weights from daily logs (same algorithm as before)."""
        if log_dir is None:
            log_dir = Path(__file__).parent / "records" / "daily"
        log_dir = Path(log_dir)

        WAKE_RE      = re.compile(rf'({WAKE_PATTERN})', re.IGNORECASE)
        DEBOUNCED_RE = re.compile(
            r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+ - \[([^\]]+)\] \(Debounced\) (.+)"
        )
        pos_scores = {"task": [], "info": [], "control": []}
        neg_scores = {"task": [], "info": [], "control": []}
        n_pos = n_neg = n_ambig = 0

        for log_path in sorted(log_dir.glob("20*.log")):
            if "stt_" in log_path.name:
                continue
            try:
                with open(log_path, encoding="utf-8") as f:
                    for line in f:
                        m = DEBOUNCED_RE.match(line.rstrip())
                        if not m:
                            continue
                        text     = m.group(2)
                        has_wake = bool(WAKE_RE.search(text))
                        t = _score_task(text)
                        i = _score_info(text)
                        c = _score_control(text)
                        if has_wake and (t >= 0.5 or c >= 0.5):
                            for k, v in (("task", t), ("info", i), ("control", c)):
                                pos_scores[k].append(v)
                            n_pos += 1
                        elif not has_wake:
                            for k, v in (("task", t), ("info", i), ("control", c)):
                                neg_scores[k].append(v)
                            n_neg += 1
                        else:
                            n_ambig += 1
            except Exception as exc:
                logger.warning(f"⚠️ [IBA] Error reading {log_path.name}: {exc}")

        if n_pos < 3 or n_neg < 10:
            logger.warning(f"⚠️ [IBA] Insufficient data (pos={n_pos}, neg={n_neg})")
            return {}

        def mean(lst): return sum(lst) / len(lst) if lst else 0.0

        alpha   = 0.8
        new_raw = {}
        disc_log = {}
        for k in ("task", "info", "control"):
            mp   = mean(pos_scores[k])
            mn   = mean(neg_scores[k])
            disc = (mp - mn) / (mp + mn + 0.01)
            disc_log[k] = round(disc, 3)
            new_raw[k]  = max(0.02, self._DEFAULT_NON_VOICE[k] * (1.0 + alpha * disc))

        total_raw   = sum(new_raw.values())
        new_weights = {k: round(v / total_raw * 0.50, 4) for k, v in new_raw.items()}
        self._non_voice = new_weights

        calibration = {
            "saved_at":          time.strftime("%Y-%m-%d %H:%M:%S"),
            "corpus_pos":        n_pos,
            "corpus_neg":        n_neg,
            "corpus_ambig":      n_ambig,
            "discriminability":  disc_log,
            "non_voice_weights": new_weights,
        }
        try:
            _CALIBRATION_FILE.parent.mkdir(parents=True, exist_ok=True)
            _CALIBRATION_FILE.write_text(
                json.dumps(calibration, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            logger.info(
                f"📐 [IBA] Saved weights task={new_weights['task']:.4f} "
                f"info={new_weights['info']:.4f} control={new_weights['control']:.4f}"
            )
        except Exception as exc:
            logger.error(f"❌ [IBA] Failed to save calibration: {exc}")
        return calibration

    # ── Legacy API (stt_cleaner backward compat) ──────────────────────────────

    def get_threshold(self, speaker: str, context_active: bool,
                      marvin_just_spoke: bool = False,
                      marvin_in_echo_window: bool = False) -> float:
        t = self.BASE_THRESHOLD
        # echo window 蓋過其他訊號：TTS 剛結束 0-2s 內，麥克回授/尾音風險最高
        if marvin_in_echo_window:
            t += self.ECHO_PENALTY
        elif marvin_just_spoke:
            t -= self.JUST_SPOKE_BONUS
        elif context_active:
            t += self.CONTEXT_PENALTY
        stats = self.speaker_stats.get(speaker, {})
        total = stats.get("false_wakes", 0) + stats.get("true_wakes", 0)
        if total >= 5 and stats.get("false_wakes", 0) / total > 0.4:
            t += self.SPEAKER_PENALTY
        return round(max(0.5, min(0.95, t)), 2)

    def decide(self, wake_intent: float, speaker: str, context_active: bool,
               marvin_just_spoke: bool = False,
               marvin_in_echo_window: bool = False) -> tuple[bool, float]:
        """Legacy single-signal path. Still used by stt_cleaner."""
        threshold = self.get_threshold(speaker, context_active, marvin_just_spoke,
                                       marvin_in_echo_window=marvin_in_echo_window)
        return wake_intent >= threshold, threshold
