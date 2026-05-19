"""
TDD 測試：ProfileCompressor

測試案例：
- test_is_stale_when_no_profile — 沒有 profile → is_stale True
- test_is_stale_false_when_fresh — 剛壓縮完 → is_stale False
- test_compress_skips_when_too_few_transcripts — 少於 5 筆 → compress 回傳 None
- test_compress_saves_profile — mock LLM 回傳文字，compress 後 get_profile 能拿到

用 :memory: db，mock _call_llm 不實際打 API
"""

import os
import time
from unittest.mock import AsyncMock, patch

import pytest

from transcript_store import TranscriptStore
from profile_compressor import ProfileCompressor

# CI 沒 GROQ_API_KEY 時跳過——ProfileCompressor.__init__ 建 Groq client，
# 沒 key 會在 fixture setup 階段就爆。
# TODO: Mock Groq client at module level so tests run unconditionally
# (記在 TODOS.md「test_profile_compressor — mock Groq client」)
pytestmark = pytest.mark.skipif(
    not os.getenv("GROQ_API_KEY"),
    reason="ProfileCompressor needs Groq client; set GROQ_API_KEY to run",
)


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def store():
    """共用記憶體 TranscriptStore"""
    return TranscriptStore(db_path=":memory:")


@pytest.fixture
def compressor(store):
    """共用 ProfileCompressor（記憶體 db，注入 store）"""
    return ProfileCompressor(db_path=":memory:", transcript_store=store)


# ── is_stale ──────────────────────────────────────────────────────────────────

def test_is_stale_when_no_profile(compressor):
    """沒有任何 profile 記錄時，is_stale 應回傳 True"""
    assert compressor.is_stale("Jack", guild_id=123) is True


def test_is_stale_false_when_fresh(compressor):
    """剛寫入 profile（updated_at = now）時，is_stale 應回傳 False"""
    # 直接在 db 寫入一筆剛壓縮的 profile
    compressor._upsert_profile("Jack", 123, "測試摘要", time.time())
    assert compressor.is_stale("Jack", guild_id=123, max_age_hours=24) is False


def test_is_stale_true_when_old(compressor):
    """profile 超過 max_age_hours 時，is_stale 應回傳 True"""
    old_ts = time.time() - 3600 * 25  # 25 小時前
    compressor._upsert_profile("Jack", 123, "舊摘要", old_ts)
    assert compressor.is_stale("Jack", guild_id=123, max_age_hours=24) is True


# ── get_profile ───────────────────────────────────────────────────────────────

def test_get_profile_returns_none_when_no_record(compressor):
    """沒有 profile 時，get_profile 應回傳 None"""
    assert compressor.get_profile("Jack", guild_id=123) is None


def test_get_profile_returns_text_after_upsert(compressor):
    """_upsert_profile 後，get_profile 應能拿到同一段文字"""
    compressor._upsert_profile("Jack", 123, "一些摘要文字", time.time())
    assert compressor.get_profile("Jack", guild_id=123) == "一些摘要文字"


# ── compress ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_compress_skips_when_too_few_transcripts(compressor, store):
    """逐字稿少於 5 筆時，compress 應直接回傳 None"""
    ts = time.time()
    for i in range(4):
        store.save("Jack", 123, f"第{i}句話", ts - i * 60)

    result = await compressor.compress("Jack", guild_id=123)
    assert result is None


@pytest.mark.asyncio
async def test_compress_saves_profile(compressor, store):
    """mock LLM 回傳文字，compress 後 get_profile 應能拿到該文字"""
    ts = time.time()
    for i in range(5):
        store.save("Jack", 123, f"這是第{i}句測試話語", ts - i * 60)

    mock_profile = "Jack 喜歡打遊戲，常提到台灣遊戲社群，說話直接。"

    with patch.object(compressor, "_call_llm", new=AsyncMock(return_value=mock_profile)):
        result = await compressor.compress("Jack", guild_id=123)

    assert result == mock_profile
    assert compressor.get_profile("Jack", guild_id=123) == mock_profile


@pytest.mark.asyncio
async def test_compress_calls_llm_with_speaker_name(compressor, store):
    """compress 傳給 _call_llm 的 prompt 必須包含 speaker 名稱"""
    ts = time.time()
    for i in range(5):
        store.save("Jack", 123, f"話語{i}", ts - i * 60)

    captured_prompt = {}

    async def capture_llm(prompt: str) -> str:
        captured_prompt["prompt"] = prompt
        return "摘要結果"

    with patch.object(compressor, "_call_llm", new=capture_llm):
        await compressor.compress("Jack", guild_id=123)

    assert "Jack" in captured_prompt.get("prompt", "")


# ── compress_if_stale ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_compress_if_stale_returns_existing_when_fresh(compressor, store):
    """profile 未過期時，compress_if_stale 應直接回傳已存的 profile，不呼叫 LLM"""
    compressor._upsert_profile("Jack", 123, "現有摘要", time.time())

    with patch.object(compressor, "_call_llm", new=AsyncMock()) as mock_llm:
        result = await compressor.compress_if_stale("Jack", guild_id=123)

    mock_llm.assert_not_called()
    assert result == "現有摘要"


@pytest.mark.asyncio
async def test_compress_if_stale_compresses_when_stale(compressor, store):
    """profile 已過期時，compress_if_stale 應呼叫 compress，更新並回傳新 profile"""
    # 先寫入過期 profile
    old_ts = time.time() - 3600 * 25
    compressor._upsert_profile("Jack", 123, "舊摘要", old_ts)

    # 補足逐字稿資料
    ts = time.time()
    for i in range(5):
        store.save("Jack", 123, f"新話語{i}", ts - i * 60)

    new_profile = "新的摘要文字"
    with patch.object(compressor, "_call_llm", new=AsyncMock(return_value=new_profile)):
        result = await compressor.compress_if_stale("Jack", guild_id=123)

    assert result == new_profile
    assert compressor.get_profile("Jack", guild_id=123) == new_profile
