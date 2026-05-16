"""
Marvin 轉化熱度報表產生器
用法：python scripts/twitch_report.py [頻道名] [--top N] [--days D]

分數設計：
  conversion_score = intent_score × loyalty_multiplier
  loyalty_multiplier = log(sessions + 1) × log(avg_msgs_per_session + 1)

  意圖分（intent_score）= 明確購買/訂閱信號
  忠誠乘數（loyalty）   = 從行為資料自動學出，不需要預設規則
  起哄、表情包、噪音都算進忠誠度，因為那是社群慣性的一部分
"""
import sqlite3
import sys
import json
import math
import re
from collections import Counter
from pathlib import Path
from datetime import datetime, timedelta, timezone

CHANNEL = sys.argv[1] if len(sys.argv) > 1 else "pinpinponpon627"
TOP_N   = 10
DAYS    = 30

for i, arg in enumerate(sys.argv):
    if arg == "--top"  and i + 1 < len(sys.argv): TOP_N = int(sys.argv[i + 1])
    if arg == "--days" and i + 1 < len(sys.argv): DAYS  = int(sys.argv[i + 1])

DB_PATH = Path(__file__).parent.parent / "marvin_twitch.db"

FUNNEL_STAGE = [
    (8, float("inf"), "核心粉絲"),
    (4, 7,            "忠實粉絲"),
    (2, 3,            "回訪粉絲"),
    (0, 1,            "初訪觀眾"),
]

def get_stage(sessions: int) -> str:
    for lo, hi, label in FUNNEL_STAGE:
        if lo <= sessions <= hi:
            return label
    return "未知"


# ── 頻道詞彙學習（從資料裡找出這個台的文化慣性）──────────────────────
NOISE_TOKENS = {
    "的", "了", "是", "我", "你", "他", "她", "在", "有", "不", "一", "這",
    "那", "也", "就", "都", "很", "and", "the", "a", "is", "to", "of",
    "it", "i", "you", "but", "lol", "haha", "xd",
}

def tokenize(text: str) -> list[str]:
    """簡單斷詞：取長度 ≥ 2 的中文詞組 或 英文詞"""
    tokens = []
    tokens += re.findall(r"[一-鿿]{2,}", text)
    tokens += re.findall(r"[a-zA-Z]{2,}", text.lower())
    return [t for t in tokens if t not in NOISE_TOKENS]


def discover_vocab(conn: sqlite3.Connection, channel: str, since: str, top_k: int = 30) -> list[tuple[str, int]]:
    """
    從全部訊息裡找出這個頻道最高頻的詞彙。
    這些詞代表這個台的文化慣性——你不用事先知道它們是什麼。
    """
    rows = conn.execute(
        "SELECT message FROM messages WHERE channel = ? AND ts > ?",
        (channel, since)
    ).fetchall()

    counter: Counter = Counter()
    for (msg,) in rows:
        counter.update(tokenize(msg))

    return counter.most_common(top_k)


# ── 語音-聊天室相關性 ─────────────────────────────────────────────────
def correlate_voice_to_chat(
    conn: sqlite3.Connection,
    channel: str,
    since: str,
    window_secs: int = 60,
) -> list[dict]:
    """
    找出每段語音之後 window_secs 秒內，聊天室的 intent 反應量。
    回傳按 intent_score_sum 降序排列的列表。

    非對稱視窗 [audio_ts, audio_ts + window_secs]：
    觀眾永遠在看到/聽到之後才打字，chat_ts < audio_ts 的訊息不算。
    """
    rows = conn.execute(
        """
        SELECT
            t.id,
            t.text         AS audio_text,
            t.ts           AS audio_ts,
            COUNT(m.id)    AS intent_msgs,
            COALESCE(SUM(m.intent_score), 0) AS intent_score_sum
        FROM stream_transcript t
        LEFT JOIN messages m
            ON  m.channel = t.channel
            AND m.intent_score > 0
            AND CAST(strftime('%s', m.ts) AS INTEGER)
                BETWEEN CAST(strftime('%s', t.ts) AS INTEGER)
                    AND CAST(strftime('%s', t.ts) AS INTEGER) + ?
        WHERE t.channel = ?
          AND t.ts >= ?
        GROUP BY t.id
        ORDER BY intent_score_sum DESC, t.ts DESC
        """,
        (window_secs, channel, since),
    ).fetchall()
    return [dict(r) for r in rows]


