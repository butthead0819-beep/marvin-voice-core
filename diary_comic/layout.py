"""不對等漫畫拼版（B 架構骨架）。

設計需求：
- 日本漫畫式不對等切割（格子大小不均）
- 格子大小 = 該時段熱度（熱的格子大）
- 馬文碎念用 CJK 字型疊字（解決 nano-banana 圖內中文糊掉的問題）

模板用相對座標 (x, y, w, h)∈[0,1]，由 compose_page 換算成像素。
"""
from __future__ import annotations

import difflib
import math
import os
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont, ImageStat

Box = tuple[float, float, float, float]  # x, y, w, h（相對 0..1）


@dataclass
class Panel:
    image: Image.Image
    heat: int
    caption: str = ""
    inset: Image.Image | None = None  # 反應特寫小格（疊在本格角落），如哄堂笑的臉


@dataclass
class CropSpec:
    box: tuple                 # (l, t, r, b) 相對座標 0..1
    caption: str = ""
    heat: int = 3


def crops_from_source(source_img, specs):
    """同源推鏡：從一張（高清）素材按相對框裁出多格 Panel。

    省 API、角色零飄移、自帶遠→中→特推鏡。裁太緊會糊 → 素材需夠高解析度（2K）。
    """
    W, H = source_img.size
    panels = []
    for s in specs:
        l, t, r, b = s.box
        l, t = max(0.0, l), max(0.0, t)
        r, b = min(1.0, r), min(1.0, b)
        crop = source_img.crop((int(l * W), int(t * H), int(r * W), int(b * H)))
        panels.append(Panel(image=crop, heat=s.heat, caption=s.caption))
    return panels


def split_lr_specs(ratio=0.30, captions=None, heats=None):
    """遠景精準對切左右兩格：左 [0,ratio]、右 [ratio,1]，邊界共用、**零重疊**。

    源圖須有左右兩個主體（一個落在左 ratio、一個落在右），切下去才是兩主體不重複。
    """
    caps = captions or ["", ""]
    hs = heats or [3, 4]
    return [CropSpec(box=(0.0, 0.0, ratio, 1.0), caption=caps[0], heat=hs[0]),
            CropSpec(box=(ratio, 0.0, 1.0, 1.0), caption=caps[1], heat=hs[1])]


def zoom_wide_specs(focus_box, captions=None, heats=None):
    """格1 焦點+全景（B 打法）：左=某角色放大特寫 focus_box、右=全景。

    左格放大「講笑話的那個人」當情緒錨點，右格給全場脈絡。左格放大需 2K 源才不糊。
    """
    caps = captions or ["", ""]
    hs = heats or [4, 3]
    return [CropSpec(box=tuple(focus_box), caption=caps[0], heat=hs[0]),
            CropSpec(box=(0.0, 0.0, 1.0, 1.0), caption=caps[1], heat=hs[1])]


def pushin_specs(captions=None, heats=None):
    """標準遠→中→特推鏡三框（一路推緊，置中偏 rule-of-thirds）。"""
    boxes = [(0.0, 0.0, 1.0, 1.0),            # 遠景：全場
             (0.15, 0.12, 0.82, 0.80),        # 中景：推進
             (0.34, 0.30, 0.68, 0.62)]        # 特寫：重點（需高清素材才不糊）
    caps = captions or ["", "", ""]
    hs = heats or [3, 4, 5]
    return [CropSpec(box=b, caption=caps[i], heat=hs[i]) for i, b in enumerate(boxes)]


# 手刻不對等模板（手機直式：垂直堆疊為主，穿插 2-up 排，面積刻意不均）。
# 一頁讀下來像滑手機，仍保留日漫不對等切割的呼吸感。
_TEMPLATES: dict[int, list[Box]] = {
    1: [(0.0, 0.0, 1.0, 1.0)],
    2: [(0.0, 0.0, 1.0, 0.52), (0.0, 0.53, 1.0, 0.47)],
    3: [(0.0, 0.0, 1.0, 0.40), (0.0, 0.41, 0.52, 0.59), (0.53, 0.41, 0.47, 0.59)],
    4: [(0.0, 0.0, 1.0, 0.30), (0.0, 0.31, 0.50, 0.28),
        (0.51, 0.31, 0.49, 0.28), (0.0, 0.60, 1.0, 0.40)],
    5: [(0.0, 0.0, 1.0, 0.26), (0.0, 0.27, 0.55, 0.24), (0.56, 0.27, 0.44, 0.24),
        (0.0, 0.52, 0.45, 0.48), (0.46, 0.52, 0.54, 0.48)],
    6: [(0.0, 0.0, 1.0, 0.22), (0.0, 0.23, 0.48, 0.20), (0.50, 0.23, 0.50, 0.20),
        (0.0, 0.44, 1.0, 0.26), (0.0, 0.71, 0.55, 0.29), (0.56, 0.71, 0.44, 0.29)],
}


