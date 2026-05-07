import re
import json
import os
import time
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_STATS_FILE        = os.path.join(os.path.dirname(__file__), "wake_stats.json")
_CALIBRATION_FILE  = Path(__file__).parent / "records" / "iba_calibration.json"
_CUSTOM_PATTERNS_FILE = Path(__file__).parent / "records" / "iba_custom_patterns.json"

# ── 4-Channel scoring regexes (defaults, may be overridden by calibration) ──

# Task channel: unambiguous multi-char command phrases
# No ^ anchor — commands follow wake word: "馬文，幫我查" / "馬文你幫我找"
_TASK_HARD_RE = re.compile(
    r'幫我|幫[你他她]?(?:查|播|找|搜|推|告|解|分|帶|念|翻|算)'
    r'|播放|搜尋|推薦|解釋|分析',
    re.IGNORECASE,
)
# Task channel: soft intent starters or trailing question particles
_TASK_SOFT_RE = re.compile(
    r'(?:^|[\s，,])(?:我想|我要|我需要|可以|能不能|可不可以|你可以|你能|你知道)'
    r'|[嗎呢？?]\s*$',
    re.IGNORECASE,
)

# Info channel: knowledge-gap markers
# NOTE: intentionally narrow after calibration showed info fires too much on casual chat
_INFO_GENERAL_RE = re.compile(
    r'(?:什麼意思|怎麼回事|為什麼會|哪裡可以|是什麼東西|誰知道)',
    re.IGNORECASE,
)
# Info channel: music-specific info query (higher score when stream active)
_INFO_MUSIC_RE = re.compile(
    r'這首(?:歌|曲)?(?:叫什麼|是什麼|是誰|叫做|的名字|哪首|叫|叫啥)'
    r'|(?:現在|剛才|正在)(?:播|放|唱)的(?:是|叫)?'
    r'|(?:歌名|歌手|藝人|誰唱|誰寫)(?:是什麼|叫什麼|是誰|叫)',
    re.IGNORECASE,
)

