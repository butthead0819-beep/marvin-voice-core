"""
AtmosphereTracker — 即時讀空氣模組

從 STT 語料串流提取話題標籤與說話者情緒狀態，
產出 AtmosphereSnapshot 供 GeminiRouter 注入系統提示。
"""

import re
import time
import logging
from collections import deque, defaultdict
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ── 話題關鍵字表（與 analyze_speech_dna.py 保持一致）────────────────────────────
TOPIC_KEYWORDS: dict[str, list[str]] = {
    "drinking": ["喝酒", "買酒", "啤酒", "紅酒", "威士忌", "高粱", "乾杯", "喝一杯", "醉了", "喝醉"],
    "gaming":   ["遊戲", "打電動", "minecraft", "麥塊", "ps5", "switch", "開局", "掉線", "上分", "電競",
                 "boss", "技能", "刷怪", "副本", "公會", "掛機", "打牌"],
    "work":     ["工作", "老闆", "加班", "上班", "客戶", "開會", "專案", "薪水", "面試", "辭職", "同事",
                 "deadline", "下班", "出差"],
    "tech":     ["電腦", "手機", "網路", "wifi", "系統", "更新", "app", "程式", "bug", "軟體", "硬體",
                 "iphone", "android", "伺服器", "api", "youtube", "ig"],
    "food":     ["吃飯", "吃什麼", "點餐", "火鍋", "燒烤", "便當", "飲料", "外送", "好吃", "餐廳",
                 "宵夜", "泡麵", "咖啡", "奶茶"],
    "family":   ["爸", "媽", "爸媽", "老婆", "老公", "小孩", "家裡", "家人", "弟弟", "妹妹", "哥哥",
                 "姊姊", "爺爺", "奶奶"],
    "music":    ["歌", "音樂", "馬文播", "播放", "歌手", "專輯", "演唱會", "kkbox", "spotify"],
    "casual":   [],  # fallback，永遠匹配
}

# 說話者情緒狀態標籤
MOOD_STRESSED  = "stressed"   # 談壓力話題，句長↑
MOOD_DRINKING  = "drinking"   # 飲酒關鍵字出現
MOOD_LOW       = "low"        # 句長↓ 且 filler↓（沉默低落）
MOOD_ENERGETIC = "energetic"  # 笑聲↑
MOOD_NORMAL    = "normal"

# 滾動窗口長度（秒）
_WINDOW_SEC = 10 * 60

_LAUGH_RE    = re.compile(r"哈{2,}|笑死|lol|笑{2,}", re.IGNORECASE)
_FILLER_SET  = frozenset(["那個", "就是", "然後", "啊", "喔", "欸", "嗯", "嘿", "這個", "啦", "嘛"])


# ── 資料結構 ──────────────────────────────────────────────────────────────────

@dataclass
class _Entry:
    speaker:      str
    text:         str
    ts:           float
    topic:        str
    char_count:   int
    has_laugh:    bool
    filler_count: int


@dataclass
class AtmosphereSnapshot:
    dominant_topic:   str   = "casual"
    topic_confidence: float = 0.0
    room_mood:        str   = "放鬆閒聊"
    speaker_states:   dict  = field(default_factory=dict)
    recent_topics:    list  = field(default_factory=list)
    ts:               float = field(default_factory=time.time)

    def to_prompt_str(self) -> str:
        """產生注入系統提示的中文摘要行，空房間時回傳空字串。"""
        if not self.speaker_states:
            return ""

        parts: list[str] = []
        if self.dominant_topic != "casual":
            parts.append(f"當前話題：{self.dominant_topic}（信心 {self.topic_confidence:.0%}）")
        else:
            parts.append("當前話題：閒聊")
        parts.append(f"氣氛：{self.room_mood}")

        stressed = [s for s, m in self.speaker_states.items() if m == MOOD_STRESSED]
        if stressed:
            parts.append(f"{'、'.join(stressed)} 進入壓力模式（句長↑）")

        drinking = [s for s, m in self.speaker_states.items() if m == MOOD_DRINKING]
        if drinking:
            parts.append(f"{'、'.join(drinking)} 可能在喝酒")

        low = [s for s, m in self.speaker_states.items() if m == MOOD_LOW]
        if low:
            parts.append(f"{'、'.join(low)} 目前話少、能量低落")

        energetic = [s for s, m in self.speaker_states.items() if m == MOOD_ENERGETIC]
        if energetic:
            parts.append(f"{'、'.join(energetic)} 情緒高昂")

        return "[當前氣氛] " + " | ".join(parts)


# ── 輔助函式 ──────────────────────────────────────────────────────────────────

def _tag_topic(text: str, kwds: dict | None = None) -> str:
    if kwds is None:
        kwds = TOPIC_KEYWORDS
    text_lower = text.lower()
    for topic, kws in kwds.items():
        if topic == "casual":
            continue
        if any(kw in text_lower for kw in kws):
            return topic
    return "casual"


def _count_fillers(text: str) -> int:
    return sum(text.count(f) for f in _FILLER_SET)


# ── 主類別 ────────────────────────────────────────────────────────────────────

