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
