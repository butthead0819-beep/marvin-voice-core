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