# Control channel: Marvin behaviour control
_CTRL_SILENCE_RE = re.compile(
    r'閉嘴|不要說話|別說了|安靜|暫時靜音|停止說話|先別說',
    re.IGNORECASE,
)
# Control channel: music control (unambiguous compound phrases)
_CTRL_MUSIC_RE = re.compile(
    r'換一首|下一首|跳過|換歌|不要這首'
    r'|停止播放|音樂停|不要播了|關掉音樂|停音樂|音樂關掉'
    r'|暫停音樂|繼續播|繼續音樂|播回來'
    r'|音量大[一點些]?|音量小[一點些]?|大聲一點|小聲一點',
    re.IGNORECASE,
)
# Control channel: delegate to Marmo bot
_CTRL_MARMO_RE = re.compile(
    r'(?:問|叫|用|讓|請|call|問問|叫一下)?(?:marmo|馬某|馬摸|馬墨)',
    re.IGNORECASE,
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
    if _CTRL_MARMO_RE.search(text):   return 0.75
    return 0.0


# ── Custom channel support (loaded from records/iba_custom_patterns.json) ────
# Format: {"channels": {"channel_name": {"patterns": [...], "score": 0.85, "weight": 0.10}}}
# Built-in channels (voice/task/info/control) weights are adjusted by calibration.
# Custom channels occupy weight budget carved from the remaining 0.50 (non-voice).
_custom_channels: list[dict] = []  # [{name, re, score, weight}]


def _load_custom_patterns():
    global _custom_channels
    _custom_channels = []
    if not _CUSTOM_PATTERNS_FILE.exists():
        return
    try:
        data = json.loads(_CUSTOM_PATTERNS_FILE.read_text(encoding="utf-8"))
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
    except Exception as e:
        logger.warning(f"⚠️ [IBA] Custom patterns load failed: {e}")


_load_custom_patterns()


def _score_custom(text: str) -> list[tuple[str, float, float]]:
    """Returns [(name, raw_score, weight), ...]"""
    results = []
    for ch in _custom_channels:
        if ch["re"].search(text):
            results.append((ch["name"], ch["score"], ch["weight"]))
        else:
            results.append((ch["name"], 0.0, ch["weight"]))
    return results


class WakeSignalFusion:
    """
    4-channel + N-custom-channel confidence accumulation → one wake decision.

    Default channel weights (voice is anchor; non-voice sum = 0.50):
      voice   0.50 — name detection (pre_filter action + LLM intent)
      task    0.20 — command/task intent (regex)
      info    0.05 — knowledge-gap markers (calibrated down; fires too broadly)
      control 0.25 — Marvin behaviour / music control (regex)

    Weights are learned from daily logs via calibrate_from_logs().
    Saved to records/iba_calibration.json, loaded on startup.

    Custom channels:
      Add records/iba_custom_patterns.json with format:
        {"channels": {"greeting": {"patterns":["你好","嗨"], "score":0.7, "weight":0.05}}}
      Custom channel weights are added ON TOP of the base non-voice budget.

    Multi-channel threshold (MULTI_THRESHOLD = 0.35):
      Calibrated so LLM intent 0.70 alone still crosses (0.70 × 0.50 = 0.35).
    """

    VOICE_WEIGHT    = 0.50
    MULTI_THRESHOLD = 0.35

    # Default non-voice weights (calibrated from real corpus — see calibrate_from_logs)
    _DEFAULT_NON_VOICE_WEIGHTS = {
        "task":    0.22,   # best discriminator in real data
        "info":    0.04,   # calibrated down: fires on casual questions
        "control": 0.24,   # strong signal when present
    }

    # Legacy single-signal constants (kept for decide() backward compat)
    BASE_THRESHOLD   = 0.70
    CONTEXT_PENALTY  = 0.05
    SPEAKER_PENALTY  = 0.10
    JUST_SPOKE_BONUS = 0.05

    def __init__(self):
        self.speaker_stats: dict[str, dict] = {}
        self._non_voice_weights = dict(self._DEFAULT_NON_VOICE_WEIGHTS)
        self._load_stats()
        self._load_calibration()

    # ── Persistence ──────────────────────────────────────────────────────────

    def _load_stats(self):
        try:
            if os.path.exists(_STATS_FILE):
                with open(_STATS_FILE, "r", encoding="utf-8") as f:
                    self.speaker_stats = json.load(f)
                logger.info(f"📊 [WakeFusion] Loaded stats for {len(self.speaker_stats)} speakers")
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"⚠️ [WakeFusion] Could not load wake_stats.json: {e}")
            self.speaker_stats = {}

    def _save_stats(self):
        try:
            tmp = str(_STATS_FILE) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.speaker_stats, f, ensure_ascii=False, indent=2)
            os.replace(tmp, _STATS_FILE)
        except OSError as e:
            logger.error(f"❌ [WakeFusion] Failed to save wake_stats.json: {e}")

    def _load_calibration(self):
        """Load weight calibration from records/iba_calibration.json if available."""
        if not _CALIBRATION_FILE.exists():
            return
        try:
            data = json.loads(_CALIBRATION_FILE.read_text(encoding="utf-8"))
            weights = data.get("non_voice_weights", {})
            for k in self._non_voice_weights:
                if k in weights:
                    self._non_voice_weights[k] = float(weights[k])
            logger.info(
                f"📐 [IBA Calibration] Loaded weights: "
                f"task={self._non_voice_weights['task']:.3f} "
                f"info={self._non_voice_weights['info']:.3f} "
                f"control={self._non_voice_weights['control']:.3f} "
                f"(saved {data.get('saved_at','?')})"
            )
        except Exception as e:
            logger.warning(f"⚠️ [IBA Calibration] Load failed: {e}")

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _speaker_threshold_delta(self, speaker: str, context_active: bool, marvin_just_spoke: bool) -> float:
        delta = 0.0
        if marvin_just_spoke:
            delta -= 0.03
        elif context_active:
            delta += 0.02
        stats = self.speaker_stats.get(speaker, {})
        total = stats.get("false_wakes", 0) + stats.get("true_wakes", 0)
        if total >= 5:
            false_rate = stats.get("false_wakes", 0) / total
            if false_rate > 0.4:
                delta += 0.05
        return delta

    @staticmethod
    def _voice_score(action: str, wake_intent: float | None, track: str | None) -> float:
        if track == "B" and wake_intent is not None and wake_intent < 0.65:
            return wake_intent
        if action == "fast_intervene":
            return 1.0
        if action == "force_intervene":
            return 0.95
        if action == "llm_verify" and wake_intent is not None:
            return wake_intent
        if wake_intent is not None:
            return wake_intent * 0.6
        return 0.0

    # ── Primary API ──────────────────────────────────────────────────────────

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
        """
        4-channel + custom-channel weighted confidence accumulation.

        Returns (should_wake, total_confidence, channel_scores_dict)
        channel_scores_dict keys: voice, task, info, control,
                                  [custom channel names...], total, threshold
        """
        voice   = self._voice_score(action, wake_intent, track)
        task    = _score_task(text)
        info    = _score_info(text, stream_active)
        control = _score_control(text)
        w       = self._non_voice_weights

        # 🛡️ [LLM Veto Guard] Track B LLM 明確判定「不是呼叫」(intent < 0.65) 時，
        # 停用 task/control 加分通道。否則 "我覺得馬文可以播放音樂" 這類被動提及，
        # 被 LLM 改寫為句首 "馬文，播放..." 後，task 通道反而把它推過門檻。
        # LLM 看過完整語境，它的低信心應覆蓋 regex 通道的加分。
        if track == "B" and wake_intent is not None and wake_intent < 0.65:
            task = info = control = 0.0

        threshold = self.MULTI_THRESHOLD + self._speaker_threshold_delta(
            speaker, context_active, marvin_just_spoke
        )
        threshold = round(max(0.25, min(0.60, threshold)), 3)

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

        # Custom channels add on top of base total
        for name, raw_score, ch_weight in _score_custom(text):
            contrib = ch_weight * raw_score
            total  += contrib
            scores[name] = round(raw_score, 2)

        scores["total"]     = round(total, 3)
        scores["threshold"] = threshold

        return total >= threshold, round(total, 3), scores

    # ── Calibration ──────────────────────────────────────────────────────────

    def calibrate_from_logs(self, log_dir: str | Path | None = None) -> dict:
        """
        Learn non-voice channel weights from daily logs.

        Positive corpus  = texts containing a wake word + task/control pattern
                           (confirmed call-with-intent)
        Negative corpus  = texts with no wake word (casual conversation)
        Ambiguous        = texts with wake word but no clear intent pattern
                           (passive mentions — excluded from calibration)

        Discriminability score per channel k:
          disc_k = (mean_pos_k - mean_neg_k) / (mean_pos_k + mean_neg_k + 0.01)

        New weight_k = max(0.02, base_k × (1 + alpha × disc_k)), normalized so
          sum(task, info, control) = 0.50   [voice stays fixed at 0.50]

        Returns the new weights dict and saves to records/iba_calibration.json.
        """
        try:
            from utils import WAKE_PATTERN
        except ImportError:
            logger.error("❌ [IBA Calibration] Cannot import WAKE_PATTERN from utils")
            return {}

        if log_dir is None:
            log_dir = Path(__file__).parent / "records" / "daily"
        log_dir = Path(log_dir)

        WAKE_RE      = re.compile(rf'({WAKE_PATTERN})', re.IGNORECASE)
        DEBOUNCED_RE = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+ - \[([^\]]+)\] \(Debounced\) (.+)")

        pos_scores  = {"task": [], "info": [], "control": []}
        neg_scores  = {"task": [], "info": [], "control": []}
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
                        text = m.group(2)
                        has_wake = bool(WAKE_RE.search(text))
                        t = _score_task(text)
                        i = _score_info(text)
                        c = _score_control(text)
                        has_intent = (t >= 0.5 or c >= 0.5)

                        if has_wake and has_intent:
                            # Positive: clearly calling Marvin with an action
                            for k, v in (("task", t), ("info", i), ("control", c)):
                                pos_scores[k].append(v)
                            n_pos += 1
                        elif not has_wake:
                            # Negative: casual conversation
                            for k, v in (("task", t), ("info", i), ("control", c)):
                                neg_scores[k].append(v)
                            n_neg += 1
                        else:
                            n_ambig += 1   # passive mention — skip
            except Exception as e:
                logger.warning(f"⚠️ [IBA Calibration] Error reading {log_path.name}: {e}")

        if n_pos < 3 or n_neg < 10:
            logger.warning(f"⚠️ [IBA Calibration] Insufficient data "
                           f"(pos={n_pos}, neg={n_neg}) — keeping current weights")
            return {}

        def mean(lst): return sum(lst) / len(lst) if lst else 0.0

        alpha = 0.8   # how aggressively to shift weights based on discriminability
        new_raw = {}
        disc_log = {}
        for k in ("task", "info", "control"):
            mp = mean(pos_scores[k])
            mn = mean(neg_scores[k])
            disc = (mp - mn) / (mp + mn + 0.01)
            disc_log[k] = round(disc, 3)
            base = self._DEFAULT_NON_VOICE_WEIGHTS[k]
            new_raw[k] = max(0.02, base * (1.0 + alpha * disc))

        # Normalize: task + info + control = 0.50 (to keep voice at 0.50)
        total_raw = sum(new_raw.values())
        new_weights = {k: round(v / total_raw * 0.50, 4) for k, v in new_raw.items()}

        # Apply
        self._non_voice_weights = new_weights

        # Save
        calibration = {
            "saved_at":         time.strftime("%Y-%m-%d %H:%M:%S"),
            "corpus_pos":       n_pos,
            "corpus_neg":       n_neg,
            "corpus_ambig":     n_ambig,
            "discriminability": disc_log,
            "non_voice_weights": new_weights,
        }
        try:
            _CALIBRATION_FILE.parent.mkdir(parents=True, exist_ok=True)
            _CALIBRATION_FILE.write_text(
                json.dumps(calibration, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            logger.info(
                f"📐 [IBA Calibration] Saved weights "
                f"task={new_weights['task']:.4f} info={new_weights['info']:.4f} "
                f"control={new_weights['control']:.4f} | "
                f"disc={disc_log} | pos={n_pos} neg={n_neg}"
            )
        except Exception as e:
            logger.error(f"❌ [IBA Calibration] Failed to save: {e}")

        return calibration

    # ── Legacy API (stt_cleaner backward compat) ──────────────────────────────

    def get_threshold(self, speaker: str, context_active: bool, marvin_just_spoke: bool = False) -> float:
        t = self.BASE_THRESHOLD
        if marvin_just_spoke:
            t -= self.JUST_SPOKE_BONUS
        elif context_active:
            t += self.CONTEXT_PENALTY
        stats = self.speaker_stats.get(speaker, {})
        total = stats.get("false_wakes", 0) + stats.get("true_wakes", 0)
        if total >= 5:
            false_rate = stats.get("false_wakes", 0) / total
            if false_rate > 0.4:
                t += self.SPEAKER_PENALTY
        return round(max(0.5, min(0.95, t)), 2)

    def decide(self, wake_intent: float, speaker: str, context_active: bool, marvin_just_spoke: bool = False) -> tuple[bool, float]:
        """Legacy single-signal path. Still used by stt_cleaner."""
        threshold = self.get_threshold(speaker, context_active, marvin_just_spoke)
        return wake_intent >= threshold, threshold

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
        _total = s["true_wakes"] + s["false_wakes"]
        _rate  = s["false_wakes"] / _total if _total else 0.0
        logger.debug(
            f"📊 [WakeFusion] {speaker}: {'✅ true' if was_true_wake else '❌ false'} wake "
            f"(total={_total}, false_rate={_rate:.0%}, "
            f"threshold={self.get_threshold(speaker, False):.2f})"
        )
