"""
macOS say 男聲選用 — 靠「實際安裝清單」挑聲，不靠 returncode。

背景（2026-06-06 實測）：
  - 程式原本寫死 Liao（中文）/ Alex（英文），這台機器兩個都沒裝。
  - say 對未知聲音會 silent fallback 到系統預設並回 exit 0（甚至產出靜音
    wav），所以舊的「returncode != 0 才降級」邏輯永遠不會觸發。
  - 唯一可靠的中文男聲是 Han（瀚）；英文是 Fred。

Rules:
  1. 中文偏好順序 Han → Meijia，挑第一個「實際裝了」的。
  2. 英文偏好順序 Fred → Daniel，挑第一個「實際裝了」的。
  3. 偏好聲音都沒裝 → 不帶 -v，讓 say 用系統預設（不靠 returncode 判斷）。
  4. say 聲音清單解析：取每行第一個 token 當聲音名（含 "Han (Premium)" → "Han"）。
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from tts_engine import SukiTTS


# 模擬 `say -v '?'` 的真實輸出片段（這台機器實際裝的）
_SAY_LIST_OUTPUT = b"""Daniel              en_GB    # Hello! My name is Daniel.
Fred                en_US    # Hello! My name is Fred.
Grandpa (zh_TW)     zh_TW    # \xe4\xbd\xa0\xe5\xa5\xbd
Han (Premium)       zh_CN    # \xe4\xbd\xa0\xe5\xa5\xbd
Meijia              zh_TW    # \xe4\xbd\xa0\xe5\xa5\xbd
"""


def _make_engine():
    with patch("os.makedirs"):
        return SukiTTS()


def _patch_voice_list(output: bytes, returncode: int = 0):
    """patch asyncio.create_subprocess_exec 讓 say -v '?' 回傳指定輸出。"""
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(output, b""))
    return patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc))


@pytest.mark.asyncio
async def test_installed_voices_parses_names_including_premium_suffix():
    eng = _make_engine()
    with _patch_voice_list(_SAY_LIST_OUTPUT):
        voices = await eng._get_installed_say_voices()
    assert {"Daniel", "Fred", "Grandpa", "Han", "Meijia"} <= voices


@pytest.mark.asyncio
async def test_installed_voices_cached_after_first_call():
    eng = _make_engine()
    mock_exec = AsyncMock()
    proc = MagicMock()
    proc.returncode = 0
    proc.communicate = AsyncMock(return_value=(_SAY_LIST_OUTPUT, b""))
    mock_exec.return_value = proc
    with patch("asyncio.create_subprocess_exec", mock_exec):
        await eng._get_installed_say_voices()
        await eng._get_installed_say_voices()
    assert mock_exec.call_count == 1


def test_pick_voice_returns_first_installed():
    eng = _make_engine()
    assert eng._pick_say_voice(("Han", "Meijia"), {"Han", "Meijia"}) == "Han"


def test_pick_voice_falls_through_when_first_missing():
    eng = _make_engine()
    assert eng._pick_say_voice(("Han", "Meijia"), {"Meijia"}) == "Meijia"


def test_pick_voice_returns_none_when_all_missing():
    eng = _make_engine()
    assert eng._pick_say_voice(("Han", "Meijia"), {"Tingting"}) is None


@pytest.mark.asyncio
async def test_chinese_uses_han_when_installed():
    eng = _make_engine()
    eng._installed_voices_cache = {"Han", "Fred"}
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec), \
         patch("os.path.exists", return_value=True):
        ok = await eng._generate_marvin_macos_say("今天天氣真好", "/tmp/x.wav")

    assert ok is True
    assert "-v" in captured["args"]
    assert captured["args"][captured["args"].index("-v") + 1] == "Han"


@pytest.mark.asyncio
async def test_english_uses_fred_when_installed():
    eng = _make_engine()
    eng._installed_voices_cache = {"Han", "Meijia", "Fred"}
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec), \
         patch("os.path.exists", return_value=True):
        ok = await eng._generate_marvin_macos_say("hello marvin", "/tmp/x.wav")

    assert ok is True
    assert captured["args"][captured["args"].index("-v") + 1] == "Fred"


@pytest.mark.asyncio
async def test_no_voice_flag_when_all_preferred_missing():
    """偏好男聲都沒裝 → 不帶 -v，交給系統預設，不能硬塞不存在的聲音。"""
    eng = _make_engine()
    eng._installed_voices_cache = {"Tingting"}  # 只有不在偏好清單的聲音
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec), \
         patch("os.path.exists", return_value=True):
        ok = await eng._generate_marvin_macos_say("今天天氣真好", "/tmp/x.wav")

    assert ok is True
    assert "-v" not in captured["args"]
