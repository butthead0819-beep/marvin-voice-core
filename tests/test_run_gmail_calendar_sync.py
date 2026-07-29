"""tests/test_run_gmail_calendar_sync.py — TDD for scripts/run_gmail_calendar_sync.py Option A1 implementation.
"""
import pytest
from scripts.run_gmail_calendar_sync import (
    classify_sender,
    summarize_important_emails,
    load_google_credentials,
)


def test_classify_sender():
    assert classify_sender("service@ctbcbank.com") == "銀行通知"
    assert classify_sender("receipt@uber.com") == "發票郵件"
    assert classify_sender("notifications@github.com") == "工作郵件"
    assert classify_sender("no-reply@google.com") == "重要通知"
    assert classify_sender("random@friend.org") == "關注的信件"


@pytest.mark.asyncio
async def test_summarize_important_emails_cache_reuse():
    cached = [{
        "id": "t1",
        "subject": "舊信標題",
        "sender": "test@example.com",
        "date": "2026-07-29",
        "summary": "舊信摘要",
        "action_item": "無須動作",
        "priority": "low",
    }]

    raw_items = [
        {
            "id": "t1",
            "subject": "舊信標題",
            "sender": "test@example.com",
            "date": "2026-07-29",
            "snippet": "這是內文snippet",
            "category": "關注的信件",
        }
    ]

    res = await summarize_important_emails(raw_items, cached)
    assert len(res) == 1
    assert res[0]["summary"] == "舊信摘要"


def test_load_google_credentials_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.run_gmail_calendar_sync.TOKEN_PATH", str(tmp_path / "non_existent_tokens.json"))
    creds = load_google_credentials()
    assert creds is None
