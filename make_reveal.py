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
from dataclasses import dataclass

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
BIN_S = 30.0             # 發言密度 bin 寬（秒）


@dataclass
class Reel:
    window: tuple[float, float]                 # 整晚 (start_ts, end_ts)
    activity_track: list[tuple[float, float]]   # 底層曲線＝發言密度（句/bin），＝「熱鬧」
    peaks: list[tuple[float, float]]            # 搶話峰標記 (ts, heat)，疊在曲線上
    hero_ts: float                              # 最熱搶話事件（freeze + 引言來源）
    hero_heat: float
    speakers: list[str]
    quote: str


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


def curate_reel(rows, bin_s: float = BIN_S) -> Reel | None:
    """rows → Reel，或 None（無人說話 / 無乾淨搶話引言 → 退靜態海報）。

    底層曲線＝整晚發言密度（熱鬧弧線）；搶話峰當標記疊上；hero 引言取最熱搶話事件
    第一句過品質閘者。整晚無乾淨搶話引言 → None。
    """
    rows = _human_rows(rows)
    act = activity_track(rows, bin_s)
    if not act:                             # 整晚沒人說話
        return None
    events = _crosstalk_events(rows)
    peaks = [(e.ts, e.heat) for e in events]
    window = (act[0][0], act[-1][0] + bin_s)
    for ev in sorted(events, key=lambda e: -e.heat):
        for _spk, txt in ev.lines:
            if _quote_quality_ok(txt):
                return Reel(window=window, activity_track=act, peaks=peaks,
                            hero_ts=ev.ts, hero_heat=ev.heat,
                            speakers=ev.speakers, quote=txt)
    return None                             # 整晚無乾淨搶話引言


def night_reel_dict(reel: Reel, date_label: str = "") -> dict:
    """Reel → 可序列化 dict（debug dump，非治理契約：無 schema_version/cast）。"""
    return {
        "date": date_label,
        "window": list(reel.window),
        "activity_track": [{"t": t, "n": n} for t, n in reel.activity_track],
        "peaks": [{"ts": ts, "heat": h} for ts, h in reel.peaks],
        "hero": {
            "ts": reel.hero_ts,
            "heat": reel.hero_heat,
            "speakers": reel.speakers,
            "quote": reel.quote,
        },
    }


# ── 靜態 EKG 渲染（Pillow）────────────────────────────────────────────
#   ┌──────────────────────────────────────────┐
#   │  [date]                                   │
#   │        ╱‾‾╲    ●hero  ╱‾╲                  │  ← 發言密度曲線＝整晚熱鬧弧
#   │   ╱‾╲╱    ╲  ·  · ╱‾‾    ╲                 │  ← · = 搶話峰標記疊在曲線上
#   │ ╱‾        ╲╱  ╲╱          ╲╱               │
#   │  「<quote>」  — speakers                    │  ← hero 搶話事件的真實引言
#   └──────────────────────────────────────────┘
_W, _H = 1200, 675           # 16:9
_MARGIN = 80
_BG = (18, 18, 22)
_FILL = (60, 70, 110)        # 密度曲線下的柔色填充（熱鬧弧）
_CURVE = (130, 160, 235)     # 密度曲線
_MARK = (235, 110, 110)      # 搶話峰標記
_HERO = (255, 220, 120)      # hero freeze 點
_TXT = (235, 235, 235)


def render_ekg_png(reel: Reel, out_path: str) -> str:
    from PIL import Image, ImageDraw

    from diary_comic.layout import _load_font

    img = Image.new("RGB", (_W, _H), _BG)
    d = ImageDraw.Draw(img)

    start, end = reel.window
    span = max(end - start, 1e-6)
    plot_top, plot_bot = 150, 430
    x0, x1 = _MARGIN, _W - _MARGIN
    nmax = max((n for _, n in reel.activity_track), default=1.0) or 1.0

    def _x(t):
        return x0 + (t - start) / span * (x1 - x0)

    def _y(n):
        return plot_bot - (n / nmax) * (plot_bot - plot_top)

    # 發言密度曲線 + 下方填充（整晚熱鬧弧）
    pts = [(_x(t), _y(n)) for t, n in reel.activity_track]
    if len(pts) >= 2:
        poly = [(x0, plot_bot)] + pts + [(x1, plot_bot)]
        d.polygon(poly, fill=_FILL)
        d.line(pts, fill=_CURVE, width=3, joint="curve")

    # 在密度曲線該 ts 的高度上找對應 y（最近 bin）
    def _act_y_at(ts):
        best = min(reel.activity_track, key=lambda tn: abs(tn[0] - ts),
                   default=(start, 0.0))
        return _y(best[1])

    # 搶話峰標記（小紅點疊在曲線上）
    for ts, _h in reel.peaks:
        mx, my = _x(ts), _act_y_at(ts)
        d.ellipse([mx - 4, my - 4, mx + 4, my + 4], fill=_MARK)

    # hero freeze（大亮點 + 外環）
    hx, hy = _x(reel.hero_ts), _act_y_at(reel.hero_ts)
    d.ellipse([hx - 11, hy - 11, hx + 11, hy + 11], outline=_HERO, width=3)
    d.ellipse([hx - 6, hy - 6, hx + 6, hy + 6], fill=_HERO)

    f_small = _load_font(28)
    f_quote = _load_font(48)
    d.text((_MARGIN, 56), getattr(reel, "date", "") or "", font=f_small, fill=_TXT)
    d.text((_MARGIN, plot_bot + 70), f"「{reel.quote}」", font=f_quote, fill=_TXT)
    d.text((_MARGIN, plot_bot + 140), "— " + "、".join(reel.speakers),
           font=f_small, fill=(170, 170, 175))

    img.save(out_path)
    return out_path


def build_reveal(rows, out_dir: str, date_label: str = "") -> tuple[str, str] | None:
    """rows → (png_path, json_path)，或 None（平淡夜 / 無乾淨引言）。"""
    import os

    reel = curate_reel(rows)
    if reel is None:
        return None
    os.makedirs(out_dir, exist_ok=True)
    stamp = (date_label or "reveal").replace(":", "").replace(" ", "_").replace("-", "")
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


def make_reveal_from_db(db_path: str, start_ts_str: str, end_ts_str: str,
                        out_dir: str) -> tuple[str, str] | None:
    """從 marvin.db 撈一段時間窗 → 靜態 EKG PNG。無 rows / 平淡夜 → None。"""
    rows = _db_rows(start_ts_str, end_ts_str, db_path)
    if not rows:
        return None
    return build_reveal(rows, out_dir, date_label=start_ts_str[:10])


if __name__ == "__main__":      # pragma: no cover
    # v0.1 手動跑：抓最新一場合格夜晚 → 出靜態 EKG PNG 給自己眼驗命中率。
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
    sess = sessions[-1]
    out = make_reveal_from_db(DB_PATH, sess[0].ts_str, sess[-1].ts_str, OUT_DIR)
    print(f"[reveal] {out}" if out else "[reveal] 平淡夜 / 無乾淨引言 → 退靜態海報")