def pick_template(n: int) -> list[Box]:
    """回傳 n 格的不對等模板。超出手刻範圍 → 退回近似格狀（並非靜默：呼叫端可自行 log）。"""
    if n in _TEMPLATES:
        return _TEMPLATES[n]
    # fallback：盡量分散大小，避免完全均等
    cols = 2 if n <= 8 else 3
    rows = (n + cols - 1) // cols
    boxes: list[Box] = []
    for i in range(n):
        r, c = divmod(i, cols)
        w = (1.0 / cols) - 0.02
        h = (1.0 / rows) - 0.02
        boxes.append((c * (1.0 / cols), r * (1.0 / rows), w, h))
    return boxes


def assign_boxes(heats: list[int], boxes: list[Box]) -> list[int]:
    """回傳 panel_index -> box_index 的對應：最熱的格子配最大的 box。"""
    by_heat = sorted(range(len(heats)), key=lambda i: heats[i], reverse=True)
    by_area = sorted(range(len(boxes)),
                     key=lambda i: boxes[i][2] * boxes[i][3], reverse=True)
    order = [0] * len(heats)
    for rank, panel_idx in enumerate(by_heat):
        order[panel_idx] = by_area[rank]
    return order


def plan_boxes(heats: list[int]) -> list[Box]:
    """回傳每個 panel 分到的 box（panel_index 對齊）。給出圖端先知道每格形狀。"""
    boxes = pick_template(len(heats))
    order = assign_boxes(heats, boxes)
    return [boxes[order[i]] for i in range(len(heats))]


# nano-banana 支援的出圖比例（之後新增可擴充）
_SUPPORTED_RATIOS = {
    "9:16": 9 / 16, "2:3": 2 / 3, "3:4": 3 / 4, "1:1": 1.0,
    "4:3": 4 / 3, "3:2": 3 / 2, "16:9": 16 / 9,
}


def nearest_aspect_ratio(box: Box, page_size: tuple[int, int]) -> str:
    """box 在實際頁面上的長寬比 → 最接近的支援出圖比例字串。"""
    W, H = page_size
    _x, _y, bw, bh = box
    ar = (bw * W) / (bh * H)
    return min(_SUPPORTED_RATIOS, key=lambda name: abs(_SUPPORTED_RATIOS[name] - ar))


Poly = list[tuple[float, float]]  # 四邊形相對座標 [tl, tr, br, bl]


def slanted_bands(heats: list[int], tilt: float = 0.035) -> list[Poly]:
    """垂直堆疊的滿寬斜格：高度依 heat、分隔線交替傾斜（日漫斜分鏡）。

    回傳每格的相對四邊形 [左上, 右上, 右下, 左下]。相鄰格共用斜分隔邊（不重疊不留縫）；
    頁面最上緣、最下緣保持水平（不歪）。tilt 自動夾住，避免越界/翻轉。
    """
    n = len(heats)
    w = [max(h, 1) for h in heats]
    total = sum(w)
    ys = [0.0]
    for hh in w:
        ys.append(ys[-1] + hh / total)
    ys[-1] = 1.0

    d = [0.0] * (n + 1)  # 每條分隔線的傾斜量；頭尾為 0
    for k in range(1, n):
        sign = 1.0 if k % 2 else -1.0
        gap = min(ys[k] - ys[k - 1], ys[k + 1] - ys[k])
        d[k] = sign * min(tilt, gap * 0.45)  # 不超過相鄰半格

    def clamp(v: float) -> float:
        return max(0.0, min(1.0, v))

    polys: list[Poly] = []
    for i in range(n):
        tl = (0.0, clamp(ys[i] - d[i]))
        tr = (1.0, clamp(ys[i] + d[i]))
        br = (1.0, clamp(ys[i + 1] + d[i + 1]))
        bl = (0.0, clamp(ys[i + 1] - d[i + 1]))
        polys.append([tl, tr, br, bl])
    return polys


def cover_fit(img: Image.Image, w: int, h: int) -> Image.Image:
    """填滿 (w,h) 並保持原比例、置中裁切 —— 永不拉伸變形。

    用 LANCZOS 取樣：縮小細節圖才不會糊（預設 BICUBIC 縮小會軟掉）。
    """
    w, h = max(1, w), max(1, h)
    sw, sh = img.size
    scale = max(w / sw, h / sh)
    rw, rh = max(w, int(sw * scale + 0.5)), max(h, int(sh * scale + 0.5))
    resized = img.resize((rw, rh), Image.LANCZOS)
    left, top = (rw - w) // 2, (rh - h) // 2
    return resized.crop((left, top, left + w, top + h))


