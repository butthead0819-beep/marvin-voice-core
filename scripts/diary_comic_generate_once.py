"""手動出一頁漫畫試看：production 同路徑（render_story + 真 img/text），
但繞過 _last_posted dedup、存 TEST 檔名、不動線上 state。

用法：venv_simon/bin/python scripts/diary_comic_generate_once.py
"""
import datetime
import sys
from pathlib import Path

sys.path.insert(0, ".")
import diary_comic_poster as poster
from diary_comic.render import render_story


def main():
    key = poster._key()
    if not key:
        print("無 GEMINI_PAID_API_KEY")
        return
    planned = poster.plan_latest_session(
        Path(poster.LOG_PATH).read_text(encoding="utf-8"), poster._db_rows)
    if not planned:
        print("plan_latest_session → None（無場次/太短/無爆笑精華）")
        return
    session, plan, end = planned
    print(f"場次收尾 {end} | 格式 {plan.format} | 高潮 {plan.highlight.laugher} "
          f"強度 {plan.highlight.strength}")

    try:
        from llm_paid import PaidUsageGuard
        guard = PaidUsageGuard()
    except Exception:
        guard = None

    npanels = 1 if plan.format == "meme" else 4
    if guard is not None and not guard.allow(poster.EST_USD_PER_IMG * npanels):
        print("💰 超 spending cap，今天不出")
        return

    page = render_story(
        plan, img_fn=poster._img_fn(key, guard), text_fn=poster._text_fn(key),
        cache_dir=poster.CACHE_DIR, page_size=(1080, 1920), variant="nano",
        day_index=datetime.date.today().toordinal())
    if page is None:
        print("render_story → None")
        return
    out = f"records/diary_comic_TEST_{end.replace(':','').replace(' ','_').replace('-','')}.png"
    page.save(out)
    print(f"✅ 已存 {out}　尺寸 {page.size}")


if __name__ == "__main__":
    main()
