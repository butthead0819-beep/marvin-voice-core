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
    crosstalk_track,
    pick_hottest_window,
)

try:
    from utils import is_whisper_hallucination
except Exception:                       # 離線/路徑問題時退化成只靠重複字閘
    def is_whisper_hallucination(text: str, prompt: str) -> bool:
        return False

MIN_QUOTE_LEN = 6        # 引言最短字數（短於此資訊量不足）
WINDOW_S = 120.0         # 渲染窗長（秒）：最熱 2 分鐘
BIN_S = 10.0             # 熱度 bin 寬（秒）


@dataclass
class Reel:
    window: tuple[float, float]
    heat_track: list[tuple[float, float]]   # 已裁到 window 內
    peak_ts: float
    peak_heat: float
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


def curate_reel(rows, win_s: float = WINDOW_S, bin_s: float = BIN_S) -> Reel | None:
    """rows → Reel，或 None（平淡夜 / 窗內選不出乾淨引言 → 退靜態海報）。

    自動選最熱窗，引言取窗內最熱事件的第一句通過品質閘者。窗內全糊字 → None。
    """
    track = crosstalk_track(rows, bin_s)
    win = pick_hottest_window(track, win_s)
    if win is None:                         # 無搶話事件＝平淡夜
        return None
    start, end = win
    clipped = [(t, h) for t, h in track if start <= t <= end]
    for ev in sorted(_crosstalk_events(rows), key=lambda e: -e.heat):
        if not (start <= ev.ts <= end):
            continue
        for _spk, txt in ev.lines:
            if _quote_quality_ok(txt):
                return Reel(window=win, heat_track=clipped, peak_ts=ev.ts,
                            peak_heat=ev.heat, speakers=ev.speakers, quote=txt)
    return None                             # 窗內無乾淨引言


def night_reel_dict(reel: Reel, date_label: str = "") -> dict:
    """Reel → 可序列化 dict（debug dump，非治理契約：無 schema_version/cast）。"""
    return {
        "date": date_label,
        "window": list(reel.window),
        "heat_track": [{"t": t, "heat": h} for t, h in reel.heat_track],
        "peak": {
            "ts": reel.peak_ts,
            "heat": reel.peak_heat,
            "speakers": reel.speakers,
            "quote": reel.quote,
        },
    }


# ── 靜態 EKG 渲染（Pillow）────────────────────────────────────────────
#   ┌──────────────────────────────────────────┐
#   │  [date]                                   │
#   │            ╱╲      ● freeze(peak)          │  ← heat 折線，峰值打點
#   │      ╱╲  ╱   ╲   ╱                         │
#   │  ───╯  ╲╯     ╲─╯                          │
#   │  「<quote>」  — speakers                    │  ← 真實逐字稿引言
#   └──────────────────────────────────────────┘
_W, _H = 1200, 675           # 16:9
_MARGIN = 80
_BG = (18, 18, 22)
_CURVE = (235, 90, 90)
_DOT = (255, 220, 120)
_TXT = (235, 235, 235)


def render_ekg_png(reel: Reel, out_path: str) -> str:
    from PIL import Image, ImageDraw

    from diary_comic.layout import _load_font

    img = Image.new("RGB", (_W, _H), _BG)
    d = ImageDraw.Draw(img)

    start, end = reel.window
    span = max(end - start, 1e-6)
    plot_top, plot_bot = 160, 420
    x0, x1 = _MARGIN, _W - _MARGIN
    hmax = max((h for _, h in reel.heat_track), default=1.0) or 1.0

    def _xy(t, h):
        x = x0 + (t - start) / span * (x1 - x0)
        y = plot_bot - (h / hmax) * (plot_bot - plot_top)
        return x, y

    pts = [_xy(t, h) for t, h in reel.heat_track]
    if len(pts) >= 2:
        d.line(pts, fill=_CURVE, width=4, joint="curve")
    # freeze 點：峰值
    px, py = _xy(reel.peak_ts, reel.peak_heat)
    d.ellipse([px - 9, py - 9, px + 9, py + 9], fill=_DOT)

    f_small = _load_font(28)
    f_quote = _load_font(48)
    d.text((_MARGIN, 60), reel.date if hasattr(reel, "date") else "", font=f_small, fill=_TXT)
    d.text((_MARGIN, plot_bot + 60), f"「{reel.quote}」", font=f_quote, fill=_TXT)
    d.text((_MARGIN, plot_bot + 130), "— " + "、".join(reel.speakers),
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