_FONT_CANDIDATES = [
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
]


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    for path in _FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _text_width(font, s: str) -> float:
    try:
        return font.getlength(s)
    except Exception:
        return len(s) * getattr(font, "size", 16) * 0.6


def wrap_text(text: str, font, max_width: int) -> list[str]:
    """逐字斷行（CJK 無詞界），每行寬度 ≤ max_width，不丟字。

    過窄時每行至少塞一字，避免無限迴圈。
    """
    lines: list[str] = []
    cur = ""
    for ch in text:
        if ch == "\n":
            lines.append(cur)
            cur = ""
            continue
        if cur and _text_width(font, cur + ch) > max_width:
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    return lines


def compose_page(panels: list[Panel],
                 page_size: tuple[int, int] = (1200, 1600)) -> Image.Image:
    """兩階段：先 resize+拼接所有圖，全部拼完後再把字幕當最上層疊上去。

    字幕直接畫在全頁原生解析度上（永不跟著圖 resize），所以清楚。
    """
    W, H = page_size
    page = Image.new("RGB", (W, H), (250, 248, 244))
    draw = ImageDraw.Draw(page)
    boxes = pick_template(len(panels))
    order = assign_boxes([p.heat for p in panels], boxes)
    gutter = max(4, int(min(W, H) * 0.012))
    font = _load_font(max(14, int(min(W, H) * 0.030)))  # 字級放大，縮圖也讀得清
    pad = max(8, int(font.size * 0.4))

    # Phase 1：resize + 拼接所有圖 + 邊框
    placed = []
    for pi, panel in enumerate(panels):
        bx, by, bw, bh = boxes[order[pi]]
        x = int(bx * W) + gutter
        y = int(by * H) + gutter
        w = max(1, int(bw * W) - 2 * gutter)
        h = max(1, int(bh * H) - 2 * gutter)
        page.paste(cover_fit(panel.image, w, h), (x, y))  # 填滿+裁切，不拉伸
        draw.rectangle([x, y, x + w, y + h], outline=(30, 30, 30), width=3)
        placed.append((panel, x, y, w, h))

    # Phase 2：圖全部拼好後，字幕最上層疊（不透明底 + 多行）
    line_h = int(font.size * 1.3)
    for panel, x, y, w, h in placed:
        if not panel.caption:
            continue
        lines = wrap_text(panel.caption, font, w - 2 * pad)
        band_h = min(h, line_h * len(lines) + 2 * pad)
        band_top = y + h - band_h
        band = Image.new("RGBA", (w, band_h), (15, 15, 15, 230))
        page.paste(band, (x, band_top), band)
        for li, ln in enumerate(lines):
            draw.text((x + pad, band_top + pad + li * line_h), ln,
                      fill=(255, 255, 255), font=font)
    return page


def hero_split_polys(x0: int, y0: int, x1: int, y1: int, tilt: float = 0.12):
    """Hero 矩形內用一條對角線切成上下兩個梯形（共用斜邊）。

    外框矩形，上緣/下緣保持水平 → 字幕可乾淨貼在上緣與下緣。
    回傳 (upper, lower)，各為 [左上, 右上, 右下, 左下]。
    """
    h = y1 - y0
    y_left = int(y0 + (0.5 + tilt) * h)   # 對角線左端（偏下）
    y_right = int(y0 + (0.5 - tilt) * h)  # 右端（偏上）→ 斜線
    upper = [(x0, y0), (x1, y0), (x1, y_right), (x0, y_left)]
    lower = [(x0, y_left), (x1, y_right), (x1, y1), (x0, y1)]
    return upper, lower


def poly_bbox(poly: Poly) -> Box:
    """多邊形的外接矩形 (x, y, w, h)，相對座標。給出圖比例用。"""
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    return (min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))


def _edge_caption(draw, page, x0, x1, y_edge, caption, font, anchor):
    """簡化字幕：上緣/下緣的半透明邊條 + 文字。anchor='top'/'bottom'。"""
    pad = max(8, int(font.size * 0.4))
    line_h = int(font.size * 1.3)
    lines = wrap_text(caption, font, (x1 - x0) - 2 * pad)
    bar_h = line_h * len(lines) + 2 * pad
    by0 = y_edge if anchor == "top" else y_edge - bar_h
    bar = Image.new("RGBA", (x1 - x0, bar_h), (15, 15, 15, 205))
    page.paste(bar, (x0, by0), bar)
    for li, ln in enumerate(lines):
        draw.text((x0 + pad, by0 + pad + li * line_h), ln, fill=(255, 255, 255), font=font)


