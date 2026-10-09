"""聊天撞歌詞：聊天室剛講的話跟這首歌詞字面撞上時，回傳撞點。純函式，無 IO。"""
import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass

import zhconv

from associative_curation import _pinyin_key
from wake_words_data import words_for

logger = logging.getLogger(__name__)

MIN_CJK_RUN = 4
PINYIN_MIN_LEN = 12
SING_ALONG_RUN = 8
CHAT_QUOTE_MAX = 20
CONTEXT_CHARS = 4
MARVIN_SPEAKERS = frozenset({"Marvin", "馬文"})
GENERIC_PHRASES = frozenset({"是不是可以", "不是可以", "有時候", "是什麼", "不知道", "我們的"})
_GENERIC_PINYIN = tuple(k for k in (_pinyin_key(g) for g in GENERIC_PHRASES) if k)
_NON_TEXT_RE = re.compile(r"[^一-鿿a-z0-9]")
_LRC_PREFIX_RE = re.compile(r"^\s*(\[[\d:.]+\]\s*)+")


@dataclass(frozen=True)
class Collision:
    speaker: str
    chat_quote: str
    lyric_line: str
    matched_key: str
    score: int
    kind: str  # "literal" | "pinyin_only"


def _zh(s: str) -> str:
    return zhconv.convert(s, "zh-hant")


def _norm(s: str) -> str:
    return _NON_TEXT_RE.sub("", _zh(s).lower())


def _cjk_count(s: str) -> int:
    return sum(1 for ch in s if "一" <= ch <= "鿿")


def _common_runs(a: str, b: str, min_len: int) -> list[str]:
    """a 與 b 的所有極大共同子字串，長度 >= min_len。"""
    runs = set()
    prev = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] >= min_len and (i == len(a) or j == len(b) or a[i] != b[j]):
                    runs.add(a[i - cur[j]:i])
        prev = cur
    return sorted(runs, key=len, reverse=True)


def _is_valid_literal(run: str, title_n: str) -> bool:
    if _cjk_count(run) < MIN_CJK_RUN:
        return False
    if any(g in run for g in GENERIC_PHRASES):
        return False
    if title_n and run in title_n:
        return False
    return True


def find_collision(utts, lyrics, *, title="", artist="", exclude=frozenset()) -> "Collision | None":
    """utts: [(speaker, text), ...]（時間順）；lyrics: 整首歌詞純文字（可含 LRC 時間標記）。"""
    if not lyrics or not utts:
        return None

    wake_words_n = [w for w in (_norm(w) for w in words_for("detector")) if w]
    title_n = _norm(title) if title else ""
    artist_n = _norm(artist) if artist else ""

    lines: list[tuple[str, str]] = []
    for raw_line in lyrics.split("\n"):
        line_t = _LRC_PREFIX_RE.sub("", raw_line)
        line_t = _zh(line_t).strip()
        line_n = _norm(line_t)
        if line_n:
            lines.append((line_t, line_n))

    if not lines:
        return None

    best_priority: tuple[int, int] | None = None
    best: Collision | None = None

    for speaker, text in utts:
        if speaker in MARVIN_SPEAKERS:
            continue
        t_zh = _zh(text).strip()
        t_n = _norm(t_zh)
        if not t_n:
            continue
        if any(w in t_n for w in wake_words_n):
            continue
        if (title_n and title_n in t_n) or (artist_n and artist_n in t_n):
            continue

        sing_along = False
        for _line_t, line_n in lines:
            if _common_runs(t_n, line_n, SING_ALONG_RUN):
                sing_along = True
                break
        if sing_along:
            continue

        for line_t, line_n in lines:
            run = None
            kind = None
            for candidate_run in _common_runs(t_n, line_n, MIN_CJK_RUN):
                if _is_valid_literal(candidate_run, title_n):
                    run = candidate_run
                    kind = "literal"
                    break

            if run is None:
                pk_t = _pinyin_key(t_zh)
                pk_l = _pinyin_key(line_t)
                pruns = _common_runs(pk_t, pk_l, PINYIN_MIN_LEN)
                pinyin_run = next((r for r in pruns if not any(g in r for g in _GENERIC_PINYIN)), None)
                if pinyin_run is not None:
                    run = pinyin_run
                    kind = "pinyin_only"

            if run is None:
                continue

            score = len(run)
            matched_key = run

            if len(t_n) <= CHAT_QUOTE_MAX:
                chat_quote = t_n
            elif kind == "literal":
                i = t_n.index(run)
                chat_quote = t_n[max(0, i - CONTEXT_CHARS): i + len(run) + CONTEXT_CHARS]
            else:
                chat_quote = t_n[:CHAT_QUOTE_MAX]

            lyric_line = line_t

            if chat_quote in exclude or lyric_line in exclude:
                continue

            priority = (0 if kind == "literal" else 1, -score)
            if best_priority is None or priority < best_priority:
                best_priority = priority
                best = Collision(
                    speaker=speaker,
                    chat_quote=chat_quote,
                    lyric_line=lyric_line,
                    matched_key=matched_key,
                    score=score,
                    kind=kind,
                )

    if best is None:
        return None
    logger.info(
        "🎯 [Collision] kind=%s score=%d speaker=%s key=%s line=%s",
        best.kind, best.score, best.speaker, best.matched_key, best.lyric_line,
    )
    return best


