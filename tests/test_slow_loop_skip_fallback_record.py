"""TDD — LLM 判 SKIP 時，DJ interjection 的生活素材來源不該跟著全丟。

設計不變：SKIP 的內容依舊不貼進 Discord 日記頻道。
但只要這輪確實捕捉到人類發言，就該留一筆可被 diary_comic.parser 解析出
「核心：」的 fallback 記錄（給 dj_life_context 當雞湯素材用），
而不是寫死的 "[SKIPPED - 內容無新意]"（parser 會整段丟棄，DJ 完全吃不到）。
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_fake(*, summary_return=None):
    """造一個能走到『累積字數過 200 門檻 → 呼叫 LLM』分支的 fake self。"""
    fake = MagicMock()
    fake.bot.engine.conv_buffer = MagicMock()
    fake.last_player_speech_time = time.time()
    now = time.time()
    entries = [
        {"speaker": "大肚", "text": "今天公司在吵要不要導入新的排班系統，吵了快一小時，大家意見都不一樣，主管也拿不定主意，會議一直拖延，最後決定下週再開一次會討論", "timestamp": now},
        {"speaker": "狗與露", "text": "對啊而且那個系統介面超難用，大家都在抱怨，說根本沒人會用，教學文件也寫得亂七八糟看不懂，客服也不太理人", "timestamp": now},
        {"speaker": "Alice", "text": "我們部門已經在用類似的東西了，其實還好，習慣就好用了，一開始也是覺得卡卡的後來就順了，可能是每個系統都要適應期", "timestamp": now},
        {"speaker": "大肚", "text": "希望下週開會能有個結論，不然這樣拖下去大家都很煩躁，工作效率也受影響", "timestamp": now},
    ]
    fake.bot.engine.conv_buffer.pop_new_entries.return_value = entries
    fake.slow_loop_accumulator = []
    fake.bot.router.current_game = None
    fake._stt_call_counter = 0
    fake.get_online_members.return_value = ["大肚", "狗與露", "Alice"]
    fake.bot.router.generate_slow_summary = AsyncMock(return_value=summary_return)
    fake.pending_intervention = None
    fake.active_text_channel = MagicMock()
    fake.active_text_channel.guild = None
    return fake


@pytest.mark.asyncio
async def test_skip_still_writes_fallback_core_for_dj_material(tmp_path, monkeypatch):
    """LLM 回 None（SKIP）+ 有捕捉到人類發言 → 落地的記錄要有『核心：』可讓 parser 解析。"""
    from cogs.voice_controller import VoiceController
    from diary_comic.parser import parse_log

    monkeypatch.chdir(tmp_path)
    fake = _make_fake(summary_return=None)

    await VoiceController.slow_system_loop.coro(fake)

    log_path = tmp_path / "records" / "chat_summary_log.txt"
    assert log_path.exists(), "SKIP 時也該落地一筆記錄"
    text = log_path.read_text(encoding="utf-8")
    assert "[SKIPPED" not in text, "不該再寫死無法被 parser 解析的 SKIPPED 佔位字串"

    entries = parse_log(text)
    assert len(entries) == 1, f"fallback 記錄要能被 parser 解析出一則 DiaryEntry: {text!r}"
    assert entries[0].core, "核心內容不可為空，DJ 需要這句話當雞湯素材"


@pytest.mark.asyncio
async def test_skip_fallback_does_not_post_to_discord(tmp_path, monkeypatch):
    """設計不變：即使留了 fallback 記錄，SKIP 的內容依舊不貼進 Discord 頻道。"""
    from cogs.voice_controller import VoiceController

    monkeypatch.chdir(tmp_path)
    fake = _make_fake(summary_return=None)

    await VoiceController.slow_system_loop.coro(fake)

    fake.active_text_channel.send.assert_not_called()


@pytest.mark.asyncio
async def test_no_human_speech_still_skips_without_fallback(tmp_path, monkeypatch):
    """反例：這輪完全沒捕捉到人類發言（只有馬文自言自語）→ 維持原樣不留記錄。"""
    from cogs.voice_controller import VoiceController

    monkeypatch.chdir(tmp_path)
    fake = _make_fake(summary_return=None)
    fake.bot.engine.conv_buffer.pop_new_entries.return_value = [
        {"speaker": "Marvin", "text": "喔天啊真的假的" * 10},
    ]

    await VoiceController.slow_system_loop.coro(fake)

    log_path = tmp_path / "records" / "chat_summary_log.txt"
    assert not log_path.exists()
    fake.bot.router.generate_slow_summary.assert_not_called()
