"""策展層：一夜對話 → CurationPlan（選「要講什麼」，與「怎麼呈現」解耦）。

設計（Jack 2026-06-23）：
- 這台的日記，每夜一頁，關台後策展、下次開台發布。
- **忠實**：呈現實際發生的，不做主角輪替。
- **輸出無關呈現**：CurationPlan 是渲染器中立的契約——
  漫畫渲染器現在吃它（Hero 當高潮格 + context 鋪陳）；
  未來**有聲書**渲染器吃同一份（Hero 當高潮橋段 + context 串場旁白）。

Hero 來源（可配置條件）：
- 搶話峰值（同時講長的人數 ≥ 在場人數 × ratio）= 全場最投入的混戰
- 不夠熱 → 退最強話題（heat_score 最高的 10 分鐘段）

純函式、不碰 DB / API。
"""
from __future__ import annotations

import datetime as _dt
import math
from dataclasses import dataclass, field

from diary_comic.crosstalk import crosstalk_peak
from diary_comic.parser import DiaryEntry, heat_score


@dataclass
class HeroMoment:
    kind: str                              # "crosstalk" | "topic"
    ts_str: str
    speakers: list[str]
    lines: list[tuple[str, str]]           # (speaker, text) 爆點/選題那幾句，供渲染器還原
    heat: float
    transcript: list[tuple[str, str]] = field(default_factory=list)  # hero 時間窗完整對話，供故事導演看來龍去脈


@dataclass
class Segment:
    ts_str: str
    summary: str
    speakers: list[str] = field(default_factory=list)


@dataclass
class CurationPlan:
    """一夜的策展結果。渲染器中立——漫畫 / 有聲書共用。"""
    date: str
    cast: list[str]
    hero: HeroMoment
    context: list[Segment]
    source: str                            # hero 來源（可追溯）："crosstalk" | "topic"
    songs: list = field(default_factory=list)  # 當夜使用者主動點歌 [(點歌者, 歌名)]，「點歌台」一格用
    quote: str = ""                            # 今夜馬文最毒一句碎念，當開頁語錄 epigraph


@dataclass
class CuratorConditions:
    min_entries: int = 6                   # 話題段 < 此 → 太短不出
    crosstalk_ratio: float = 0.6           # 搶話人數 ≥ 在場 × 此 → 用搶話當 hero
    context_beats: int = 3


def _needed(present: int, ratio: float) -> int:
    return max(2, math.ceil(present * ratio))


def _spread(pool: list, k: int) -> list:
    """從 pool 等距取 k 個（保序），呈現整夜的質地而非只開頭。"""
    if k <= 0 or not pool:
        return []
    if len(pool) <= k:
        return list(pool)
    step = len(pool) / k
    return [pool[min(len(pool) - 1, int(i * step))] for i in range(k)]


def _ts_str(epoch: float) -> str:
    return _dt.datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")


def _window_transcript(session_rows, center_ts: float, *, before: float = 40.0,
                       after: float = 80.0, max_lines: int = 16, min_len: int = 4):
    """切 center_ts 前後時間窗的實際對話 [(speaker, text)]，濾掉太短的噪音句、依時間序、封頂行數。
    給故事導演看主題來龍去脈用——比爆點窄窗多看前因後果。

    窗重心刻意偏後（before<after）：搶話峰常是主題的爆點、主線在峰值之後展開；
    往前只留短引子，避免吃進峰值前的「上一個子話題」稀釋主題（2026-06-23 Anker 漫畫
    被前面的 SHOKZ 耳機話題污染的修正）。"""
    win = [(s, t) for s, t, ts in session_rows
           if center_ts - before <= ts <= center_ts + after and len((t or "").strip()) >= min_len]
    return win[:max_lines]


def curate(session_rows, topic_entries, conditions: CuratorConditions | None = None,
           song_requests=None):
    """session_rows=(speaker,text,ts_float) 原始逐字稿；topic_entries=10 分鐘摘要 DiaryEntry。
    song_requests=當夜使用者主動點歌 [(點歌者, 歌名)]（給「點歌台」一格）。

    回 CurationPlan 或 None（內容太少）。
    """
    c = conditions or CuratorConditions()
    if len(topic_entries) < c.min_entries:
        return None

    cast = sorted({s for s, _, _ in session_rows}
                  | {sp for e in topic_entries for sp in e.speakers})
    present = len(cast) or 1
    date = topic_entries[-1].ts_str

    peak = crosstalk_peak(session_rows) if session_rows else None
    hero_entry = None
    if peak is not None and len(peak.speakers) >= _needed(present, c.crosstalk_ratio):
        hero = HeroMoment(kind="crosstalk", ts_str=_ts_str(peak.ts),
                          speakers=peak.speakers, lines=peak.lines, heat=peak.heat,
                          transcript=_window_transcript(session_rows, peak.ts))
        source = "crosstalk"
    else:
        hero_entry = max(topic_entries, key=heat_score)
        # 話題段是 10 分鐘摘要：取該段起點往後一窗的實際對話當主題逐字稿
        try:
            _ep = _dt.datetime.strptime(hero_entry.ts_str, "%Y-%m-%d %H:%M:%S").timestamp()
        except ValueError:
            _ep = None
        _tx = _window_transcript(session_rows, _ep, before=0.0, after=600.0) if _ep else []
        hero = HeroMoment(kind="topic", ts_str=hero_entry.ts_str,
                          speakers=list(hero_entry.speakers),
                          lines=[("．".join(hero_entry.speakers) or "群聊", hero_entry.core)],
                          heat=float(heat_score(hero_entry)), transcript=_tx)
        source = "topic"

    pool = [e for e in topic_entries if e is not hero_entry]
    context = [Segment(ts_str=e.ts_str, summary=e.core, speakers=list(e.speakers))
               for e in _spread(pool, c.context_beats)]
    return CurationPlan(date=date, cast=cast, hero=hero, context=context, source=source,
                        songs=list(song_requests or []))
