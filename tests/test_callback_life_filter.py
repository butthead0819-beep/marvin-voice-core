"""TDD — 源頭過濾：只收現實生活承諾（2026-10-02 Jack 拍板）。

1. commitment_to_callback: real_life=False 或缺欄位回 None，real_life=True 回 (speaker, text)。
2. session_summarizer: LLM 產生的 real_life 欄位（true / "true" / "True" vs false / 缺）正確映射為 bool。
3. suki_memory: enqueue_callback(..., life=True) 存入 item 帶 "life": True，預設為 False。
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import json
import time

import pytest
from recall_handler import PendingConfirmation
from session_summarizer import SessionSummarizer, commitment_to_callback
from suki_memory import MemoryManager


def test_commitment_to_callback_real_life_filtering():
    """commitment_to_callback 只接受 real_life=True，False 或缺欄位一律過濾（回 None）。"""
    # 缺 real_life 欄位
    c_missing = SimpleNamespace(speaker="大肚", task_text="出門買菜", direction="inbound")
    assert commitment_to_callback(c_missing) is None

    # real_life=False (例如遊戲任務)
    c_game = SimpleNamespace(speaker="大肚", task_text="摸狼豬收集蟻卵", direction="inbound", real_life=False)
    assert commitment_to_callback(c_game) is None

    # real_life=True (現實承諾)
    c_life = SimpleNamespace(speaker="大肚", task_text="出門買菜", direction="inbound", real_life=True)
    assert commitment_to_callback(c_life) == ("大肚", "出門買菜")


@pytest.mark.asyncio
async def test_session_summarizer_parses_real_life_flag():
    """summarizer 解析 LLM 回傳的 real_life: true/"true"/"True" -> True，其他/缺 -> False。"""
    llm_payload = {
        "summary": "大家討論了今天要做的事情。",
        "commitments": [
            {"speaker": "A", "text": "洗碗", "type": "todo", "real_life": True},
            {"speaker": "B", "text": "看醫生", "type": "todo", "real_life": "true"},
            {"speaker": "C", "text": "繳水費", "type": "todo", "real_life": "True"},
            {"speaker": "D", "text": "打龍王", "type": "todo", "real_life": False},
            {"speaker": "E", "text": "做鎬子", "type": "todo", "real_life": "false"},
            {"speaker": "F", "text": "採集草藥", "type": "todo"},  # 缺 real_life
        ]
    }
    mock_transcript = MagicMock()
    mock_summary = MagicMock()
    mock_groq = MagicMock()
    mock_groq.chat.completions.create = AsyncMock(
        return_value=MagicMock(choices=[MagicMock(message=MagicMock(content=json.dumps(llm_payload)))])
    )

    captured: list[PendingConfirmation] = []
    summarizer = SessionSummarizer(
        transcript_store=mock_transcript,
        summary_store=mock_summary,
        groq_client=mock_groq,
        owner_speaker="Jack",
        on_commitment_detected=captured.append,
    )

    now = time.time()
    mock_transcript.get_recent.return_value = [
        {"speaker": "A", "text": "你好", "timestamp": now - 20},
        {"speaker": "B", "text": "哈囉", "timestamp": now - 10},
        {"speaker": "C", "text": "嗨", "timestamp": now - 5},
    ]

    await summarizer.summarize_window(guild_id=1, window_start=now - 300, window_end=now)

    assert len(captured) == 6
    results = {c.speaker: c.real_life for c in captured}
    assert results["A"] is True
    assert results["B"] is True
    assert results["C"] is True
    assert results["D"] is False
    assert results["E"] is False
    assert results["F"] is False


def test_enqueue_callback_stores_life_flag(tmp_path):
    """suki_memory.enqueue_callback 存入的 item 包含 life 欄位。"""
    mem = MemoryManager(
        db_path=str(tmp_path / "cb.db"),
        json_compat_path=str(tmp_path / "cb.json"),
    )
    mem.enqueue_callback("Alice", "繳房租", shareable=True, life=True)
    mem.enqueue_callback("Alice", "採集木頭", shareable=True, life=False)
    mem.enqueue_callback("Alice", "預設項", shareable=True)

    items = mem.get_player_memory("Alice")["callback_queue"]
    assert len(items) == 3
    assert items[0]["text"] == "繳房租"
    assert items[0]["life"] is True
    assert items[1]["text"] == "採集木頭"
    assert items[1]["life"] is False
    assert items[2]["text"] == "預設項"
    assert items[2]["life"] is False


def test_on_commitment_detected_enqueues_with_life_flag():
    """接線守門：voice_controller 把現實生活承諾排進 queue 時必須帶 life=True——
    少了它，新舊事全部被 DJ 串場與主動追問的 life 過濾擋掉，功能靜默失效。"""
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from cogs.voice_controller import VoiceController
    from recall_handler import PendingConfirmation

    fake = SimpleNamespace(_pending_confirmations=[], bot=MagicMock())
    conf = PendingConfirmation(
        task_text="買烤雞叉子", speaker="大肚", direction="inbound", assignee="大肚",
        source_quote="", window_start=0.0, window_end=0.0, expires_at=0.0, real_life=True,
    )
    VoiceController._on_commitment_detected(fake, conf)
    fake.bot.router.memory.enqueue_callback.assert_called_once_with(
        "大肚", "買烤雞叉子", shareable=True, life=True)
