"""
tests/test_topic_generator_uses_living_profile.py
TDD 測試：TopicGenerator.generate_topics() 行為驗證

Mock 策略：
- FakeVectorStore：不打真實 ChromaDB
- FakeTranscriptStore：不打真實 SQLite
- FakeGroqClient：不打真實 Groq API
- voice_members 用 MagicMock(id=..., bot=False)
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, AsyncMock


# ── Fake 下層元件 ─────────────────────────────────────────────────────────────

class FakeVectorStore:
    def __init__(self, profiles: dict[str, str] | None = None):
        # profiles: {speaker_id: profile_str}
        self._profiles = profiles or {}

    def get_profiles_bulk(self, speaker_ids: list[str], guild_id: str) -> list[str]:
        return [self._profiles[sid] for sid in speaker_ids if sid in self._profiles]


class FakeTranscriptStore:
    def __init__(self, recent: list[dict] | None = None):
        self._recent = recent or []

    def get_recent(self, speaker, guild_id, minutes: int = 10) -> list[dict]:
        return self._recent


def make_groq_client(content: str = "1. 聊聊最近的遊戲\n2. 討論最新電影\n3. 分享旅遊計畫") -> MagicMock:
    """回傳一個模擬 Groq client（async chat.completions.create）"""
    client = MagicMock()
    choice = MagicMock()
    choice.message.content = content
    response = MagicMock()
    response.choices = [choice]
    client.chat = MagicMock()
    client.chat.completions = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=response)
    return client


def make_member(user_id: str, is_bot: bool = False) -> MagicMock:
    m = MagicMock()
    m.id = user_id
    m.bot = is_bot
    return m


# ── 測試案例 ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_generate_topics_returns_three_topics():
    """generate_topics 回傳 list[str]，長度為 3"""
    from topic_generator import TopicGenerator

    groq = make_groq_client("1. 聊聊最近的遊戲\n2. 討論最新電影\n3. 分享旅遊計畫")
    gen = TopicGenerator(
        vector_store=FakeVectorStore({"u1": "愛打遊戲的人"}),
        transcript_store=FakeTranscriptStore([{"speaker": "u1", "text": "我昨天打遊戲", "timestamp": 1.0}]),
        groq_client=groq,
    )
    members = [make_member("u1")]
    result = await gen.generate_topics("guild1", members)

    assert isinstance(result, list)
    assert len(result) == 3


@pytest.mark.asyncio
async def test_generate_topics_profile_passed_to_llm():
    """profiles 非空時，prompt 包含 profile 內容（驗證 profile 有被傳入 LLM）"""
    from topic_generator import TopicGenerator

    profile_text = "這個用戶熱愛登山，常提到百岳挑戰"
    groq = make_groq_client("1. 話題A\n2. 話題B\n3. 話題C")
    gen = TopicGenerator(
        vector_store=FakeVectorStore({"u1": profile_text}),
        transcript_store=FakeTranscriptStore([]),
        groq_client=groq,
    )
    members = [make_member("u1")]
    await gen.generate_topics("guild1", members)

    # 檢查 LLM 呼叫的 messages 中包含 profile 文字
    call_args = groq.chat.completions.create.call_args
    messages = call_args.kwargs.get("messages") or call_args.args[0] if call_args.args else call_args.kwargs["messages"]
    full_prompt = " ".join(m["content"] for m in messages)
    assert profile_text in full_prompt


@pytest.mark.asyncio
async def test_generate_topics_no_profile_uses_transcript_only():
    """所有 member 無 profile → fallback 只用 transcript，不 crash，仍回傳 3 個話題"""
    from topic_generator import TopicGenerator

    groq = make_groq_client("1. 話題A\n2. 話題B\n3. 話題C")
    gen = TopicGenerator(
        vector_store=FakeVectorStore({}),  # 空 profiles
        transcript_store=FakeTranscriptStore([
            {"speaker": "u1", "text": "今天天氣很好", "timestamp": 1.0},
        ]),
        groq_client=groq,
    )
    members = [make_member("u1")]
    result = await gen.generate_topics("guild1", members)

    assert isinstance(result, list)
    assert len(result) == 3


@pytest.mark.asyncio
async def test_generate_topics_empty_voice_members_returns_fallback():
    """voice_members 為空 → 回傳 fallback 訊息，不 crash"""
    from topic_generator import TopicGenerator

    groq = make_groq_client()
    gen = TopicGenerator(
        vector_store=FakeVectorStore({}),
        transcript_store=FakeTranscriptStore([]),
        groq_client=groq,
    )
    result = await gen.generate_topics("guild1", [])

    assert isinstance(result, list)
    assert len(result) >= 1
    # 沒人在語音，應有提示性的 fallback
    assert any("沒有" in t or "沒人" in t or "空" in t or "語音" in t for t in result)


@pytest.mark.asyncio
async def test_generate_topics_groq_timeout_returns_fallback():
    """Groq 呼叫 timeout → 回傳 fallback ['我想不到好話題，等一下再試']"""
    import asyncio
    from topic_generator import TopicGenerator

    groq = MagicMock()
    groq.chat = MagicMock()
    groq.chat.completions = MagicMock()
    groq.chat.completions.create = AsyncMock(side_effect=asyncio.TimeoutError())

    gen = TopicGenerator(
        vector_store=FakeVectorStore({"u1": "某人的 profile"}),
        transcript_store=FakeTranscriptStore([]),
        groq_client=groq,
    )
    members = [make_member("u1")]
    result = await gen.generate_topics("guild1", members)

    assert result == ["我想不到好話題，等一下再試"]


@pytest.mark.asyncio
async def test_generate_topics_both_empty_returns_generic_fallback():
    """transcript 和 profile 都空 → 回傳通用 fallback（不應 crash 也不應回空 list）"""
    from topic_generator import TopicGenerator

    groq = make_groq_client("1. 聊聊近況\n2. 討論天氣\n3. 分享今日心情")
    gen = TopicGenerator(
        vector_store=FakeVectorStore({}),
        transcript_store=FakeTranscriptStore([]),
        groq_client=groq,
    )
    members = [make_member("u1")]
    result = await gen.generate_topics("guild1", members)

    assert isinstance(result, list)
    assert len(result) >= 1


@pytest.mark.asyncio
async def test_generate_topics_bot_members_excluded():
    """bot 成員應被排除在 speaker_ids 之外（不查詢 bot 的 profile）"""
    from topic_generator import TopicGenerator

    groq = make_groq_client("1. 話題A\n2. 話題B\n3. 話題C")

    checked_ids: list[list[str]] = []

    class TrackingVectorStore:
        def get_profiles_bulk(self, speaker_ids, guild_id):
            checked_ids.append(list(speaker_ids))
            return []

    gen = TopicGenerator(
        vector_store=TrackingVectorStore(),
        transcript_store=FakeTranscriptStore([]),
        groq_client=groq,
    )
    members = [
        make_member("u1", is_bot=False),
        make_member("bot1", is_bot=True),
        make_member("bot2", is_bot=True),
    ]
    await gen.generate_topics("guild1", members)

    # 只有非 bot 的 u1 應該被查詢
    assert checked_ids, "get_profiles_bulk 未被呼叫"
    assert "bot1" not in checked_ids[0]
    assert "bot2" not in checked_ids[0]
    assert "u1" in checked_ids[0]
