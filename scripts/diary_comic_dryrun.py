"""漫畫流程 dry-run：跑昨晚 chat_summary_log.txt 的 production 路徑（render_session），
但**跳過生圖**，只印分格 / 故事順序 / 字幕。文字 LLM（punchline）照跑（不是 NB 生圖）。

用法：venv_simon/bin/python scripts/diary_comic_dryrun.py
"""
import sys
from pathlib import Path

sys.path.insert(0, ".")
from diary_comic.parser import (
    parse_log, dedupe_adjacent, eligible_sessions, choose_style,
    should_generate, reduce_to_topics, heat_score)
from diary_comic.camera import shot_for
from diary_comic.punchline import generate_page_punchline
from diary_comic_poster import _key, _text_fn, LOG_PATH

NO_LLM = "--no-llm" in sys.argv


def main():
    sessions = eligible_sessions(dedupe_adjacent(parse_log(
        Path(LOG_PATH).read_text(encoding="utf-8"))))
    if not sessions:
        print("沒有任何符合資格的場次")
        return
    session = sessions[-1]
    print(f"共 {len(sessions)} 個場次，取最後一個（=昨晚收尾那場）")
    print(f"場次：{session[0].ts_str} → {session[-1].ts_str}　原始 {len(session)} 筆")
    if not should_generate(session, min_entries=6):
        print("⚠️ should_generate=False（<6 筆）→ production 會跳過不出漫畫")

    layout = choose_style(session)
    page_entries = session[:8] if layout == "webtoon" else reduce_to_topics(session, 4)
    n = len(page_entries)
    heats = [heat_score(e) for e in page_entries]
    hero = max(range(n), key=lambda i: heats[i])

    text_fn = None if NO_LLM else _text_fn(_key())
    marvin_line = generate_page_punchline([e.core for e in page_entries], generate_fn=text_fn)

    if layout == "slant":
        partner = hero + 1 if hero + 1 < n else hero - 1
        char_idx = {hero, partner}
        aspects = ["16:9"] * n
    elif layout == "webtoon":
        char_idx = set(range(n))
        aspects = ["4:3"] * n
    else:  # stack
        char_idx = set(range(n))
        aspects = ["?"] * n  # 動態 box aspect，dry-run 不算

    print(f"\n版面 layout = {layout}（{n} 格，hero=第 {hero+1} 格）")
    print(f"連貫度 continuity = {None}")
    print("=" * 66)
    for i, e in enumerate(page_entries):
        caption = marvin_line if (i == hero and marvin_line) else e.core
        shot = shot_for(i, n, is_hero=(i == hero))
        tag = "🔥HERO" if i == hero else "      "
        kind = "角色" if i in char_idx else "物件only"
        print(f"\n[格 {i+1}] {tag}　heat={heats[i]}　{aspects[i]}　{kind}")
        print(f"   鏡頭：{shot}")
        print(f"   場景源：{e.core}")
        if e.speakers:
            print(f"   人物：{', '.join(e.speakers)}")
        if e.aside:
            print(f"   碎念：{e.aside}")
        print(f"   字幕：{caption}")
    print("\n" + "=" * 66)
    print(f"Marvin 收尾 punchline（hero 格字幕）：{marvin_line or '（無，text_fn 關或失敗）'}")


if __name__ == "__main__":
    main()
