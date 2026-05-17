"""TDD — USERNOTICE (subgift / submysterygift / resub) + PRIVMSG bits 解析

驗項：
A) parse_usernotice 正常解析 subgift 格式 → 回 dict
B) parse_usernotice 解析 submysterygift → mass_subgift + amount=N
C) parse_usernotice 解析 resub → resub + amount=1
D) parse_usernotice 不認識的 msg-id → None
E) parse_bits_from_tags：tags 有 bits=N → int N；無則 0
F) init_db 創建 loyalty_events 表
G) save_loyalty_event 寫入正確欄位
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

# 讓 tests 能 import scripts/twitch_collector.py
SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))


# ── parse_badges ────────────────────────────────────────────────────────────

def test_parse_badges_extracts_sub_months_and_flags():
    from twitch_collector import parse_badges
    out = parse_badges("subscriber/24,vip/1,moderator/1", badge_info_str="subscriber/26")
    assert out["sub_months"] == 26    # badge-info 比 badges 精準，取大值
    assert out["is_vip"] == 1
    assert out["is_mod"] == 1
    assert out["is_founder"] == 0


def test_parse_badges_founder_detected():
    from twitch_collector import parse_badges
    out = parse_badges("founder/0,subscriber/12")
    assert out["is_founder"] == 1
    assert out["sub_months"] == 12


def test_parse_badges_empty():
    from twitch_collector import parse_badges
    out = parse_badges("", "")
    assert out == {"is_vip": 0, "is_mod": 0, "is_founder": 0, "is_broadcaster": 0, "sub_months": 0}


def test_parse_badges_malformed_does_not_crash():
    from twitch_collector import parse_badges
    out = parse_badges("garbage,subscriber/notanumber,vip", "")
    assert out["is_vip"] == 0  # vip 沒 "/N" 不算
    assert out["sub_months"] == 0  # notanumber 解析失敗


def test_parse_badges_caps_unreasonable_sub_months():
    """Partner channel 可設自訂 sub-badge tier ID（例如 subscriber/3012）。
    這不是真實月數（251 年顯然不可能），超過 200 視為自訂 badge tier 不採信。"""
    from twitch_collector import parse_badges
    out = parse_badges("subscriber/3012", "subscriber/3012")
    assert out["sub_months"] == 0, "subscriber/3012 應視為自訂 badge tier ID，不是月數"
    # 但 100 是合理的（8 年訂閱）
    out2 = parse_badges("subscriber/100", "")
    assert out2["sub_months"] == 100


def test_parse_badges_broadcaster_flagged():
    from twitch_collector import parse_badges
    out = parse_badges("broadcaster/1,subscriber/100", "")
    assert out["is_broadcaster"] == 1


# ── parse_sub_tier ──────────────────────────────────────────────────────────

def test_parse_sub_tier_mapping():
    from twitch_collector import parse_sub_tier
    assert parse_sub_tier("1000") == 1
    assert parse_sub_tier("2000") == 2
    assert parse_sub_tier("3000") == 3
    assert parse_sub_tier("Prime") == 1
    assert parse_sub_tier("") == 1
    assert parse_sub_tier("garbage") == 1


# ── 雜訊分類修正：gift_received / bot / system_message ─────────────────────

def test_classify_intent_gift_received_takes_precedence():
    """'@XXX 謝謝您的贈禮訂閱！' 應被分到 gift_received（已成交事件），不是 subscription_intent。"""
    from twitch_collector import classify_intent
    intent, score = classify_intent("@莓汽泡 謝謝您的贈禮訂閱！")
    assert intent == "gift_received"
    assert score == 0  # 已成交，不是潛在意圖


def test_classify_intent_subscription_keyword_still_works_when_not_gift_thanks():
    """正常的「想訂閱」「怎麼訂」訊息仍應被 subscription_intent 抓。"""
    from twitch_collector import classify_intent
    for msg in ["我想訂閱", "怎麼訂", "訂一個月有什麼好處"]:
        intent, _ = classify_intent(msg)
        assert intent == "subscription_intent", f"{msg!r} 應為 subscription_intent，得到 {intent}"


def test_is_bot_user_recognises_known_bots():
    from twitch_collector import is_bot_user
    assert is_bot_user("nightbot")
    assert is_bot_user("Nightbot")  # 大小寫不敏感
    assert is_bot_user("streamelements")
    assert is_bot_user("moobot")
    assert not is_bot_user("pjbitter")
    assert not is_bot_user("dou_wha")


def test_save_message_forces_bot_user_to_general_intent():
    """nightbot 等 bot 發訊息含「訂閱」字也不該被分到 subscription_intent。"""
    from twitch_collector import init_db, save_message
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        path = Path(tmp.name)
    try:
        conn = init_db(path)
        save_message(conn, "ch", "nightbot", "歡迎DC晃晃 訂閱者限定 連結", display_name="Nightbot")
        row = conn.execute("SELECT intent_type FROM messages WHERE username='nightbot'").fetchone()
        assert row[0] == "general", f"bot 訊息應 force 為 general，得到 {row[0]}"
        conn.close()
    finally:
        path.unlink(missing_ok=True)


def test_save_message_flags_system_message():
    """🎰 / 🐱 等 extension 訊息要被標記 is_system_message=1。"""
    from twitch_collector import init_db, save_message
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        path = Path(tmp.name)
    try:
        conn = init_db(path)
        save_message(conn, "ch", "pinpinponpon627", "🎰 目前獎池：跳舞30秒",
                     display_name="蘋蘋澎澎")
        save_message(conn, "ch", "pinpinponpon627", "謝謝大家今天的支持",
                     display_name="蘋蘋澎澎")
        rows = conn.execute(
            "SELECT message, is_system_message FROM messages WHERE channel='ch' ORDER BY id"
        ).fetchall()
        assert rows[0][1] == 1, "🎰 訊息要標記 is_system_message=1"
        assert rows[1][1] == 0, "一般訊息不該被標記"
        conn.close()
    finally:
        path.unlink(missing_ok=True)


def test_is_system_bot_message_recognises_extension_templates():
    """拉霸機 / 飽食度 等 broadcaster 帳號發的 bot extension 訊息。"""
    from twitch_collector import is_system_bot_message
    assert is_system_bot_message("🎰 目前獎池【活動】：蘋跳舞30秒")
    assert is_system_bot_message("🎰 機率｜蘋跳舞30秒 14.7%")
    assert is_system_bot_message("🎰 今日中獎（共 14 筆）：莓汽泡→潔自拍")
    assert is_system_bot_message("🎰 觸發條件：贈送 10 訂 / 1600 Bits")
    assert is_system_bot_message("🐱 喵～肚子餓（飽食度 29）")
    # 一般訊息不該被誤殺
    assert not is_system_bot_message("我想訂閱")
    assert not is_system_bot_message("早安啊蘋")
    assert not is_system_bot_message("謝謝大家今天的支持")


# ── classify_intent: question_intent ─────────────────────────────────────────

def test_classify_intent_question_mark_only():
    from twitch_collector import classify_intent
    intent, score = classify_intent("這個怎麼用？")
    assert intent == "question_intent"
    assert score == 2


def test_classify_intent_question_starts_with_qword():
    from twitch_collector import classify_intent
    for msg in ["請問現在幾點", "怎麼參加", "如何拿到身分"]:
        intent, score = classify_intent(msg)
        # 「身分」會被 community_inquiry 抓走（"身分組"），所以 "如何拿到身分" 仍可能是 community
        # 但「請問現在幾點」不含其他關鍵字 → 應為 question_intent
        if intent == "question_intent":
            assert score == 2


def test_classify_intent_specific_beats_question():
    """「想訂閱嗎？」應該是 subscription_intent，不是 question_intent。"""
    from twitch_collector import classify_intent
    intent, _ = classify_intent("想訂閱嗎？")
    assert intent == "subscription_intent"


def test_classify_intent_general_when_no_match():
    from twitch_collector import classify_intent
    intent, score = classify_intent("早安")
    assert intent == "general"
    assert score == 0


# ── parse_usernotice ────────────────────────────────────────────────────────

def test_parse_usernotice_subgift():
    from twitch_collector import parse_usernotice
    line = "@badge-info=;badges=premium/1;color=;display-name=GifterName;login=gifter_login;msg-id=subgift;msg-param-recipient-display-name=RcptName;msg-param-recipient-user-name=rcpt_login;msg-param-sub-plan=1000;room-id=12345 :tmi.twitch.tv USERNOTICE #ch"
    evt = parse_usernotice(line)
    assert evt is not None
    assert evt["event_type"] == "subgift"
    assert evt["username"] == "gifter_login"
    assert evt["display_name"] == "GifterName"
    assert evt["amount"] == 1
    assert evt["recipient"] == "rcpt_login"


def test_parse_usernotice_mass_subgift():
    from twitch_collector import parse_usernotice
    line = "@badge-info=;display-name=BigGifter;login=big_gifter;msg-id=submysterygift;msg-param-mass-gift-count=10;msg-param-sub-plan=1000;room-id=12345 :tmi.twitch.tv USERNOTICE #ch"
    evt = parse_usernotice(line)
    assert evt is not None
    assert evt["event_type"] == "mass_subgift"
    assert evt["username"] == "big_gifter"
    assert evt["amount"] == 10
    assert evt["recipient"] == ""


def test_parse_usernotice_resub():
    from twitch_collector import parse_usernotice
    line = "@badge-info=;display-name=Loyal;login=loyal_user;msg-id=resub;msg-param-cumulative-months=12;msg-param-sub-plan=1000;room-id=12345 :tmi.twitch.tv USERNOTICE #ch"
    evt = parse_usernotice(line)
    assert evt is not None
    assert evt["event_type"] == "resub"
    assert evt["username"] == "loyal_user"
    assert evt["amount"] == 1


def test_parse_usernotice_first_time_sub():
    from twitch_collector import parse_usernotice
    line = "@badge-info=;display-name=NewSub;login=new_sub;msg-id=sub;msg-param-cumulative-months=1;msg-param-sub-plan=1000;room-id=12345 :tmi.twitch.tv USERNOTICE #ch"
    evt = parse_usernotice(line)
    assert evt is not None
    assert evt["event_type"] == "sub"
    assert evt["username"] == "new_sub"


def test_parse_usernotice_unknown_msg_id_returns_none():
    from twitch_collector import parse_usernotice
    line = "@msg-id=raid;login=raider;room-id=12345 :tmi.twitch.tv USERNOTICE #ch"
    evt = parse_usernotice(line)
    assert evt is None


def test_parse_usernotice_non_usernotice_returns_none():
    from twitch_collector import parse_usernotice
    line = "@badges=...;display-name=Foo :foo!foo@foo.tmi.twitch.tv PRIVMSG #ch :hi"
    evt = parse_usernotice(line)
    assert evt is None


# ── parse_bits_from_tags ────────────────────────────────────────────────────

def test_parse_bits_present():
    from twitch_collector import parse_bits_from_tags
    assert parse_bits_from_tags({"bits": "500"}) == 500
    assert parse_bits_from_tags({"bits": "1"}) == 1


def test_parse_bits_absent_or_zero():
    from twitch_collector import parse_bits_from_tags
    assert parse_bits_from_tags({}) == 0
    assert parse_bits_from_tags({"bits": "0"}) == 0
    assert parse_bits_from_tags({"bits": "garbage"}) == 0


# ── init_db & save_loyalty_event ────────────────────────────────────────────

def test_init_db_creates_loyalty_events_table():
    from twitch_collector import init_db
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        path = Path(tmp.name)
    try:
        conn = init_db(path)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "loyalty_events" in tables, "init_db 必須創 loyalty_events 表"
        # schema 檢查
        cols = {r[1] for r in conn.execute("PRAGMA table_info(loyalty_events)").fetchall()}
        for required in ["channel", "username", "event_type", "amount", "ts"]:
            assert required in cols, f"loyalty_events 缺欄位 {required}"
        conn.close()
    finally:
        path.unlink(missing_ok=True)


def test_save_loyalty_event_inserts_row():
    from twitch_collector import init_db, save_loyalty_event
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        path = Path(tmp.name)
    try:
        conn = init_db(path)
        save_loyalty_event(conn, "ch", "gifter1", "GifterDN", "subgift", amount=1, recipient="rcpt")
        save_loyalty_event(conn, "ch", "cheerer1", "Cheerer", "cheer", amount=500)
        rows = conn.execute("SELECT username, event_type, amount, recipient FROM loyalty_events ORDER BY id").fetchall()
        assert len(rows) == 2
        assert rows[0] == ("gifter1", "subgift", 1, "rcpt")
        assert rows[1] == ("cheerer1", "cheer", 500, "")
        conn.close()
    finally:
        path.unlink(missing_ok=True)


# ── Dedup via IRC `id` tag — protects against USERNOTICE replays on reconnect ─

def test_save_loyalty_event_dedups_same_irc_id():
    """同一個 IRC id 重送 → 只寫入一筆。USERNOTICE 在 reconnect/supervisor backoff
    迴圈下會重播，沒去重就會雙倍計算禮物。"""
    from twitch_collector import init_db, save_loyalty_event
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        path = Path(tmp.name)
    try:
        conn = init_db(path)
        save_loyalty_event(conn, "ch", "g1", "G1", "subgift", amount=1, recipient="r", msg_id="abc-123")
        save_loyalty_event(conn, "ch", "g1", "G1", "subgift", amount=1, recipient="r", msg_id="abc-123")
        rows = conn.execute("SELECT COUNT(*) FROM loyalty_events").fetchone()
        assert rows[0] == 1, "same IRC id should not double-insert"
    finally:
        conn.close()
        path.unlink(missing_ok=True)


def test_save_loyalty_event_without_msg_id_still_inserts_each_time():
    """msg_id 缺漏（舊行為相容）時不要 dedup，仍逐筆寫入。"""
    from twitch_collector import init_db, save_loyalty_event
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        path = Path(tmp.name)
    try:
        conn = init_db(path)
        save_loyalty_event(conn, "ch", "g1", "G1", "subgift", amount=1)
        save_loyalty_event(conn, "ch", "g1", "G1", "subgift", amount=1)
        rows = conn.execute("SELECT COUNT(*) FROM loyalty_events").fetchone()
        assert rows[0] == 2
    finally:
        conn.close()
        path.unlink(missing_ok=True)


def test_save_loyalty_event_distinct_ids_both_stored():
    """不同 msg_id 應各自寫一筆。"""
    from twitch_collector import init_db, save_loyalty_event
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        path = Path(tmp.name)
    try:
        conn = init_db(path)
        save_loyalty_event(conn, "ch", "g1", "G1", "subgift", amount=1, msg_id="aaa")
        save_loyalty_event(conn, "ch", "g1", "G1", "subgift", amount=1, msg_id="bbb")
        rows = conn.execute("SELECT COUNT(*) FROM loyalty_events").fetchone()
        assert rows[0] == 2
    finally:
        conn.close()
        path.unlink(missing_ok=True)


def test_parse_usernotice_extracts_msg_id():
    """parse_usernotice 必須把 IRC `id` tag 透傳出來給 save_loyalty_event 用。"""
    from twitch_collector import parse_usernotice
    line = (
        "@id=abc-123;msg-id=subgift;login=gifter;display-name=Gifter;"
        "msg-param-recipient-user-name=rcpt;msg-param-sub-plan=1000 "
        ":tmi.twitch.tv USERNOTICE #chan"
    )
    evt = parse_usernotice(line)
    assert evt is not None
    assert evt["msg_id"] == "abc-123"


# ── Commit batching — save_message defers commit; flusher commits on cadence ──

def test_save_message_does_not_commit_per_call():
    """新 save_message 不在每次呼叫時 commit。改由 flush_pending_commit() 控制。
    熱門頻道 50+ msg/s 時 commit-per-call 會把單 worker executor 拖到落後實時。"""
    from twitch_collector import init_db, save_message
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        path = Path(tmp.name)
    try:
        # 第二個 connection 在 commit 前看不到新資料（WAL 隔離）
        conn = init_db(path)
        reader = sqlite3.connect(path)
        save_message(conn, "ch", "alice", "hello")
        rows_before_flush = reader.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        assert rows_before_flush == 0, (
            "save_message commit-per-call defeats batching — should defer commit"
        )
        reader.close()
    finally:
        conn.close()
        path.unlink(missing_ok=True)


def test_flush_pending_commit_makes_rows_visible():
    """flush_pending_commit() 之後新 row 立即可見。"""
    from twitch_collector import init_db, save_message, flush_pending_commit
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        path = Path(tmp.name)
    try:
        conn = init_db(path)
        save_message(conn, "ch", "alice", "hello")
        save_message(conn, "ch", "bob", "world")
        flush_pending_commit(conn)
        reader = sqlite3.connect(path)
        rows = reader.execute("SELECT username, message FROM messages ORDER BY id").fetchall()
        reader.close()
        assert rows == [("alice", "hello"), ("bob", "world")]
    finally:
        conn.close()
        path.unlink(missing_ok=True)


# ── save_message coverage — the 7 new fields + user_profiles upsert + dn fallback ─

def test_save_message_writes_all_new_fields():
    """All 7 identity/behavior columns persist with the values provided."""
    from twitch_collector import init_db, save_message, flush_pending_commit
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        path = Path(tmp.name)
    try:
        conn = init_db(path)
        save_message(
            conn, "ch", "alice", "hi",
            display_name="Alice", is_subscriber=1,
            is_first_msg=1, is_returning_chatter=0, sub_months=12,
            is_vip=1, is_mod=0, is_founder=1,
        )
        flush_pending_commit(conn)
        row = conn.execute(
            "SELECT display_name, is_subscriber, is_first_msg, is_returning_chatter, "
            "sub_months, is_vip, is_mod, is_founder FROM messages"
        ).fetchone()
        assert row == ("Alice", 1, 1, 0, 12, 1, 0, 1), f"got {row}"
    finally:
        conn.close()
        path.unlink(missing_ok=True)


def test_save_message_falls_back_to_username_when_display_name_empty():
    """Empty display_name → store username as display_name (downstream display safety)."""
    from twitch_collector import init_db, save_message, flush_pending_commit
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        path = Path(tmp.name)
    try:
        conn = init_db(path)
        save_message(conn, "ch", "alice", "hi", display_name="")
        flush_pending_commit(conn)
        row = conn.execute("SELECT display_name FROM messages").fetchone()
        assert row == ("alice",)
        # user_profiles upsert path should also have stored the fallback.
        prof = conn.execute(
            "SELECT display_name FROM user_profiles WHERE username = 'alice'"
        ).fetchone()
        assert prof == ("alice",)
    finally:
        conn.close()
        path.unlink(missing_ok=True)


def test_save_message_upserts_user_profile():
    """Same (channel, username) on second insert updates display_name + is_subscriber."""
    from twitch_collector import init_db, save_message, flush_pending_commit
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        path = Path(tmp.name)
    try:
        conn = init_db(path)
        save_message(conn, "ch", "alice", "msg1", display_name="AliceOld", is_subscriber=0)
        save_message(conn, "ch", "alice", "msg2", display_name="AliceNew", is_subscriber=1)
        flush_pending_commit(conn)
        rows = conn.execute(
            "SELECT display_name, is_subscriber FROM user_profiles WHERE username = 'alice'"
        ).fetchall()
        assert len(rows) == 1, "should be exactly one profile row per (channel, username)"
        assert rows[0] == ("AliceNew", 1), "upsert should reflect latest values"
    finally:
        conn.close()
        path.unlink(missing_ok=True)


def test_save_message_records_user_session():
    """user_sessions row created with (channel, username, session_date) on first message of the day."""
    from twitch_collector import init_db, save_message, flush_pending_commit
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        path = Path(tmp.name)
    try:
        conn = init_db(path)
        save_message(conn, "ch", "alice", "first today")
        save_message(conn, "ch", "alice", "second today")
        flush_pending_commit(conn)
        rows = conn.execute(
            "SELECT COUNT(*) FROM user_sessions WHERE channel = 'ch' AND username = 'alice'"
        ).fetchone()
        # INSERT OR IGNORE on (channel, username, session_date) → only one row for same day
        assert rows[0] == 1
    finally:
        conn.close()
        path.unlink(missing_ok=True)
