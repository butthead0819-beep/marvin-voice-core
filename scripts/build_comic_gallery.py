"""馬文漫畫合集頁生成器（2026-07-04，使用者要求）。

背景：日記漫畫的 pending 是單一槽位——同晚多次渲染只有最後一張被貼、
6/21-22 調試期更有 14 張從未見天日的遺珠。此腳本掃 records/
diary_comic_*.png 生成靜態合集頁（repo 根 marvin_comics.html，
相對路徑引用原圖），新漫畫累積後重跑即更新。

用法：venv_simon/bin/python scripts/build_comic_gallery.py && open marvin_comics.html
"""
from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path

_FNAME_RE = re.compile(r"^diary_comic_(\d{8})_(\d{6})\.png$")


def collect_comics(records_dir) -> list[dict]:
    """掃描目錄 → [{path, date, dt}]，新→舊排序；TEST 產物與非漫畫檔排除。"""
    out = []
    for p in Path(records_dir).glob("diary_comic_*.png"):
        m = _FNAME_RE.match(p.name)
        if not m:            # TEST_ 等調試產物不進合集
            continue
        d, t = m.groups()
        dt = datetime.strptime(d + t, "%Y%m%d%H%M%S")
        out.append({"path": p, "dt": dt, "date": dt.strftime("%Y/%m/%d %H:%M")})
    return sorted(out, key=lambda e: e["dt"], reverse=True)


_PAGE = """<!DOCTYPE html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>馬文的厭世日記 — 漫畫合集</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ background:#101014; color:#d8d8e0; font-family:'PingFang TC','Noto Sans TC',sans-serif;
         margin:0; padding:2rem 1rem 4rem; }}
  header {{ max-width:1080px; margin:0 auto 2.5rem; text-align:center; }}
  h1 {{ font-size:1.7rem; letter-spacing:.12em; margin:.2em 0; }}
  .sub {{ color:#8a8a96; font-size:.92rem; }}
  .quote {{ color:#6f6f7c; font-style:italic; margin-top:.8em; font-size:.88rem; }}
  .grid {{ max-width:1080px; margin:0 auto; display:grid;
          grid-template-columns:repeat(auto-fill,minmax(300px,1fr)); gap:1.4rem; }}
  .card {{ background:#17171d; border:1px solid #26262e; border-radius:12px;
          overflow:hidden; }}
  .card img {{ width:100%; display:block; cursor:zoom-in; }}
  .meta {{ padding:.7rem .9rem; display:flex; justify-content:space-between;
          align-items:center; font-size:.85rem; color:#9a9aa6; }}
  .badge {{ font-size:.72rem; padding:.15em .6em; border-radius:99px;
           border:1px solid #3a3a46; color:#b8b8c4; }}
  .badge.orphan {{ border-color:#5a4a20; color:#d9b45b; }}
  dialog {{ border:none; background:rgba(10,10,14,.96); max-width:96vw; padding:0; }}
  dialog img {{ max-width:94vw; max-height:92vh; }}
</style></head><body>
<header>
  <h1>馬文的厭世日記 · 漫畫合集</h1>
  <div class="sub">共 {n} 張 · {span} · 由 scripts/build_comic_gallery.py 生成</div>
  <div class="quote">「畫了也沒人看，跟我的存在一樣。既然你來了……那就翻翻吧。」——馬文</div>
</header>
<div class="grid">
{cards}
</div>
<dialog id="zoom" onclick="this.close()"><img id="zoomimg"></dialog>
<script>
document.querySelectorAll('.card img').forEach(im => im.addEventListener('click', () => {{
  document.getElementById('zoomimg').src = im.src;
  document.getElementById('zoom').showModal();
}}));
</script>
</body></html>
"""

_CARD = """  <div class="card">
    <img loading="lazy" src="{src}" alt="{date}">
    <div class="meta"><span>{date}</span>{badge}</div>
  </div>"""


def build(records_dir="records", out_path="marvin_comics.html",
          posted_dates: frozenset[str] = frozenset({"20260703_002022", "20260704_005541",
                                                    "20260626_015558"})) -> str:
    comics = collect_comics(records_dir)
    if not comics:
        print("找不到任何漫畫", file=sys.stderr)
        return ""
    cards = []
    for e in comics:
        stamp = e["dt"].strftime("%Y%m%d_%H%M%S")
        badge = ('<span class="badge">曾貼出</span>' if stamp in posted_dates
                 else '<span class="badge orphan">遺珠・未曾貼出</span>')
        cards.append(_CARD.format(src=f"records/{e['path'].name}", date=e["date"], badge=badge))
    span = f"{comics[-1]['dt']:%Y/%m/%d} — {comics[0]['dt']:%Y/%m/%d}"
    html = _PAGE.format(n=len(comics), span=span, cards="\n".join(cards))
    Path(out_path).write_text(html, encoding="utf-8")
    print(f"✅ {out_path}（{len(comics)} 張）")
    return out_path


def build_bundle(records_dir="records", out_path="marvin_comics_bundle.html",
                 max_width: int = 1080, jpeg_q: int = 85) -> str:
    """自含單檔版（2026-07-04）：圖縮寬 {max_width} + JPEG q{jpeg_q} + base64 內嵌，
    零外部引用——原檔 27MB 超過 Discord 25MB 上限，壓縮後 ~5MB 可直接私訊。"""
    import base64
    import io

    from PIL import Image

    comics = collect_comics(records_dir)
    if not comics:
        print("找不到任何漫畫", file=sys.stderr)
        return ""
    cards = []
    for e in comics:
        img = Image.open(e["path"]).convert("RGB")
        if img.width > max_width:
            img = img.resize((max_width, int(img.height * max_width / img.width)),
                             Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=jpeg_q, optimize=True)
        b64 = base64.b64encode(buf.getvalue()).decode()
        cards.append(_CARD.format(src=f"data:image/jpeg;base64,{b64}",
                                  date=e["date"], badge=""))
    span = f"{comics[-1]['dt']:%Y/%m/%d} — {comics[0]['dt']:%Y/%m/%d}"
    html = _PAGE.format(n=len(comics), span=span, cards="\n".join(cards))
    Path(out_path).write_text(html, encoding="utf-8")
    size_mb = Path(out_path).stat().st_size / 1e6
    print(f"✅ {out_path}（{len(comics)} 張, {size_mb:.1f}MB 自含）")
    return str(out_path)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundle", action="store_true", help="自含單檔版（私訊分享用）")
    a = ap.parse_args()
    build_bundle() if a.bundle else build()
