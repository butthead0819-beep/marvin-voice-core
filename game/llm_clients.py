"""Shared 3-layer LLM client builders + constants.

Used by:
  - game/busted_llm_engine.py        (JSON judge mode)
  - game/busted99/llm_engine.py      (JSON judge mode)
  - game/marvin_player.py            (plain text generation mode)

Each module keeps its own response-parsing logic (the validators differ),
but the client construction + model selection is now in one place. Changing
an API key env var name, swapping a base URL, or adding a new provider only
needs to be done here.
"""
from __future__ import annotations

import os
from typing import Any

# ── Provider config (env-overridable) ─────────────────────────────────────
CEREBRAS_BASE_URL = "https://api.cerebras.ai/v1"
GROQ_BASE_URL = "https://api.groq.com/openai/v1"

CEREBRAS_MODEL = os.environ.get("CEREBRAS_MODEL", "qwen-3-235b-a22b-instruct-2507")
GROQ_MODEL = os.environ.get("GROQ_FALLBACK_MODEL", "llama-3.3-70b-versatile")
GROQ_WEAK_MODEL = os.environ.get("GROQ_SIMPLE_MODEL", "openai/gpt-oss-20b")
GEMINI_MODEL = os.environ.get("GEMINI_FALLBACK_MODEL", "gemini-2.5-flash")

# ── Process-level singletons (lazy) ───────────────────────────────────────
_cerebras: Any = None
_groq: Any = None
_gemini: Any = None


def get_cerebras_client() -> Any:
    """Return cached AsyncOpenAI client for Cerebras, or None if no key/lib."""
    global _cerebras
    if _cerebras is not None:
        return _cerebras
    key = (os.environ.get("CEREBRAS_API_KEY") or "").strip()
    if not key:
        return None
    try:
        from openai import AsyncOpenAI
    except ImportError:
        return None
    _cerebras = AsyncOpenAI(api_key=key, base_url=CEREBRAS_BASE_URL)
    return _cerebras


def get_groq_client() -> Any:
    """Return cached AsyncOpenAI client for Groq, or None if no key/lib."""
    global _groq
    if _groq is not None:
        return _groq
    key = (os.environ.get("GROQ_API_KEY") or "").strip()
    if not key:
        return None
    try:
        from openai import AsyncOpenAI
    except ImportError:
        return None
    _groq = AsyncOpenAI(api_key=key, base_url=GROQ_BASE_URL)
    return _groq


def get_gemini_client() -> Any:
    """Return cached genai client for Gemini, or None if no key/lib."""
    global _gemini
    if _gemini is not None:
        return _gemini
    key = os.environ.get("GOOGLE_API_KEY", "").strip()
    if not key:
        return None
    try:
        from google import genai
    except ImportError:
        return None
    _gemini = genai.Client(api_key=key)
    return _gemini


def reset_clients() -> None:
    """Test hook — drop cached clients so the next get_*() re-reads env."""
    global _cerebras, _groq, _gemini
    _cerebras = _groq = _gemini = None
