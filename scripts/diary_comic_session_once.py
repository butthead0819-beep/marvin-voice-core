"""手動出一頁「保底/日常向」漫畫：用舊 render_session 路徑（不靠笑點，話題當 beats），
畫今晚最後一場。繞過 dedup、存 TEST 檔、不動線上 state。

用法：venv_simon/bin/python scripts/diary_comic_session_once.py
"""
import datetime
import sys
from pathlib import Path

sys.path.insert(0, ".")
import diary_comic_poster as poster
from diary_comic.parser import parse_log, dedupe_adjacent, eligible_sessions
from diary_comic.render import render_session


def main():
    key = poster._key()
    if not key:
        print("無 GEMINI_PAID_API_KEY")
        return
    sessions = eligible_sessions(dedupe_adjacent(parse_log(
        Path(poster.LOG_PATH).read_text(encoding="utf-8"))))
    session = sessions[-1]
    end = session[-1].ts_str
    print(f"場次 {session[0].ts_str} → {end}　{len(session)} 筆")

    try:
        from llm_paid import PaidUsageGuard
        guard = PaidUsageGuard()
    except Exception:
        guard = None

    page, layout, line = render_session(
        session, img_fn=poster._img_fn(key, guard), text_fn=poster._text_fn(key),
        cache_dir=poster.CACHE_DIR, page_size=(1080, 1920), variant="nano",
        force_layout="slant")  # 不用條漫，固定 4 格日漫
    out = f"records/diary_comic_TEST_{end.replace(':','').replace(' ','_').replace('-','')}.png"
    page.save(out)
    print(f"✅ 已存 {out}　版面={layout}　尺寸={page.size}")
    print(f"Marvin 收尾：{line or '（無）'}")


if __name__ == "__main__":
    main()
