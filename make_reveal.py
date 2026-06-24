"""夜晚回放秀 v0.1 — 離線靜態 EKG PNG。

目的：驗證「crosstalk 峰值選的時刻值不值得看」。先用最便宜的靜態圖證明選材假設，
再決定要不要燒 v0.2 影片管線（動畫 + TTS 旁白）。

完全離線、不碰 bot 主進程、不碰 Sink.write 熱路徑。

資料流：
  marvin.db transcripts ─_db_rows(start,end)→ rows[(speaker,text,ts)]
    → curate_reel(rows)                    自動選最熱窗 + 過品質閘的引言
        ├─ None（平淡夜 / 全糊字）          → 呼叫端退既有靜態海報、不出圖
        └─ Reel                            → render_ekg_png + night_reel.json

刻意限制（見 design doc / eng review）：
  - 自動選、不手挑（v0.1 目的就是量自動選命中率）。
  - 引言過品質閘：is_whisper_hallucination + 重複字（嗯嗯嗯嗯）/純標點 過濾。
  - 只渲最熱固定窗（pick_hottest_window），不線性壓整晚。
  - v0.1 內部驗證、不對外發（匿名化推 v0.2，見 TODOS.md）。
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import sqlite3
from dataclasses import dataclass, field

from diary_comic.crosstalk import (
    _crosstalk_events,
    activity_track,
)

try:
    from diary_comic.curator import _BOT_NAMES
except Exception:
    _BOT_NAMES = {"marvin", "馬文", "馬汶"}

try:
    from utils import is_whisper_hallucination
except Exception:                       # 離線/路徑問題時退化成只靠重複字閘
    def is_whisper_hallucination(text: str, prompt: str) -> bool:
        return False

MIN_QUOTE_LEN = 6        # 引言最短字數（短於此資訊量不足）
MAX_TOPICS = 5           # 每晚最多標幾個「有主題」紅點
BAR_MIN_S = 300.0        # EQ 每格最短 5 分鐘
BAR_MAX_S = 600.0        # EQ 每格最長 10 分鐘
BAR_TARGET = 20          # 目標格數（在 5-10 分鐘間自適應）


def _bar_bin_s(rows) -> float:
    """依整晚時長自適應每格秒數，夾在 5-10 分鐘（目標約 BAR_TARGET 格）。"""
    if len(rows) < 2:
        return BAR_MIN_S
    span = rows[-1][2] - rows[0][2]
    return max(BAR_MIN_S, min(BAR_MAX_S, span / BAR_TARGET))


@dataclass
class Reel:
    window: tuple[float, float]                 # 整晚 (start_ts, end_ts)
    activity_track: list[tuple[float, float]]   # 底層曲線＝發言密度（句/bin），＝「熱鬧」
    topic_peaks: list[tuple[float, float, str]]  # 「有主題」紅點 (ts, heat, 引言)，≤MAX_TOPICS
    songs: list[tuple[float, str, str]]         # 點歌標記 (ts, 點歌者, 歌名)
    hero_ts: float                              # 最熱有主題搶話事件（freeze + 引言來源）
    hero_heat: float
    speakers: list[str]
    quote: str
    topic_labels: list[str] = field(default_factory=list)  # LLM 精煉的≤8字主題（空＝用原引言）


def _quote_quality_ok(text: str) -> bool:
    """引言品質閘：擋 STT 糊字，避免單句放大零容錯地選到垃圾。

    擋四類：太短 / Whisper 幻覺（逗號重複片語）/ 重複字（嗯嗯嗯嗯、哈哈哈哈）/ 純標點。
    """
    t = (text or "").strip()
    if len(t) < MIN_QUOTE_LEN:
        return False
    if is_whisper_hallucination(t, ""):
        return False
    core = re.sub(r"\s", "", t)
    if len(set(core)) <= 2:                 # 嗯嗯嗯嗯 / 哈哈哈哈 類重複字
        return False
    if not re.search(r"[一-鿿A-Za-z0-9]", t):   # 純標點/符號
        return False
    return True


def _human_rows(rows):
    """濾掉 bot 自己的句（TTS 被 STT 轉錄回來），不讓 Marvin 灌大發言密度。"""
    return [(s, t, ts) for (s, t, ts) in rows
            if (s or "").strip().lower() not in _BOT_NAMES]


def _event_quote(ev) -> str | None:
    """事件「有主題」＝至少一句過品質閘；回那句乾淨引言，否則 None（純嗨/糊字）。"""
    for _spk, txt in ev.lines:
        if _quote_quality_ok(txt):
            return txt
    return None


def curate_reel(rows, song_requests=None, bin_s: float | None = None,
                max_topics: int = MAX_TOPICS) -> Reel | None:
    """rows → Reel，或 None（無人說話 / 無有主題搶話 → 退靜態海報）。

    底層＝整晚發言密度 EQ 長條（每格 5-10 分鐘自適應，由下往上綠轉紅）。紅點只標「有主題」
    的搶話（事件含過品質閘的句，濾掉純嗨/糊字），每晚最多 max_topics 個、取最熱。hero＝
    最熱有主題事件（freeze+引言）。songs＝(ts,點歌者,歌名)，裁到整晚窗內、標在時間軸上。
    """
    rows = _human_rows(rows)
    if bin_s is None:
        bin_s = _bar_bin_s(rows)
    act = activity_track(rows, bin_s)
    if not act:                             # 整晚沒人說話
        return None
    window = (act[0][0], act[-1][0] + bin_s)
    start, end = window

    # 只留「有主題」事件（含乾淨引言），依 heat 取前 max_topics 個當紅點
    topic_events = [(ev, q) for ev in _crosstalk_events(rows)
                    if (q := _event_quote(ev)) is not None]
    if not topic_events:                    # 整晚無有主題搶話 → 退海報
        return None
    topic_events.sort(key=lambda eq: -eq[0].heat)
    top = topic_events[:max_topics]
    topic_peaks = [(ev.ts, ev.heat, q) for ev, q in top]   # 帶上每個主題的引言
    hero_ev, hero_q = top[0]                 # 最熱者當 hero

    songs = sorted((ts, u, t) for (ts, u, t) in (song_requests or [])
                   if start <= ts <= end)

    return Reel(window=window, activity_track=act, topic_peaks=topic_peaks,
                songs=songs, hero_ts=hero_ev.ts, hero_heat=hero_ev.heat,
                speakers=hero_ev.speakers, quote=hero_q)


def night_reel_dict(reel: Reel, date_label: str = "") -> dict:
    """Reel → 可序列化 dict（debug dump，非治理契約：無 schema_version/cast）。"""
    from diary_comic.song_requests import clean_title
    return {
        "date": date_label,
        "window": list(reel.window),
        "activity_track": [{"t": t, "n": n} for t, n in reel.activity_track],
        "topic_peaks": [
            {"ts": ts, "heat": h, "quote": q,
             "label": reel.topic_labels[i] if i < len(reel.topic_labels) else ""}
            for i, (ts, h, q) in enumerate(reel.topic_peaks)],
        "songs": [{"ts": ts, "by": u, "title": clean_title(t)} for ts, u, t in reel.songs],
        "hero": {
            "ts": reel.hero_ts,
            "heat": reel.hero_heat,
            "speakers": reel.speakers,
            "quote": reel.quote,
        },
    }


# ── 靜態 EKG 渲染（Pillow）── 音響等化器風格：由下往上 LED、綠轉紅色階 ──────
#   ┌──────────────────────────────────────────┐
#   │  [date]        ▽   ▽▼(hero)   ▽            │  ← 有主題搶話標記（▼=hero）
#   │              ▔ ▔   ▔   ▔ ▔                 │  ← 紅(滿格)
#   │            ▔ █ ▔ █ ▔ █ █ ▔                 │  ← 黃
#   │   █ ▔ █  █ █ █ █ █ █ █ █ █ █  ▔            │  ← 綠(底)   ← 每格 5-10 分鐘
#   │  「<quote>」 — speakers     ♪  ♪    ♪       │  ← 點歌 ♪
#   └──────────────────────────────────────────┘
_W, _H = 1200, 675           # 16:9
_MARGIN = 80
_BG = (18, 18, 22)
_MARK = (235, 110, 110)      # 有主題搶話標記
_HERO = (255, 220, 120)      # hero
_SONG = (120, 210, 150)      # 點歌 ♪
_TXT = (235, 235, 235)
_CELL_ROWS = 18              # 每根 EQ 長條的 LED 格數
_CELL_GAP = 3               # LED 格之間的縫
_BAR_PAD = 0.16             # 長條左右留白比例
# 固定刻度：滿格＝12 句/分鐘（跨晚共用，才能比高/低話量；最高觀測 11.5）。
# 不是每晚各自縮放——安靜夜整片偏綠偏矮、熱鬧夜偏紅偏高，顏色＝絕對話量。
_FULL_RATE = 12.0            # 句/分鐘 = 滿格


def _eq_color(frac: float) -> tuple[int, int, int]:
    """LED 色階：底(0.0)綠 → 中(0.5)黃 → 頂(1.0)紅。"""
    frac = max(0.0, min(1.0, frac))
    g, y, r = (60, 200, 90), (235, 205, 70), (235, 70, 70)
    if frac < 0.5:
        a, b, t = g, y, frac / 0.5
    else:
        a, b, t = y, r, (frac - 0.5) / 0.5
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def render_ekg_png(reel: Reel, out_path: str) -> str:
    from PIL import Image, ImageDraw

    from diary_comic.layout import _load_font

    img = Image.new("RGB", (_W, _H), _BG)
    d = ImageDraw.Draw(img)

    start, end = reel.window
    span = max(end - start, 1e-6)
    plot_top, plot_bot = 150, 410
    x0, x1 = _MARGIN, _W - _MARGIN
    track = reel.activity_track
    step = (track[1][0] - track[0][0]) if len(track) >= 2 else span
    bin_min = max(step / 60.0, 1e-6)         # 每格分鐘數（5-10）

    def _x(t):
        return x0 + (t - start) / span * (x1 - x0)

    # 固定刻度格線：滿格 = _FULL_RATE 句/分（跨晚共用，不每晚縮放）
    plot_h = plot_bot - plot_top
    cell_h = plot_h / _CELL_ROWS
    d.line([(x0, plot_top), (x1, plot_top)], fill=(70, 70, 78), width=1)
    f_axis = _load_font(20)
    d.text((x1 + 6, plot_top - 10), f"{_FULL_RATE:.0f}", font=f_axis, fill=(120, 120, 128))
    d.text((x1 + 6, plot_bot - 10), "0", font=f_axis, fill=(120, 120, 128))
    d.text((x1 + 6, (plot_top + plot_bot) // 2 - 24), "句\n分", font=f_axis, fill=(120, 120, 128))

    # EQ 長條：每格一根，由下往上點亮 LED，綠→紅。高度＝該段「句/分」/ 固定滿格
    for t, n in track:
        left, right = _x(t), _x(t + step)
        pad = (right - left) * _BAR_PAD
        bx0, bx1 = left + pad, right - pad
        rate = n / bin_min                   # 句/分鐘（跟格寬無關，可跨晚比）
        lit = round(min(rate / _FULL_RATE, 1.0) * _CELL_ROWS)
        for k in range(lit):
            col = _eq_color(k / (_CELL_ROWS - 1))
            cy_bot = plot_bot - k * cell_h
            cy_top = plot_bot - (k + 1) * cell_h + _CELL_GAP
            d.rectangle([bx0, cy_top, bx1, cy_bot], fill=col)

    f_small = _load_font(26)
    f_num = _load_font(22)
    f_song = _load_font(24)

    def _circ(i):                            # ①②③④⑤
        return chr(0x2460 + i) if 0 <= i < 20 else f"{i + 1}."

    # 有主題搶話標記：頂端一排倒三角（▼）+ 編號；hero 較大加亮
    mark_y = plot_top - 14

    def _tri(cx, s, fill):
        d.polygon([(cx - s, mark_y - s), (cx + s, mark_y - s), (cx, mark_y + s)], fill=fill)

    for i, (ts, _h, _q) in enumerate(reel.topic_peaks):   # 已依 heat 排序，0=hero
        hero = (i == 0)
        _tri(_x(ts), 12 if hero else 8, _HERO if hero else _MARK)
        d.text((_x(ts) - 7, mark_y - 36), _circ(i), font=f_num,
               fill=_HERO if hero else _MARK)

    # 點歌標記：時間軸下方一條 lane，每首歌打 ♪ tick
    song_lane = plot_bot + 16
    for ts, _u, _t in reel.songs:
        sx = _x(ts)
        d.line([(sx, plot_bot), (sx, song_lane)], fill=_SONG, width=2)
        d.ellipse([sx - 4, song_lane - 4, sx + 4, song_lane + 4], fill=_SONG)

    d.text((_MARGIN, 56), getattr(reel, "date", "") or "", font=f_small, fill=_TXT)

    def _fit(s, font, avail):                # 過長截斷加省略
        if d.textlength(s, font=font) <= avail:
            return s
        while s and d.textlength(s + "…", font=font) > avail:
            s = s[:-1]
        return s + "…"

    # 左半欄：5 個有主題搶話的主題（時間 + 引言），① = hero
    lx = _MARGIN
    lavail = _W // 2 - 30 - lx
    ly = plot_bot + 40
    d.text((lx, ly), "🔥 主題搶話", font=f_song, fill=_MARK)
    ly += 34
    for i, (ts, _h, q) in enumerate(reel.topic_peaks):
        hhmm = _dt.datetime.fromtimestamp(ts).strftime("%H:%M")
        topic = reel.topic_labels[i] if i < len(reel.topic_labels) else _short(q, 10)
        line = _fit(f"{_circ(i)} {hhmm} {topic}", f_song, lavail)
        d.text((lx, ly), line, font=f_song, fill=_HERO if i == 0 else (200, 200, 205))
        ly += 30

    # 點歌清單（時間 + 歌名 + 點歌者）：右半欄，最多 5 行、超過標 +N
    if reel.songs:
        from diary_comic.song_requests import clean_title
        col_x = _W // 2 + 20
        avail = _W - _MARGIN - col_x
        ly = plot_bot + 40
        d.text((col_x, ly), "🎵 今夜點歌", font=f_song, fill=_SONG)
        ly += 34
        shown = reel.songs[:5]
        for ts, u, t in shown:
            hhmm = _dt.datetime.fromtimestamp(ts).strftime("%H:%M")
            line = f"{hhmm}  {clean_title(t)} — {u}"
            while line and d.textlength(line, font=f_song) > avail:  # 過長截斷
                line = line[:-1]
            d.text((col_x, ly), line, font=f_song, fill=(190, 190, 195))
            ly += 30
        extra = len(reel.songs) - len(shown)
        if extra > 0:
            d.text((col_x, ly), f"…＋{extra} 首", font=f_song, fill=(150, 150, 155))

    img.save(out_path)
    return out_path


_TOPIC_SYS = (
    "你是聊天室日記的主題標籤器。每行是一段「搶話高潮」的逐字稿（STT，可能有糊字）。"
    "幫每行回一個 ≤8 字的繁體中文主題短語，點出他們在聊什麼。"
    "只回主題、一行一個、順序對齊輸入、不要編號不要標點不要解釋。"
)


def _short(q: str, n: int = 12) -> str:
    q = (q or "").strip()
    return q if len(q) <= n else q[:n] + "…"


def refine_topics(quotes: list[str], text_fn=None) -> list[str]:
    """把原始搶話引言用 LLM 精煉成 ≤8 字主題；text_fn=None 或任何失敗 → 退回截斷原句。

    text_fn(system, user) -> str（注入式，方便測試、且離線預設不打 LLM）。一次批次 5 句。
    """
    fallback = [_short(q) for q in quotes]
    if not quotes or text_fn is None:
        return fallback
    try:
        user = "\n".join(f"{i + 1}. {q}" for i, q in enumerate(quotes))
        out = text_fn(_TOPIC_SYS, user)
        lines = [re.sub(r"^\s*\d+[.、）)]?\s*", "", ln).strip()
                 for ln in (out or "").splitlines() if ln.strip()]
        if len(lines) != len(quotes):       # 行數對不上 → 不冒險，退原句
            return fallback
        return [(_short(ln, 10) or fallback[i]) for i, ln in enumerate(lines)]
    except Exception:
        return fallback


def build_reveal(rows, out_dir: str, date_label: str = "",
                 song_requests=None, text_fn=None, stamp=None) -> tuple[str, str] | None:
    """rows (+選用點歌) → (png_path, json_path)，或 None（平淡夜 / 無乾淨引言）。

    date_label＝圖上顯示的日期；stamp＝檔名（None→由 date_label 推，同日多場會撞檔故可外帶）。
    text_fn 給定時把 5 個主題用 LLM 精煉成短語（離線預設 None → 用原引言）。
    """
    import os

    reel = curate_reel(rows, song_requests=song_requests)
    if reel is None:
        return None
    reel.topic_labels = refine_topics([q for _ts, _h, q in reel.topic_peaks], text_fn)
    os.makedirs(out_dir, exist_ok=True)
    stamp = (stamp or date_label or "reveal").replace(":", "").replace(" ", "_").replace("-", "")
    png = os.path.join(out_dir, f"night_reel_{stamp}.png")
    js = os.path.join(out_dir, f"night_reel_{stamp}.json")
    reel.date = date_label                  # 給 render 標日期用
    render_ekg_png(reel, png)
    with open(js, "w", encoding="utf-8") as f:
        json.dump(night_reel_dict(reel, date_label), f, ensure_ascii=False, indent=2)
    return png, js


def _db_rows(start_ts_str: str, end_ts_str: str, db_path: str):
    """撈場次時間窗（前後各留 10 分鐘）的 (speaker,text,ts)。

    鏡像 diary_comic_poster._db_rows，但離線腳本自帶以免拉進整個 poster 模組。
    任何失敗（壞時戳/無 DB/無表）→ []，不炸。
    """
    try:
        lo = _dt.datetime.fromisoformat(start_ts_str).timestamp() - 600
        hi = _dt.datetime.fromisoformat(end_ts_str).timestamp() + 600
    except (ValueError, TypeError):
        return []
    try:
        con = sqlite3.connect(db_path)
        try:
            return con.execute(
                "SELECT speaker, text, timestamp FROM transcripts "
                "WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp",
                (lo, hi)).fetchall()
        finally:
            con.close()
    except Exception:
        return []


BOT_LOG = "~/Library/Logs/Marvin/bot_stdout.log"   # [點歌-手動]/[點歌-語音] 來源


def _songs_in_window(start_ts_str: str, end_ts_str: str,
                     bot_log: str = BOT_LOG) -> list[tuple[float, str, str]]:
    """讀 bot log，回時間窗內的點歌 [(ts,點歌者,歌名)]。無 log → []，不炸。"""
    import os

    from diary_comic.song_requests import parse_requests_with_ts
    try:
        lo = _dt.datetime.fromisoformat(start_ts_str).timestamp() - 600
        hi = _dt.datetime.fromisoformat(end_ts_str).timestamp() + 600
        text = open(os.path.expanduser(bot_log), encoding="utf-8", errors="ignore").read()
    except (OSError, ValueError, TypeError):
        return []
    return parse_requests_with_ts(text, since=lo, until=hi)


def make_reveal_from_db(db_path: str, start_ts_str: str, end_ts_str: str,
                        out_dir: str, bot_log: str = BOT_LOG,
                        text_fn=None) -> tuple[str, str] | None:
    """從 marvin.db 撈一段時間窗 + bot log 點歌 → 靜態 EKG PNG。無 rows / 平淡夜 → None。

    text_fn 給定時用 LLM 精煉 5 個主題短語（離線預設 None → 用原引言）。
    """
    rows = _db_rows(start_ts_str, end_ts_str, db_path)
    if not rows:
        return None
    songs = _songs_in_window(start_ts_str, end_ts_str, bot_log)
    return build_reveal(rows, out_dir, date_label=start_ts_str[:10],
                        song_requests=songs, text_fn=text_fn,
                        stamp=start_ts_str[:16])   # 含時間 → 同日多場不撞檔


def _default_text_fn():
    """沿用日記的 Gemini text_fn（免費/付費池同路）；無 key → None（離線退原引言）。"""
    try:
        from diary_comic_poster import _key, _text_fn
        key = _key()
        return _text_fn(key) if key else None
    except Exception:
        return None


if __name__ == "__main__":      # pragma: no cover
    # v0.1 手動跑，眼驗命中率 / 比高低話量（Y 軸固定刻度，可跨晚比）：
    #   python make_reveal.py                  最新一場
    #   python make_reveal.py --last 5         最近 5 場
    #   python make_reveal.py 2026-06-15 2026-06-23   指定日期（同日多場全出）
    import sys

    sys.path.insert(0, ".")
    from diary_comic.parser import dedupe_adjacent, eligible_sessions, parse_log

    LOG_PATH = "records/chat_summary_log.txt"
    DB_PATH = "marvin.db"
    OUT_DIR = "records"
    log_text = open(LOG_PATH, encoding="utf-8").read()
    sessions = eligible_sessions(dedupe_adjacent(parse_log(log_text)))
    if not sessions:
        print("[reveal] 無合格場次")
        sys.exit(0)

    args = sys.argv[1:]
    if args and args[0] == "--last":
        picked = sessions[-(int(args[1]) if len(args) > 1 else 3):]
    elif args:
        want = set(args)
        picked = [s for s in sessions if s[0].ts_str[:10] in want]
    else:
        picked = [sessions[-1]]

    if not picked:
        print(f"[reveal] 找不到符合的場次：{args}")
        sys.exit(0)

    tf = _default_text_fn()
    print(f"[reveal] 產 {len(picked)} 場（Y 軸固定滿格={_FULL_RATE:.0f} 句/分，可跨晚比話量）")
    for sess in picked:
        day = sess[0].ts_str[:10]
        out = make_reveal_from_db(DB_PATH, sess[0].ts_str, sess[-1].ts_str, OUT_DIR,
                                  text_fn=tf)
        print(f"  {day}  → {out[0]}" if out else f"  {day}  平淡夜/無乾淨引言 → 退海報")
