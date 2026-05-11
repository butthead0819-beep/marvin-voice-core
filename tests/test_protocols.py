"""Tests that concrete classes satisfy their Protocols and that Protocol checks work."""
import pytest
from protocols import STTService, LLMClient, MemoryStore
from marvin_voice_core.stt_handler import STTHandler
from suki_memory import MemoryManager


# ── STTService ────────────────────────────────────────────────────────────────

def test_stt_handler_is_sttservice():
    assert isinstance(STTHandler(whisper_model=None), STTService)


def test_non_stt_object_is_not_sttservice():
    assert not isinstance(object(), STTService)


# ── MemoryStore ───────────────────────────────────────────────────────────────

def test_memory_manager_is_memorystore(tmp_path):
    db = str(tmp_path / "test.db")
    jpath = str(tmp_path / "mem.json")
    mem = MemoryManager(db_path=db, json_compat_path=jpath)
    assert isinstance(mem, MemoryStore)


def test_non_memory_object_is_not_memorystore():
    assert not isinstance(object(), MemoryStore)


# ── LLMClient — duck typing (no concrete class to import without heavy deps) ──

class _FakeLLM:
    async def complete(self, system, user, *, is_json=False, temperature=None) -> str:
        return ""

    async def stream_text(self, system, user, *, temperature=None):
        return
        yield  # makes it a generator

def test_fake_llm_satisfies_protocol():
    assert isinstance(_FakeLLM(), LLMClient)


def test_missing_method_fails_protocol_check():
    class Incomplete:
        async def complete(self, system, user, **_): return ""
        # missing stream_text

    assert not isinstance(Incomplete(), LLMClient)
