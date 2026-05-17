"""
直播結束後自動產生 pitch demo HTML
用法：python scripts/gen_pitch_html.py [channel] [--days 1]
      通常由 stream_session.py 結尾自動呼叫
"""
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT     = Path(__file__).parent.parent
DB_PATH  = ROOT / "marvin_twitch.db"
ASSETS   = ROOT / "assets"
TEMPLATE = ASSETS / "pitch_demo_0516.html"   # 結構模板（含 drawer / CSS）

BOT_USERS = {"nightbot", "moobot", "streamelements", "streamlabs", "fossabot"}
BOT_VOCAB = {
    "歡迎dc", "訂閱者限定", "有團長啊蘋隨時出沒跟福利唷", "請將", "帳號與",
    "訂閱帳號進行綁定", "因為訊息量比較多", "如果主播漏看了可以多說幾次唷", "https",
}
FUNNEL = [
    (8, float("inf"), "核心粉絲", "stage-core"),
    (4, 7,            "忠實粉絲", "stage-loyal"),
    (2, 3,            "回訪粉絲", "stage-return"),
    (0, 1,            "初訪觀眾", "stage-new"),
]
INTENT_LABELS   = {"subscription_intent": "訂閱", "merch_intent": "周邊", "community_inquiry": "社群"}
INTENT_TAG_CLS  = {"subscription_intent": "tag-sub", "merch_intent": "tag-merch"}

def get_stage(sessions: int) -> tuple[str, str]:
    for lo, hi, label, cls in FUNNEL:
        if lo <= sessions <= hi:
            return label, cls
    return "未知", "stage-new"

def js(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)

def re_replace(html: str, pattern: str, replacement: str) -> str:
    return re.sub(pattern, replacement, html, flags=re.DOTALL)