# ── 主報表 ────────────────────────────────────────────────────────────
def run_report(channel: str, top_n: int, days: int):
    if not DB_PATH.exists():
        print("❌ 還沒有資料。請先執行 twitch_collector.py 等她開台。")
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    # ── 每個用戶：意圖分 + 發言量 + 場次 ────────────────────────────
    user_rows = conn.execute("""
        SELECT
            m.username,
            COUNT(*)                                        AS total_msgs,
            SUM(m.intent_score)                            AS intent_total,
            GROUP_CONCAT(DISTINCT m.intent_type)           AS intents,
            COUNT(DISTINCT m.session_date)                 AS active_days
        FROM messages m
        WHERE m.channel = ? AND m.ts > ?
        GROUP BY m.username
    """, (channel, since)).fetchall()

    session_rows = conn.execute("""
        SELECT username, COUNT(*) AS session_count
        FROM user_sessions
        WHERE channel = ?
        GROUP BY username
    """, (channel,)).fetchall()
    session_map = {r["username"]: r["session_count"] for r in session_rows}

    # ── 每場平均發言量（用來算忠誠乘數）────────────────────────────
    per_session = conn.execute("""
        SELECT username, session_date, COUNT(*) AS msgs
        FROM messages
        WHERE channel = ? AND ts > ?
        GROUP BY username, session_date
    """, (channel, since)).fetchall()

    session_avg: dict[str, float] = {}
    tmp: dict[str, list[int]] = {}
    for r in per_session:
        tmp.setdefault(r["username"], []).append(r["msgs"])
    for u, counts in tmp.items():
        session_avg[u] = sum(counts) / len(counts)

    # ── 計算 conversion_score ──────────────────────────────────────
    results = []
    for row in user_rows:
        username     = row["username"]
        intent_total = row["intent_total"] or 0
        sc           = session_map.get(username, 1)
        avg_msgs     = session_avg.get(username, 1.0)

        # 忠誠乘數：回訪次數 × 每場發言量，取 log 壓縮量級
        loyalty = math.log(sc + 1) * math.log(avg_msgs + 1)

        # 有意圖才計算轉化分；純忠誠無意圖 = 社群核心但尚未準備轉化
        if intent_total > 0:
            conv_score = round(intent_total * loyalty, 2)
        else:
            conv_score = 0.0

        intents = [
            i for i in (row["intents"] or "").split(",")
            if i and i != "general"
        ]

        results.append({
            "username":    username,
            "conv_score":  conv_score,
            "loyalty":     round(loyalty, 2),
            "intent_total":intent_total,
            "sessions":    sc,
            "avg_msgs":    round(avg_msgs, 1),
            "total_msgs":  row["total_msgs"],
            "intents":     intents,
            "stage":       get_stage(sc),
        })

    # 分兩組排序：有意圖的按 conv_score，純忠誠的按 loyalty
    with_intent    = sorted([r for r in results if r["conv_score"] > 0],
                             key=lambda x: x["conv_score"], reverse=True)
    loyal_no_intent = sorted([r for r in results if r["conv_score"] == 0],
                              key=lambda x: x["loyalty"], reverse=True)

    # ── 整體統計 ──────────────────────────────────────────────────
    total_users = conn.execute(
        "SELECT COUNT(DISTINCT username) FROM messages WHERE channel = ? AND ts > ?",
        (channel, since)
    ).fetchone()[0]

    total_msgs = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE channel = ? AND ts > ?",
        (channel, since)
    ).fetchone()[0]

    missed = conn.execute(
        """SELECT COUNT(*) FROM messages
           WHERE channel = ? AND ts > ?
           AND intent_type IN ('subscription_intent','merch_intent')""",
        (channel, since)
    ).fetchone()[0]

    # ── 頻道詞彙（這個台的文化慣性）──────────────────────────────
    vocab = discover_vocab(conn, channel, since, top_k=20)

    # ── 語音-聊天室相關性（只在有語音資料時計算）────────────────────
    voice_correlation = correlate_voice_to_chat(conn, channel, since, window_secs=60)

    conn.close()

    # ── 輸出 ──────────────────────────────────────────────────────
    print(f"\n{'='*65}")
    print(f"  Marvin 轉化熱度報表 — #{channel}")
    print(f"  資料區間：最近 {days} 天")
    print(f"{'='*65}")
    print(f"  活躍觀眾：{total_users} 人  |  總訊息：{total_msgs} 則")
    print(f"  高意圖訊息（訂閱 + 周邊）：{missed} 則")
    print(f"{'='*65}\n")

    # 這個台學到的文化詞彙
    if vocab:
        print("  📖 這個台的高頻詞彙（文化慣性，不是噪音）\n")
        vocab_line = "  " + "  ".join(f"{w}({n})" for w, n in vocab[:15])
        print(vocab_line)
        print(f"\n  ＊ 以上是這個粉絲群的語言慣性，用來理解忠誠度的基礎\n")
        print(f"{'─'*65}\n")

    # 轉化熱區（有意圖 + 有忠誠）
    hot = with_intent[:top_n]
    print(f"  🔥 轉化熱區（意圖信號 × 忠誠度）\n")
    print(f"  {'#':<3} {'用戶名':<18} {'階段':<8} {'場次':<5} {'均發言':<7} {'轉化分':<8} {'信號'}")
    print(f"  {'─'*65}")
    for i, u in enumerate(hot, 1):
        intents_str = " ".join(u["intents"]) if u["intents"] else "—"
        print(f"  {i:<3} {u['username']:<18} {u['stage']:<8} {u['sessions']:<5} "
              f"{u['avg_msgs']:<7} {u['conv_score']:<8} {intents_str}")

    # 忠誠但尚未表態（養著，等時機）
    warming = loyal_no_intent[:10]
    if warming:
        print(f"\n  💛 社群核心（高忠誠，尚無明確意圖——等時機點名）\n")
        print(f"  {'#':<3} {'用戶名':<18} {'階段':<8} {'場次':<5} {'均發言':<7} {'忠誠分'}")
        print(f"  {'─'*50}")
        for i, u in enumerate(warming, 1):
            print(f"  {i:<3} {u['username']:<18} {u['stage']:<8} {u['sessions']:<5} "
                  f"{u['avg_msgs']:<7} {u['loyalty']}")

    # 語音觸發分析（只在有語音資料時顯示）
    hot_voice = [r for r in voice_correlation if r["intent_score_sum"] > 0]
    if hot_voice:
        print(f"\n  🎙️ 語音觸發分析（說了什麼 → 60s 內聊天室 intent 反應）\n")
        print(f"  {'#':<3} {'反應分':<7} {'則數':<5} {'語音內容（前 40 字）'}")
        print(f"  {'─'*65}")
        for i, r in enumerate(hot_voice[:10], 1):
            preview = r["audio_text"][:40].replace("\n", " ")
            print(f"  {i:<3} {r['intent_score_sum']:<7} {r['intent_msgs']:<5} {preview}")

    print(f"\n{'='*65}\n")

    # JSON 存檔
    now = datetime.now(timezone.utc)
    out_path = DB_PATH.parent / f"twitch_report_{channel}_{now.strftime('%Y%m%d_%H%M')}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "channel":      channel,
            "generated_at": now.isoformat(),
            "days":         days,
            "summary": {
                "total_users":          total_users,
                "total_messages":       total_msgs,
                "high_intent_messages": missed,
            },
            "channel_vocab":      [{"token": w, "count": n} for w, n in vocab],
            "hot_leads":          with_intent[:top_n],
            "loyal_community":    loyal_no_intent[:10],
            "voice_triggers":     hot_voice[:10],
        }, f, ensure_ascii=False, indent=2)

    print(f"  JSON 儲存：{out_path}\n")


if __name__ == "__main__":
    run_report(CHANNEL, TOP_N, DAYS)