def paste_inset(page: Image.Image, inset_img: Image.Image, x: int, y: int,
                w: int, h: int, border: int = 4) -> Image.Image:
    """把反應特寫小格疊到 (x,y,w,h)：白內框 + 黑外框（日漫 inset 感）。回傳 page。"""
    w, h = max(1, w), max(1, h)
    page.paste(cover_fit(inset_img, w, h), (x, y))
    d = ImageDraw.Draw(page)
    d.rectangle([x, y, x + w, y + h], outline=(245, 245, 240), width=border)
    d.rectangle([x - 2, y - 2, x + w + 2, y + h + 2], outline=(20, 20, 20), width=2)
    return page


def _draw_inset_corner(page, panel, x0, y0, x1, y1):
    """若 panel 有 inset，疊在格子右下角（~32% 寬）。"""
    if panel.inset is None:
        return
    pw, ph = x1 - x0, y1 - y0
    iw, ih = int(pw * 0.32), int(ph * 0.32)
    m = max(6, int(min(pw, ph) * 0.04))
    paste_inset(page, panel.inset, x1 - iw - m, y1 - ih - m, iw, ih)


def gutter_between(prev_core: str, next_core: str, base: int) -> int:
    """依相鄰兩格內容相似度算 gutter：相似(同場景)→窄(快/連續)、不同(跳主題)→寬(時間流逝)。"""
    r = difflib.SequenceMatcher(None, prev_core or "", next_core or "").ratio()
    return max(4, int(base * (0.5 + (1.0 - r))))  # r=1→0.5×；r=0→1.5×


def with_title(page: Image.Image, title: str, bar_h: int | None = None) -> Image.Image:
    """頁首加一條標題 bar（單話名）。空標題 → 原圖。"""
    if not title:
        return page
    W, H = page.size
    bh = bar_h or max(48, int(H * 0.055))
    out = Image.new("RGB", (W, H + bh), (20, 20, 20))
    out.paste(page, (0, bh))
    draw = ImageDraw.Draw(out)
    font = _load_font(int(bh * 0.5))
    draw.text((int(W * 0.02), int(bh * 0.22)), title[:24], fill=(255, 245, 220), font=font)
    return out


# 馬文語錄 epigraph 配色（深墨底+米字，厭世日記調性；可調）
QUOTE_BG = (28, 24, 32)
QUOTE_FG = (228, 220, 200)
QUOTE_ATTR = (150, 140, 120)


def compose_quote_strip(quote: str, width: int = 1080) -> Image.Image:
    """今夜馬文語錄 epigraph 條：「碎念」—— 馬文。深色，異於暖色點歌台與一般字幕。"""
    pad = max(20, int(width * 0.04))
    font = _load_font(max(26, int(width * 0.040)))
    lines = wrap_text(f"「{quote}」", font, width - 2 * pad)
    lh = int(font.size * 1.4)
    attr_h = int(font.size * 1.3)
    H = pad + lh * len(lines) + attr_h + pad
    strip = Image.new("RGB", (width, H), QUOTE_BG)
    draw = ImageDraw.Draw(strip)
    y = pad
    for ln in lines:
        draw.text((pad, y), ln, fill=QUOTE_FG, font=font)
        y += lh
    attr = "—— 馬文"
    aw = _text_width(font, attr)
    draw.text((width - pad - aw, y), attr, fill=QUOTE_ATTR, font=font)
    return strip


def prepend_quote(page: Image.Image, quote: str) -> Image.Image:
    """把馬文語錄接在漫畫頁上方當開頁 epigraph。空 → 原圖。"""
    if not quote:
        return page
    strip = compose_quote_strip(quote, width=page.width)
    out = Image.new("RGB", (page.width, page.height + strip.height), (250, 248, 244))
    out.paste(strip, (0, 0))
    out.paste(page, (0, strip.height))
    return out


# 點歌台卡片配色（暖色復古電台，刻意有別於標準深框白字字幕；可調）
SONG_CARD_BG = (236, 208, 138)      # 奶油金底
SONG_CARD_FG = (74, 48, 28)         # 深褐字
SONG_CARD_ACCENT = (176, 122, 58)   # 分隔線/DJ榜