def generate(channel: str, days: int = 1) -> Path | None:
    # ── 找最新報表 JSON ──────────────────────────────────────────────
    reports = sorted(ROOT.glob(f"twitch_report_{channel}_*.json"),
                     key=lambda p: p.stat().st_mtime)
    if not reports:
        print(f"[gen_pitch_html] ERROR: 找不到 {channel} 的報表 JSON")
        return None
    report_path = reports[-1]
    report = json.loads(report_path.read_text(encoding="utf-8"))
    summary = report["summary"]

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    # ── Display name map（從 user_profiles，有 IRC tags 才有資料）───
    try:
        profile_rows = conn.execute(
            "SELECT username, display_name, is_subscriber FROM user_profiles WHERE channel = ?",
            (channel,),
        ).fetchall()
    except Exception:
        profile_rows = []  # 表尚未建立（舊 DB 或首次執行）
    dn_map: dict[str, dict] = {}
    subscriber_set: set[str] = set()
    for r in profile_rows:
        dn = r["display_name"]
        if dn and dn != r["username"]:
            dn_map[r["username"]] = {"name": dn, "fromTags": True}
        if r["is_subscriber"]:
            subscriber_set.add(r["username"])
    dn_map["nightbot"]  = {"name": "Nightbot",   "fromTags": False}
    dn_map[channel]     = {"name": channel,       "fromTags": False}

    # ── Top viewers ─────────────────────────────────────────────────
    top_rows = conn.execute("""
        SELECT username, COUNT(*) AS msgs
        FROM messages WHERE channel = ? AND ts > ?
        GROUP BY username ORDER BY msgs DESC LIMIT 25
    """, (channel, since)).fetchall()

    # 訂閱事件用戶（從 intent 訊息推斷）
    intent_raw = conn.execute("""
        SELECT username, message, intent_type
        FROM messages
        WHERE channel = ? AND ts > ?
          AND intent_type IN ('subscription_intent','merch_intent','community_inquiry')
        ORDER BY username, rowid
    """, (channel, since)).fetchall()

    sub_event_users: set[str] = {r["username"] for r in intent_raw if r["username"] not in BOT_USERS}

    # 熱名單排名 map
    hot_rank: dict[str, int] = {}
    rank_i = 1
    for lead in report["hot_leads"]:
        if lead["username"] not in BOT_USERS:
            hot_rank[lead["username"]] = rank_i
            rank_i += 1

    viewers_data = []
    for i, v in enumerate(top_rows, 1):
        uname = v["username"]
        is_bot = uname in BOT_USERS
        is_sub = uname in subscriber_set or uname in sub_event_users
        if is_bot:
            sub_status, sub_label = "bot",     "機器人"
        elif uname == channel:
            sub_status, sub_label = "owner",   "主播"
        elif is_sub:
            sub_status, sub_label = "new_sub", "本場訂閱"
        else:
            sub_status, sub_label = "unknown", "未知"
        note = f"熱名單 #{hot_rank[uname]}" if uname in hot_rank else ""
        viewers_data.append({
            "rank": i, "username": uname, "msgs": v["msgs"],
            "subStatus": sub_status, "subLabel": sub_label, "note": note,
        })

    # ── Intent messages ──────────────────────────────────────────────
    bot_intent_count = conn.execute("""
        SELECT COUNT(*) FROM messages WHERE channel = ? AND ts > ?
          AND intent_type IN ('subscription_intent','merch_intent','community_inquiry')
          AND username = 'nightbot'
    """, (channel, since)).fetchone()[0]

    intent_msgs = []
    for r in intent_raw:
        if r["username"] in BOT_USERS:
            continue
        is_gift = "贈禮訂閱" in r["message"]
        intent_msgs.append({
            "username": r["username"],
            "text":     r["message"],
            "type":     "gift_sub" if is_gift else "new_sub",
            "label":    "收贈禮訂閱" if is_gift else INTENT_LABELS.get(r["intent_type"], "訂閱"),
        })
    if bot_intent_count > 0:
        intent_msgs.append({
            "username": "nightbot",
            "text":     "歡迎DC晃晃❤️有團長啊蘋隨時出沒跟福利唷！訂閱者限定→https://discord.gg/kW4EDSN238 《請將DC帳號與Twitch帳號進行綁定…》",
            "type":     "bot",
            "label":    "機器人自動回覆",
            "count":    bot_intent_count,
        })

    # ── Hot leads (filtered) ─────────────────────────────────────────
    hot_leads = [l for l in report["hot_leads"] if l["username"] not in BOT_USERS][:10]

    leads_js = []
    for i, lead in enumerate(hot_leads, 1):
        stage_label, stage_cls = get_stage(lead["sessions"])
        intent_key = lead["intents"][0] if lead["intents"] else ""
        leads_js.append({
            "rank":       i,
            "username":   lead["username"],
            "stage":      stage_label,
            "stageClass": stage_cls,
            "sessions":   lead["sessions"],
            "score":      lead["conv_score"],
            "tag":        INTENT_LABELS.get(intent_key, "—"),
            "tagClass":   INTENT_TAG_CLS.get(intent_key, "tag-comm"),
        })

    leads_detail = []
    gift_users = {m["username"] for m in intent_msgs if m["type"] == "gift_sub"}
    for i, lead in enumerate(hot_leads, 1):
        sub_status = "收贈禮訂閱" if lead["username"] in gift_users else "本場新訂閱"
        leads_detail.append({
            "username":    lead["username"],
            "score":       lead["conv_score"],
            "loyalty":     lead["loyalty"],
            "intentTotal": lead["intent_total"],
            "msgs":        lead["total_msgs"],
            "intents":     lead["intents"],
            "subStatus":   sub_status,
            "note":        "",
        })

    # ── Vocab (bot-filtered) ─────────────────────────────────────────
    vocab_js = []
    for i, v in enumerate(report["channel_vocab"]):
        token = v["token"]
        if any(b in token.lower() for b in BOT_VOCAB):
            continue
        size = "lg" if i < 2 else "md" if i < 8 else "sm"
        vocab_js.append({"token": token, "size": size})
        if len(vocab_js) >= 15:
            break

    # ── Chat msgs（取真實有意圖的訊息 + 高發言量用戶）───────────────
    chat_rows = conn.execute("""
        SELECT username, message, intent_type FROM messages
        WHERE channel = ? AND ts > ? AND username NOT IN ('nightbot', ?)
        ORDER BY intent_score DESC, RANDOM()
        LIMIT 40
    """, (channel, since, channel)).fetchall()

    colors = ["c-accent", "c-green", "c-gold", "c-muted"]
    chat_msgs = []
    seen_chat: set[str] = set()
    for r in chat_rows:
        if r["username"] in seen_chat or len(chat_msgs) >= 15:
            continue
        seen_chat.add(r["username"])
        intent_map = {"subscription_intent": "sub", "merch_intent": "merch", "community_inquiry": "comm"}
        entry: dict = {
            "user":  r["username"],
            "color": colors[len(chat_msgs) % len(colors)],
            "text":  r["message"][:50],
        }
        if r["intent_type"] in intent_map:
            entry["intent"] = intent_map[r["intent_type"]]
        chat_msgs.append(entry)

    conn.close()

    # ── 讀模板 HTML ──────────────────────────────────────────────────
    html = TEMPLATE.read_text(encoding="utf-8")

    now       = datetime.now(timezone.utc)
    date_label = f"{now.month}/{now.day} 完整場次資料（真實數據）"

    # header badge
    html = re.sub(r'\d+/\d+ 完整場次資料（真實數據）', date_label, html)

    # DISPLAY_NAME_MAP
    html = re_replace(html,
        r'const DISPLAY_NAME_MAP = \{.*?\};',
        f'const DISPLAY_NAME_MAP = {js(dn_map)};')

    # VIEWERS_DATA
    html = re_replace(html,
        r'const VIEWERS_DATA = \[.*?\];',
        f'const VIEWERS_DATA = {js(viewers_data)};')

    # INTENT_MSGS
    html = re_replace(html,
        r'const INTENT_MSGS = \[.*?\];',
        f'const INTENT_MSGS = {js(intent_msgs)};')

    # LEADS_DETAIL
    html = re_replace(html,
        r'const LEADS_DETAIL = \[.*?\];',
        f'const LEADS_DETAIL = {js(leads_detail)};')

    # LEADS (main table, with comment)
    html = re_replace(html,
        r'// 資料來源：[^\n]+\nconst LEADS = \[.*?\];',
        f'// 資料來源：{report_path.name}\nconst LEADS = {js(leads_js)};')

    # VOCAB
    html = re_replace(html,
        r'// 真實詞彙資料[^\n]*\nconst VOCAB = \[.*?\];',
        f'// 真實詞彙資料（已過濾 Nightbot 機器人訊息片段）\nconst VOCAB = {js(vocab_js)};')

    # CHAT_MSGS
    html = re_replace(html,
        r'// 真實用戶名[^\n]*\nconst CHAT_MSGS = \[.*?\];',
        f'// 真實用戶名（真實訊息）\nconst CHAT_MSGS = {js(chat_msgs)};')

    # animateCounter stats
    total_users = summary["total_users"]
    high_intent = summary["high_intent_messages"]
    leads_count = len(leads_js)
    html = re.sub(r"animateCounter\(document\.getElementById\('stat-viewers'\),\d+\);",
                  f"animateCounter(document.getElementById('stat-viewers'),{total_users});", html)
    html = re.sub(r"animateCounter\(document\.getElementById\('stat-missed'\),\d+\);",
                  f"animateCounter(document.getElementById('stat-missed'),{high_intent});", html)
    html = re.sub(r"animateCounter\(document\.getElementById\('stat-leads'\),\d+\);",
                  f"animateCounter(document.getElementById('stat-leads'),{leads_count});", html)

    # ── 輸出 ────────────────────────────────────────────────────────
    date_str = now.strftime("%m%d")
    out_path = ASSETS / f"pitch_demo_{date_str}.html"
    out_path.write_text(html, encoding="utf-8")
    print(f"[gen_pitch_html] ✅ 已生成：{out_path}")
    return out_path


if __name__ == "__main__":
    ch   = sys.argv[1] if len(sys.argv) > 1 else "pinpinponpon627"
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    generate(ch, days)
