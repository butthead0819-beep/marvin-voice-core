"""讀空氣主題歌單 — Step 1 (theme brief, 純) + Step 2 (LLM 策展 call)。

設計：~/.gstack/projects/butthead0819-beep-marvin-voice-core/jackhuang-main-design-20260624-192239.md

把自動點歌從單首補位升級成「策展一張有主題的歌單」：主題＝今晚對話主題 + 團體口味，
LLM 選 5-8 首『合主題、合口味、盡量新鮮』的歌並給每首選歌理由（增添日記風味）。

本模組只做 Step 1-2（離線可驗、不碰播放）：
- gather_theme_brief：純函式。近窗對話主題核心句 + 口味指紋 → ThemeBrief；無共識 → None。
- build_curation_prompt / parse_themed_set：純函式（prompt 組裝 / JSON 解析）。
- curate_themed_set：協調器，call_fn 注入（預設 llm_pool.call_paid_review）→ 走 bus 付費池。

下游（resolve + 品質閘 + 成塊入隊 + 日記）是後續 step，不在此檔。
"""
from __future__ import annotations

import datetime as _dt
import json
import re
from dataclasses import dataclass


@dataclass
class ThemeBrief:
    cores: list[str]          # 近窗對話主題摘要核心句（時間序）
    core_artists: list[str]   # 團體核心歌手
    language_label: str       # 主導語言（如 "華語"）
    members: list[str]        # 在場者


@dataclass
class ThemedPick:
    artist: str
    song: str
    reason: str


@dataclass
class ThemedSet:
    theme_title: str
    picks: list[ThemedPick]


def gather_theme_brief(summary_entries, taste_fp: dict, members: list[str], *,
                       now: float, window_hours: float = 3.0,
                       min_cores: int = 2) -> ThemeBrief | None:
    """純函式。從近 window_hours 的對話主題摘要核心句 + 口味指紋組 ThemeBrief。

    summary_entries：[(ts_str, core)] 或有 .ts_str/.core 的物件（日記 DiaryEntry）。
    近窗可用核心句 < min_cores → 回 None（無可偵測主題 → caller fallback 單首 autopilot）。
    """
    cutoff = now - window_hours * 3600.0
    cores: list[str] = []
    for e in summary_entries:
        if isinstance(e, tuple):
            ts_str, core = e[0], e[1]
        else:
            ts_str, core = getattr(e, "ts_str", None), getattr(e, "core", None)
        try:
            ts = _dt.datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").timestamp()
        except (ValueError, TypeError):
            continue
        if ts >= cutoff and core and str(core).strip():
            cores.append(str(core).strip())
    if len(cores) < min_cores:
        return None
    core_artists = [a for a, _ in (taste_fp.get("core_artists") or [])][:8]
    lang = taste_fp.get("language") or {}
    language_label = max(lang, key=lang.get) if lang else "華語"
    return ThemeBrief(cores=cores[-12:], core_artists=core_artists,
                      language_label=language_label, members=list(members))


_CURATION_SYS = (
    "你是一位懂這群人的 DJ。根據他們今晚聊的主題 + 平常的口味，策展一張有主題的歌單。\n"
    "規則：\n"
    "1) 先抓今晚對話的『主題情緒』，給歌單一個有味道的名字（≤14 字）。\n"
    "2) 選歌要同時：貼合那個主題情緒、合這群人的口味歌手/語言、且盡量是清單外的『新鮮』歌"
    "（別只挑最大熱門，挖一點他們會喜歡但沒常播的）。\n"
    "3) 歌單要連貫——同一種年代/情緒/語言掛在一起，不是各自為政。\n"
    "4) 每首給一句『選歌理由』：像朋友跟你說為什麼放這首、扣回今晚的主題，短、有人味。\n"
    "5) 只選真實存在的歌（真歌手＋真歌名），不確定就不要編。\n"
    '只回 JSON：{"theme_title":"…","picks":[{"artist":"…","song":"…","reason":"…"}]}'
)


def build_curation_prompt(brief: ThemeBrief, exclude_titles: list[str], *,
                          set_size: int = 6) -> tuple[str, str]:
    """純函式 → (system, user)。把對話主題、口味、排除清單組進 user prompt。"""
    cores = "\n".join(f"- {c}" for c in brief.cores)
    artists = "、".join(brief.core_artists) or "（未知）"
    excl = "、".join(exclude_titles[:40]) or "（無）"
    user = (
        f"今晚在場：{'、'.join(brief.members) or '群聊'}\n"
        f"他們今晚聊的主題（對話摘要核心句，時間序）：\n{cores}\n\n"
        f"這群人的核心口味歌手：{artists}\n主導語言：{brief.language_label}\n\n"
        f"已經放過/不要再選的歌（歌名）：{excl}\n\n"
        f"請策展一張 {set_size} 首的主題歌單，回 JSON。"
    )
    return _CURATION_SYS, user


def parse_themed_set(resp: str, *, max_picks: int = 8) -> ThemedSet | None:
    """純函式。LLM JSON → ThemedSet；空/壞/無 title/無 picks → None。"""
    if not resp:
        return None
    m = re.search(r"\{.*\}", resp, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except Exception:
        return None
    title = str(d.get("theme_title", "")).strip()
    raw = d.get("picks") if isinstance(d.get("picks"), list) else []
    picks: list[ThemedPick] = []
    for p in raw:
        if not isinstance(p, dict):
            continue
        a = str(p.get("artist", "")).strip()
        s = str(p.get("song", "")).strip()
        r = str(p.get("reason", "")).strip()
        if a and s:
            picks.append(ThemedPick(artist=a, song=s, reason=r))
    if not title or not picks:
        return None
    return ThemedSet(theme_title=title, picks=picks[:max_picks])


async def curate_themed_set(brief: ThemeBrief | None, exclude_titles: list[str], *,
                            call_fn=None, set_size: int = 6) -> ThemedSet | None:
    """協調：build prompt → call LLM（注入 call_fn）→ parse。

    brief=None / LLM 失敗 / 解析失敗 → 回 None（caller fallback 單首 autopilot，不中斷音樂）。
    call_fn 預設 llm_pool.call_paid_review（走 bus 付費池、JSON mode、thinking off）。
    """
    if brief is None:
        return None
    if call_fn is None:
        from llm_pool import call_paid_review
        call_fn = call_paid_review
    system, user = build_curation_prompt(brief, exclude_titles, set_size=set_size)
    try:
        resp = await call_fn(user, system=system)
    except Exception:
        return None
    return parse_themed_set(resp)
