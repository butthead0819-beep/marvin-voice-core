"""聊天精華處理器 CLI：讀 marvin.db 逐字稿 → 找爆笑時刻 + 笑點前情。

用法：venv_simon/bin/python scripts/find_highlights.py [db路徑] [幾小時內] [最多幾則]
洞察：一群人同時哈哈笑，前幾句一定是精華。
"""
import datetime
import sqlite3
import sys

sys.path.insert(0, ".")
from diary_comic.highlight import find_highlights, is_laugh


def main():
    db = sys.argv[1] if len(sys.argv) > 1 else "marvin.db"
    hours = int(sys.argv[2]) if len(sys.argv) > 2 else 24 * 14
    limit = int(sys.argv[3]) if len(sys.argv) > 3 else 15

    con = sqlite3.connect(db)
    cutoff = datetime.datetime.now().timestamp() - hours * 3600
    rows = con.execute(
        "SELECT speaker, text, timestamp FROM transcripts "
        "WHERE timestamp >= ? ORDER BY timestamp ASC", (cutoff,)).fetchall()
    con.close()

    highlights = find_highlights(rows)
    print(f"DB={db}　近 {hours}h 共 {len(rows)} 句　找到 {len(highlights)} 個爆笑精華\n")
    for h in highlights[-limit:]:
        when = datetime.datetime.fromtimestamp(h.ts).strftime("%m-%d %H:%M")
        print(f"=== {when}　{h.laugher} 爆笑（強度 {h.strength}）===")
        for sp, txt in h.setup:
            print(f"   {sp}: {txt[:50]}")
        print(f"   → 😂 {h.laugh_text[:20]}\n")


if __name__ == "__main__":
    main()