class AtmosphereTracker:
    """
    維護 10 分鐘滾動窗口，對每條 STT 語料做輕量話題標記。
    與 suki_memory speech_dna baseline 做差值分析後，
    回傳 AtmosphereSnapshot 供 GeminiRouter 注入提示。
    """

    def __init__(self, memory_manager=None):
        self._window: deque[_Entry] = deque()
        self._memory = memory_manager
        self._dna_cache: dict[str, dict] = {}
        self._topic_keywords: dict[str, list[str]] = {k: list(v) for k, v in TOPIC_KEYWORDS.items()}
        if memory_manager is not None:
            self._load_calibration()

    # ── 公開 API ──────────────────────────────────────────────────────────────

    def add_utterance(self, speaker: str, text: str, ts: float = None):
        if ts is None:
            ts = time.time()
        text = text.strip()
        if not text:
            return
        self._window.append(_Entry(
            speaker      = speaker,
            text         = text,
            ts           = ts,
            topic        = _tag_topic(text, self._topic_keywords),
            char_count   = len(text.replace(" ", "")),
            has_laugh    = bool(_LAUGH_RE.search(text)),
            filler_count = _count_fillers(text),
        ))
        self._prune()

    def get_snapshot(self) -> AtmosphereSnapshot:
        self._prune()
        entries = list(self._window)
        if not entries:
            return AtmosphereSnapshot()

        # 1. 話題分佈
        topic_counts: dict[str, int] = defaultdict(int)
        recent_topics: list[str] = []
        for e in reversed(entries):
            topic_counts[e.topic] += 1
            if e.topic not in recent_topics:
                recent_topics.insert(0, e.topic)
            if len(recent_topics) >= 3:
                break

        non_casual = sorted(
            [(t, c) for t, c in topic_counts.items() if t != "casual"],
            key=lambda x: -x[1],
        )
        if non_casual:
            dominant_topic, dom_count = non_casual[0]
            topic_confidence = round(dom_count / len(entries), 2)
        else:
            dominant_topic   = "casual"
            topic_confidence = 1.0

        # 2. 說話者情緒狀態
        by_speaker: dict[str, list[_Entry]] = defaultdict(list)
        for e in entries:
            by_speaker[e.speaker].append(e)

        speaker_states = {
            sp: self._classify_speaker(sp, ses, dominant_topic)
            for sp, ses in by_speaker.items()
        }

        # 3. 整體房間氣氛
        room_mood = self._classify_room(speaker_states, dominant_topic)

        return AtmosphereSnapshot(
            dominant_topic   = dominant_topic,
            topic_confidence = topic_confidence,
            room_mood        = room_mood,
            speaker_states   = speaker_states,
            recent_topics    = recent_topics[:3],
            ts               = time.time(),
        )

    def invalidate_cache(self, speaker: str = None):
        if speaker:
            self._dna_cache.pop(speaker, None)
        else:
            self._dna_cache.clear()

    def _load_calibration(self):
        """從 memory_manager.get_atmosphere_calibration() 讀取補充關鍵字，
        merge 進 _topic_keywords（只新增不覆蓋）。"""
        try:
            additions: dict[str, list[str]] = self._memory.get_atmosphere_calibration()
            merged = 0
            for topic, kws in additions.items():
                if topic not in self._topic_keywords:
                    continue
                existing = self._topic_keywords[topic]
                for kw in kws:
                    if kw and kw not in existing:
                        existing.append(kw)
                        merged += 1
            if merged:
                logger.info(f"🌡  [AtmosphereTracker] 載入校正關鍵字 {merged} 個")
        except Exception as e:
            logger.debug(f"[AtmosphereTracker] 校正載入略過: {e}")

    # ── 私有方法 ──────────────────────────────────────────────────────────────

    def _prune(self):
        cutoff = time.time() - _WINDOW_SEC
        while self._window and self._window[0].ts < cutoff:
            self._window.popleft()

    def _get_baseline(self, speaker: str) -> dict:
        if speaker in self._dna_cache:
            return self._dna_cache[speaker]
        dna: dict = {}
        if self._memory:
            try:
                profile = self._memory.get_player(speaker)
                dna = profile.get("speech_dna", {}) if profile else {}
            except Exception:
                pass
        self._dna_cache[speaker] = dna
        return dna

    def _classify_speaker(self, speaker: str, ses: list[_Entry], dominant_topic: str) -> str:
        if any(e.topic == "drinking" for e in ses):
            return MOOD_DRINKING

        baseline = self._get_baseline(speaker)
        b_chars  = float(baseline.get("avg_chars",    15.0))
        b_filler = float(baseline.get("filler_rate",   0.4))
        b_laugh  = float(baseline.get("laugh_rate",   0.02))

        recent = ses[-10:]
        if not recent:
            return MOOD_NORMAL

        avg_chars    = sum(e.char_count   for e in recent) / len(recent)
        filler_rate  = sum(e.filler_count for e in recent) / len(recent)
        laugh_rate   = sum(1 for e in recent if e.has_laugh) / len(recent)

        chars_delta  = avg_chars   - b_chars
        filler_delta = filler_rate - b_filler
        laugh_delta  = laugh_rate  - b_laugh

        if chars_delta >= 5 and dominant_topic in {"work", "tech", "money"}:
            return MOOD_STRESSED
        if laugh_delta >= 0.05:
            return MOOD_ENERGETIC
        if chars_delta <= -5 and filler_delta <= -0.15:
            return MOOD_LOW
        return MOOD_NORMAL

    def _classify_room(self, speaker_states: dict[str, str], dominant_topic: str) -> str:
        mood_counts: dict[str, int] = defaultdict(int)
        for m in speaker_states.values():
            mood_counts[m] += 1

        n = max(1, len(speaker_states))
        if mood_counts[MOOD_DRINKING]:
            return "飲酒作樂"
        if mood_counts[MOOD_STRESSED] >= max(1, n // 2):
            return "認真討論"
        if mood_counts[MOOD_ENERGETIC] >= max(1, n // 2):
            return "熱烈互動"
        if mood_counts[MOOD_LOW] >= max(1, n // 2):
            return "低能量"
        if dominant_topic in {"work", "tech"}:
            return "認真討論"
        return "放鬆閒聊"