def compose_song_card(requests, width: int = 1080, *, covers=None, bg=SONG_CARD_BG,
                      fg=SONG_CARD_FG, accent=SONG_CARD_ACCENT) -> Image.Image:
    """「今夜點歌台」一格：歌單 + DJ 榜。暖色卡，色彩異於一般字幕。

    requests: [(點歌者, 完整歌名)]；covers: 對齊的 cover 縮圖 [PIL|None]（None→純文字列）。
    """
    from diary_comic.song_requests import dj_tally, clean_title
    pad = max(16, int(width * 0.03))
    hfont = _load_font(max(28, int(width * 0.050)))
    font = _load_font(max(22, int(width * 0.036)))
    sub = _load_font(max(18, int(width * 0.028)))
    items = requests[:8]
    covers = (covers or [None] * len(requests))[:8]
    has_cover = any(c is not None for c in covers)
    thumb_w, thumb_h = (int(width * 0.16), int(width * 0.12)) if has_cover else (0, 0)
    row_h = max(thumb_h + pad // 2, int(font.size * 1.6))
    tally = dj_tally(requests)
    tally_str = ("點歌王　" + "　".join(f"{u}×{c}" for u, c in tally[:4])) if tally else ""
    head_h = int(hfont.size * 1.3)
    H = pad + head_h + pad // 2 + row_h * len(items) + (int(font.size * 1.5) if tally_str else 0) + pad
    card = Image.new("RGB", (width, H), bg)
    draw = ImageDraw.Draw(card)
    draw.text((pad, pad), "今夜點歌台", fill=fg, font=hfont)
    hy = pad + head_h
    draw.line([(pad, hy), (width - pad, hy)], fill=accent, width=4)
    y = hy + pad // 2
    for (user, title), cover in zip(items, covers):
        tx = pad
        if has_cover:
            if cover is not None:
                card.paste(cover_fit(cover, thumb_w, thumb_h), (pad, y))
            else:
                draw.rectangle([pad, y, pad + thumb_w, y + thumb_h], fill=accent)
            tx = pad + thumb_w + pad // 2
        ty = y + (thumb_h - int(font.size * 1.0) - int(sub.size)) // 2 if has_cover else y
        draw.text((tx, ty), f"《{clean_title(title)[:16]}》", fill=fg, font=font)
        draw.text((tx, ty + int(font.size * 1.1)), user, fill=accent, font=sub)
        y += row_h
    if tally_str:
        draw.text((pad, y + pad // 2), tally_str, fill=accent, font=font)
    return card


def append_song_card(page: Image.Image, requests, covers=None) -> Image.Image:
    """把點歌台卡片接在漫畫頁下方。無點歌 → 原圖。"""
    if not requests:
        return page
    card = compose_song_card(requests, width=page.width, covers=covers)
    out = Image.new("RGB", (page.width, page.height + card.height), (250, 248, 244))
    out.paste(page, (0, 0))
    out.paste(card, (0, page.height))
    return out


def compose_meme(image: Image.Image, top: str = "", bottom: str = "",
                 size: tuple[int, int] = (1080, 1080)) -> Image.Image:
    """一格 meme：滿版圖 + 上 setup / 下 punchline 邊條（下可空=強反差單飛）。"""
    W, H = size
    page = cover_fit(image, W, H)  # 滿版
    draw = ImageDraw.Draw(page)
    font = _load_font(max(20, int(min(W, H) * 0.046)))  # meme 字大一點
    if top:
        _edge_caption(draw, page, 0, W, 0, top, font, "top")
    if bottom:
        _edge_caption(draw, page, 0, W, H, bottom, font, "bottom")
    return page


def splash_layout(n_support, page_size, climax_frac=0.45, v_gutter=None, h_gutter=None):
    """大砸框版面：高潮格佔底部 climax_frac（≥40%），鋪陳格在上方。

    鐵律：**垂直格線(欄間)窄、水平格線(列間)寬** → 防讀者跳行。
    回 (support_boxes, climax_box)，皆像素 (x0,y0,x1,y1)。
    """
    W, H = page_size
    vg = v_gutter or max(3, int(min(W, H) * 0.008))   # 垂直格線：窄
    hg = h_gutter or max(10, int(min(W, H) * 0.026))  # 水平格線：寬
    ch = int(climax_frac * H)
    climax = (vg, H - ch + hg // 2, W - vg, H - vg)    # 底部大砸框
    top_y0, top_y1 = vg, H - ch - hg // 2              # 上方鋪陳區
    n = max(1, n_support)
    cols = n if n <= 3 else (n + 1) // 2
    rows = 1 if n <= 3 else 2
    cw = (W - 2 * vg - (cols - 1) * vg) // cols        # 欄寬（窄垂直 gutter）
    rh = (top_y1 - top_y0 - (rows - 1) * hg) // rows   # 列高（寬水平 gutter）
    boxes = []
    for i in range(n):
        r, c = divmod(i, cols)
        x0 = vg + c * (cw + vg)
        y0 = top_y0 + r * (rh + hg)
        boxes.append((x0, y0, x0 + cw, y0 + rh))
    return boxes, climax


def compose_splash_page(support_panels, climax_panel, page_size=(1080, 1920),
                        climax_frac=0.45, climax_tilt=0.08):
    """一頁一個大砸框：高潮格 ≥40% 在底、鋪陳小格在上。

    規則3：鋪陳=方正（平靜）、高潮=斜框（動態）。climax_tilt=0 → 高潮也方正。
    """
    W, H = page_size
    page = Image.new("RGB", (W, H), (250, 248, 244))
    draw = ImageDraw.Draw(page)
    font = _load_font(max(14, int(min(W, H) * 0.026)))
    boxes, climax = splash_layout(len(support_panels), page_size, climax_frac)

    # 鋪陳：方正小格（平靜）
    for panel, (x0, y0, x1, y1) in zip(support_panels, boxes):
        if x1 <= x0 or y1 <= y0:
            continue
        page.paste(cover_fit(panel.image, x1 - x0, y1 - y0), (x0, y0))
        draw.rectangle([x0, y0, x1, y1], outline=(20, 20, 20), width=4)
        if panel.caption:
            _edge_caption(draw, page, x0, x1, y1, panel.caption, font, "bottom")

    # 高潮：斜框大砸框（動態）—— 頂邊傾斜切過去
    cx0, cy0, cx1, cy1 = climax
    tilt = int(climax_tilt * (cy1 - cy0))
    quad = [(cx0, cy0 + tilt), (cx1, cy0 - tilt), (cx1, cy1), (cx0, cy1)]  # 斜頂
    by0 = cy0 - tilt
    bw, bh = cx1 - cx0, cy1 - by0
    mask = Image.new("L", (bw, bh), 0)
    ImageDraw.Draw(mask).polygon([(px - cx0, py - by0) for px, py in quad], fill=255)
    page.paste(cover_fit(climax_panel.image, bw, bh), (cx0, by0), mask)
    draw.line(quad + [quad[0]], fill=(15, 15, 15), width=8, joint="curve")  # 斜框
    if climax_panel.caption:
        big = _load_font(max(20, int(min(W, H) * 0.034)))
        _edge_caption(draw, page, cx0, cx1, cy1, climax_panel.caption, big, "bottom")
    return page


def compose_page_webtoon(panels, page_width=1080, gutter=70, base_h=780, side=36):
    """韓國條漫：滿寬格垂直堆疊、變動白間距（gutter 編碼節奏）、一條長直幅。高度依 heat。"""
    n = len(panels)
    maxheat = max((p.heat for p in panels), default=1) or 1
    heights = [int(base_h * (0.7 + 0.5 * (p.heat / maxheat))) for p in panels]
    # 每格之前的 gutter：第一格=頂margin；其餘依與上一格相似度變動
    gaps = [gutter]
    for i in range(1, n):
        gaps.append(gutter_between(panels[i - 1].caption, panels[i].caption, gutter))
    total_h = sum(gaps) + sum(heights) + gutter
    page = Image.new("RGB", (page_width, total_h), (250, 248, 244))
    draw = ImageDraw.Draw(page)
    font = _load_font(max(16, int(page_width * 0.030)))
    y = 0
    for i, (panel, h) in enumerate(zip(panels, heights)):
        y += gaps[i]
        x0, x1 = side, page_width - side
        page.paste(cover_fit(panel.image, x1 - x0, h), (x0, y))
        draw.rectangle([x0, y, x1, y + h], outline=(20, 20, 20), width=5)
        _draw_inset_corner(page, panel, x0, y, x1, y + h)  # 反應特寫
        if panel.caption:
            _edge_caption(draw, page, x0, x1, y + h, panel.caption, font, "bottom")
        y += h
    return page


def compose_page_hero(rows, page_size=(1080, 1920), tilt=0.12, heights=None):
    """矩形垂直堆疊；Hero 列內部對角斜切成上下兩格。字幕走上下緣邊條（簡化）。

    rows: 每列為 ("single"/"pair"/"vpair"/"quad"/"duo", Panel...)。
    heights: 每列高度比例（手調，總和~1）。給了就用它（鎖長寬比）；否則依 heat。
    """
    W, H = page_size
    page = Image.new("RGB", (W, H), (250, 248, 244))
    draw = ImageDraw.Draw(page)
    font = _load_font(max(22, int(min(W, H) * 0.045)))  # 字幕放大（slant/hero 路徑，1080 寬→48px）
    g = max(4, int(min(W, H) * 0.012))

    if heights is not None:
        fracs = list(heights)
    else:
        weights = [sum(x.heat for x in r[1:] if isinstance(x, Panel)) or 1 for r in rows]
        total = sum(max(w, 1) for w in weights)
        fracs = [max(w, 1) / total for w in weights]
    y = 0.0
    bounds = [0]
    for fr in fracs:
        y += fr
        bounds.append(int(y * H))
    bounds[-1] = H

    for ri, row in enumerate(rows):
        x0, y0, x1, y1 = g, bounds[ri] + g, W - g, bounds[ri + 1] - g
        if y1 <= y0:
            continue
        if row[0] == "single":
            panel = row[1]
            page.paste(cover_fit(panel.image, x1 - x0, y1 - y0), (x0, y0))
            draw.rectangle([x0, y0, x1, y1], outline=(20, 20, 20), width=5)
            if panel.caption:
                _edge_caption(draw, page, x0, x1, y1, panel.caption, font, "bottom")
        elif row[0] == "vpair":  # 垂直上下兩格（不對等高，寬水平格線，鐵律）
            top_p, bot_p = row[1], row[2]
            ratio = row[3] if len(row) > 3 else 0.5
            hg = max(8, int(min(W, H) * 0.022))  # 水平格線：寬
            split = int(y0 + (y1 - y0) * ratio)
            for py0, py1, panel in ((y0, split - hg // 2, top_p),
                                    (split + hg // 2, y1, bot_p)):
                if py1 <= py0:
                    continue
                page.paste(cover_fit(panel.image, x1 - x0, py1 - py0), (x0, py0))
                draw.rectangle([x0, py0, x1, py1], outline=(20, 20, 20), width=5)
                if panel.caption:
                    _edge_caption(draw, page, x0, x1, py1, panel.caption, font, "bottom")
        elif row[0] == "quad":  # 2x2 四宮格（窄垂直 / 寬水平格線，鐵律）
            ps = row[1:5]
            vg = max(3, int(min(W, H) * 0.008))
            hg = max(8, int(min(W, H) * 0.022))
            mx, my = (x0 + x1) // 2, (y0 + y1) // 2
            cells = [(x0, y0, mx - vg // 2, my - hg // 2),
                     (mx + vg // 2, y0, x1, my - hg // 2),
                     (x0, my + hg // 2, mx - vg // 2, y1),
                     (mx + vg // 2, my + hg // 2, x1, y1)]
            for (cx0, cy0, cx1, cy1), panel in zip(cells, ps):
                if cx1 <= cx0 or cy1 <= cy0:
                    continue
                page.paste(cover_fit(panel.image, cx1 - cx0, cy1 - cy0), (cx0, cy0))
                draw.rectangle([cx0, cy0, cx1, cy1], outline=(20, 20, 20), width=5)
                if panel.caption:
                    _edge_caption(draw, page, cx0, cx1, cy1, panel.caption, font, "bottom")
        elif row[0] == "pair":  # 遠景同源切左右不對等兩格（窄垂直格線，鐵律）
            left_p, right_p = row[1], row[2]
            ratio = row[3] if len(row) > 3 else 0.3
            vg = max(3, int(min(W, H) * 0.008))  # 垂直格線：窄
            split = int(x0 + (x1 - x0) * ratio)
            for px0, px1, panel in ((x0, split - vg // 2, left_p),
                                    (split + vg // 2, x1, right_p)):
                if px1 <= px0:
                    continue
                page.paste(cover_fit(panel.image, px1 - px0, y1 - y0), (px0, y0))
                draw.rectangle([px0, y0, px1, y1], outline=(20, 20, 20), width=5)
                if panel.caption:
                    _edge_caption(draw, page, px0, px1, y1, panel.caption, font, "bottom")
        else:  # duo：矩形內斜切上下兩格
            up_p, lo_p = row[1], row[2]
            upper, lower = hero_split_polys(x0, y0, x1, y1, tilt)
            for poly, panel in ((upper, up_p), (lower, lo_p)):
                xs = [p[0] for p in poly]; ys = [p[1] for p in poly]
                bx0, by0 = min(xs), min(ys)
                bw, bh = max(xs) - bx0, max(ys) - by0
                mask = Image.new("L", (bw, bh), 0)
                ImageDraw.Draw(mask).polygon([(px - bx0, py - by0) for px, py in poly], fill=255)
                page.paste(cover_fit(panel.image, bw, bh), (bx0, by0), mask)
            draw.line(upper + [upper[0]], fill=(20, 20, 20), width=5)  # 對角線+外框
            draw.rectangle([x0, y0, x1, y1], outline=(20, 20, 20), width=5)
            if up_p.caption:  # 上格字幕貼上緣
                _edge_caption(draw, page, x0, x1, y0, up_p.caption, font, "top")
            if lo_p.caption:  # 下格字幕貼下緣
                _edge_caption(draw, page, x0, x1, y1, lo_p.caption, font, "bottom")
    return page


def _inset_toward(p, c, px):
    dx, dy = c[0] - p[0], c[1] - p[1]
    d = math.hypot(dx, dy) or 1.0
    return (p[0] + dx / d * px, p[1] + dy / d * px)


def _busyness(img: Image.Image, x: int, y: int, w: int, h: int) -> float:
    """區域的亮度標準差：背景平坦→低、臉/角色細節多→高。用來避開角色放字幕。"""
    box = (max(0, x), max(0, y), min(img.width, x + w), min(img.height, y + h))
    if box[2] <= box[0] or box[3] <= box[1]:
        return 1e9
    return ImageStat.Stat(img.crop(box).convert("L")).stddev[0]


def compose_page_slanted(panels: list[Panel],
                         page_size: tuple[int, int] = (1080, 1920),
                         tilt: float = 0.05) -> Image.Image:
    """斜格拼接：每格四邊形遮罩裁切 + 斜邊框，字幕裁進斜邊。格高依 heat、保閱讀順序。"""
    W, H = page_size
    page = Image.new("RGB", (W, H), (250, 248, 244))
    draw = ImageDraw.Draw(page)
    polys = slanted_bands([p.heat for p in panels], tilt)
    font = _load_font(max(14, int(min(W, H) * 0.030)))
    pad = max(8, int(font.size * 0.4))
    line_h = int(font.size * 1.3)
    inset = max(3, int(min(W, H) * 0.006))  # gutter

    # Phase 1：斜格出圖（多邊形遮罩裁切）+ 斜邊框
    placed = []
    for panel, poly in zip(panels, polys):
        pts = [(x * W, y * H) for x, y in poly]
        cx = sum(p[0] for p in pts) / 4
        cy = sum(p[1] for p in pts) / 4
        ipts = [_inset_toward(p, (cx, cy), inset) for p in pts]  # 內縮做 gutter
        xs = [p[0] for p in ipts]
        ys = [p[1] for p in ipts]
        bx0, by0 = int(min(xs)), int(min(ys))
        bw, bh = int(max(xs)) - bx0, int(max(ys)) - by0
        if bw <= 0 or bh <= 0:
            continue
        local = [(px - bx0, py - by0) for px, py in ipts]
        mask = Image.new("L", (bw, bh), 0)
        ImageDraw.Draw(mask).polygon(local, fill=255)
        page.paste(cover_fit(panel.image, bw, bh), (bx0, by0), mask)  # 裁成斜邊
        draw.line(ipts + [ipts[0]], fill=(20, 20, 20), width=6, joint="curve")
        placed.append((panel, ipts))

    # Phase 2：字幕當後製 bubble 疊最上層 —— 貼合文字（塊狀不空）、挑最乾淨角落避開臉
    radius = max(6, int(font.size * 0.45))
    for panel, ipts in placed:
        if not panel.caption:
            continue
        xs = [p[0] for p in ipts]
        bx0, bx1 = int(min(xs)), int(max(xs))
        top_safe = int(max(ipts[0][1], ipts[1][1]))     # 兩上角較低者
        bottom_safe = int(min(ipts[2][1], ipts[3][1]))  # 兩下角較高者 → bubble 不超斜邊

        # 窄排成塊狀，但方塊貼合文字高度（不撐空盒）
        target_w = int((bx1 - bx0) * 0.30)
        lines = wrap_text(panel.caption, font, target_w - 2 * pad)
        tw = max((_text_width(font, ln) for ln in lines), default=0)
        bub_w = int(tw + 2 * pad)
        bub_h = int(line_h * len(lines) + 2 * pad)

        # 四角候選，挑底圖最乾淨（細節最少=非臉）的位置放
        sx0, sy0 = bx0 + inset, top_safe + inset
        sx1, sy1 = bx1 - inset, bottom_safe - inset
        cands = [(sx0, sy0), (sx1 - bub_w, sy0), (sx0, sy1 - bub_h), (sx1 - bub_w, sy1 - bub_h)]
        cands = [(max(sx0, min(cx, sx1 - bub_w)), max(sy0, min(cy, sy1 - bub_h)))
                 for cx, cy in cands]
        bx_l, by0 = min(cands, key=lambda p: _busyness(page, p[0], p[1], bub_w, bub_h))

        draw.rounded_rectangle([bx_l, by0, bx_l + bub_w, by0 + bub_h], radius=radius,
                               fill=(20, 20, 20), outline=(245, 245, 240), width=3)
        for li, ln in enumerate(lines):
            draw.text((bx_l + pad, by0 + pad + li * line_h), ln,
                      fill=(255, 255, 255), font=font)
    return page