COLLISION_MIN_GAP_SONGS = 2
COLLISION_SPEAKER_COOLDOWN_S = 1800.0
REACTION_WINDOW_S = 60.0


def reaction_counts(entries: Iterable[dict], t0: float, window_s: float = REACTION_WINDOW_S) -> tuple[int, int]:
    """口白播出時刻 t0 前後各 window_s 秒的在場者發言數（不含 Marvin）。"""
    pre = post = 0
    for e in entries:
        if e.get("speaker") in MARVIN_SPEAKERS:
            continue
        ts = e.get("timestamp")
        if ts is None:
            continue
        if t0 - window_s <= ts < t0:
            pre += 1
        elif t0 <= ts < t0 + window_s:
            post += 1
    return pre, post


def utts_since(entries: Iterable[dict], since_ts: float) -> list[tuple[str, str]]:
    return [(e["speaker"], e["text"]) for e in entries if e["timestamp"] >= since_ts]


def filter_consented(utts: list[tuple[str, str]], is_consented: Callable[[str], bool]) -> list[tuple[str, str]]:
    return [(s, t) for s, t in utts if is_consented(s)]


def verbatim_ok(text: str, c: "Collision") -> bool:
    t = _norm(text)
    return _norm(c.chat_quote) in t and _norm(c.lyric_line) in t


def collision_template(c: "Collision", title: str) -> str:
    return f"{c.speaker}剛才說「{c.chat_quote}」，這首剛好唱到「{c.lyric_line}」。《{title}》"


class CollisionLedger:
    """本程序記憶體內的撞點冷卻（bot 重啟即重置）。"""

    def __init__(self, min_gap_songs: int = COLLISION_MIN_GAP_SONGS,
                 speaker_cooldown_s: float = COLLISION_SPEAKER_COOLDOWN_S):
        self._min_gap = min_gap_songs
        self._cooldown = speaker_cooldown_s
        self._songs_since = min_gap_songs
        self._speaker_last: dict[str, float] = {}
        self._used: set[str] = set()

    def tick_song(self) -> None:
        self._songs_since += 1

    def ready(self) -> bool:
        return self._songs_since >= self._min_gap

    def blocked_speakers(self, now: float) -> set[str]:
        return {s for s, t in self._speaker_last.items() if now - t < self._cooldown}

    def exclude(self) -> frozenset[str]:
        return frozenset(self._used)

    def mark(self, c: "Collision", now: float) -> None:
        self._songs_since = 0
        self._speaker_last[c.speaker] = now
        self._used.add(c.chat_quote)
        self._used.add(c.lyric_line)
