"""字體大小預覽：拿現成 cache panel，用真實 compose_page_hero（1080×1920，
字幕 0.060=64px）疊兩種字幕看大小。不生任何圖。

用法：venv_simon/bin/python scripts/font_size_preview.py <panel.png>
"""
import glob
import os
import sys

sys.path.insert(0, ".")
from PIL import Image
from diary_comic.layout import Panel, compose_page_hero


def main():
    cands = sys.argv[1:] or sorted(
        glob.glob("records/diary_comic_cache/*.png"), key=os.path.getmtime, reverse=True)
    if not cands:
        print("找不到 cache panel")
        return
    img = Image.open(cands[0]).convert("RGB")
    short = Panel(image=img.copy(), heat=5, caption="討論設計公司的分紅制度與留才機制")
    longc = Panel(image=img.copy(), heat=9,
                  caption="忙著談分紅、颱風與露營，是把人生當專案在管理嗎？")
    # 兩列 single，各放一種字幕長度
    page = compose_page_hero([("single", short), ("single", longc)], (1080, 1920))
    out = "records/font_preview.png"
    page.save(out)
    print(f"✅ {out}　用 panel={os.path.basename(cands[0])}　字體=64px(0.060)")


if __name__ == "__main__":
    main()
