"""TDD: DJ 串場故事摻「最近生活內容」寫成雞湯，別只串歌名。

2026-07-17 使用者：DJ interjection 的串故事內容，可以摻雜最近的生活內容寫成
雞湯文，這樣比單純串歌名自然一點。

三件事：
1. 新純模組 dj_life_context.recent_life_cores——從日記核心句取近幾日生活素材
   （跨天窗；低顯著度的「無意義對話」不當素材，否則雞湯熬出來是水）。
2. 生活素材接進 _fetch_dj_interjection_raw 的 LLM context。
3. autopilot（Marvin 點的）從純模板升級成走 LLM 雞湯；LLM 失敗才退回模板。
   themed 的策展理由維持原樣（不呼叫 LLM）。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


# ── 1. 純函式：dj_life_context.recent_life_cores ──────────────────────────

NOW = 1_752_700_000.0  # 固定時戳，別用 time.time()（測試要可重現）


def _entry(ts_str: str, core: str, salience: str = "中"):
    """模擬 diary_comic.parser.DiaryEntry 的鴨子型別。"""
    e = MagicMock()
    e.ts_str = ts_str
    e.core = core
    e.salience = salience
    return e


def _ts(now: float, days_ago: float) -> str:
    import datetime as _dt
    return _dt.datetime.fromtimestamp(now - days_ago * 86400.0).strftime("%Y-%m-%d %H:%M:%S")


def test_recent_life_cores_keeps_entries_within_day_window():
    from dj_life_context import recent_life_cores
    entries = [
        _entry(_ts(NOW, 1.0), "大肚在準備搬家"),
        _entry(_ts(NOW, 2.5), "狗與露報名了馬拉松"),
    ]
    cores = recent_life_cores(entries, now=NOW, days=3.0)
    assert "大肚在準備搬家" in cores
    assert "狗與露報名了馬拉松" in cores


def test_recent_life_cores_drops_entries_outside_window():
    from dj_life_context import recent_life_cores
    entries = [
        _entry(_ts(NOW, 10.0), "上個月的舊事"),
        _entry(_ts(NOW, 1.0), "昨天的事"),
    ]
    cores = recent_life_cores(entries, now=NOW, days=3.0)
    assert cores == ["昨天的事"], f"窗外的舊事不該入雞湯: {cores!r}"


def test_recent_life_cores_marks_high_salience():
    """高顯著度＝最獨特/難忘的生活事件 → 標【重點】讓 LLM 優先繞它熬湯。"""
    from dj_life_context import recent_life_cores
    entries = [
        _entry(_ts(NOW, 0.5), "聊了一下天氣", salience="中"),
        _entry(_ts(NOW, 0.4), "大肚要離職去環島", salience="高"),
    ]
    cores = recent_life_cores(entries, now=NOW, days=3.0)
    assert "【重點】大肚要離職去環島" in cores
    assert "聊了一下天氣" in cores


def test_recent_life_cores_drops_low_salience_noise():
    """低顯著度（『無意義對話』那種）不是生活素材，熬出來的雞湯是水。"""
    from dj_life_context import recent_life_cores
    entries = [
        _entry(_ts(NOW, 0.5), "無意義對話。", salience="低"),
        _entry(_ts(NOW, 0.4), "狗與露換了新工作", salience="中"),
    ]
    cores = recent_life_cores(entries, now=NOW, days=3.0)
    assert cores == ["狗與露換了新工作"], f"低顯著度雜訊應被濾掉: {cores!r}"


def test_recent_life_cores_caps_and_keeps_newest():
    from dj_life_context import recent_life_cores
    entries = [_entry(_ts(NOW, 2.0 - i * 0.1), f"事件{i}") for i in range(10)]
    cores = recent_life_cores(entries, now=NOW, days=3.0, max_cores=4)
    assert len(cores) == 4
    assert "事件9" in cores, f"應保留最新的: {cores!r}"
    assert "事件0" not in cores, f"最舊的應被裁掉: {cores!r}"


def test_recent_life_cores_empty_when_nothing_in_window():
    from dj_life_context import recent_life_cores
    assert recent_life_cores([], now=NOW, days=3.0) == []


def test_recent_life_cores_survives_bad_timestamps():
    """壞時戳不該炸掉整條 DJ 路徑（優雅降級）。"""
    from dj_life_context import recent_life_cores
    entries = [_entry("not-a-timestamp", "壞的"), _entry(_ts(NOW, 1.0), "好的")]
    assert recent_life_cores(entries, now=NOW, days=3.0) == ["好的"]


# ── 2. wiring：生活素材進 LLM context ─────────────────────────────────────

def _make_cog(life_cores=None):
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.tts_engine = MagicMock()
    bot.tts_engine.generate_audio = AsyncMock(return_value="/tmp/dj_audio.opus")
    bot.tts_engine.get_estimated_duration = MagicMock(return_value=3.0)
    bot.router = MagicMock()
    bot.router.generate_dynamic_system_msg = AsyncMock(
        return_value="搬家那種空蕩蕩的感覺，配這首剛剛好"
    )
    bot.engine = MagicMock()
    bot.engine.conv_buffer = MagicMock()
    bot.engine.conv_buffer.get_last_n_utterances = MagicMock(return_value=[])
    bot.engine.post_summon_callback = None
    bot.music_memory = MagicMock()
    bot.music_memory._key = MagicMock(return_value="song_key_xyz")
    bot.music_memory._data = {"songs": {}}
    bot.music_memory.time_slot = MagicMock(return_value="深夜")

    from cogs.music_cog import MusicCog
    cog = MusicCog(bot)
    cog._life_cores = MagicMock(return_value=list(life_cores or []))
    return cog


def _info(title="周杰倫 - 夜曲", requester="大肚", **kw):
    d = {"title": title, "uploader": "周杰倫", "requested_by": requester,
         "url": "https://example/x"}
    d.update(kw)
    return d


def _ctx_str(cog):
    call = cog.bot.router.generate_dynamic_system_msg.call_args
    assert call is not None, "generate_dynamic_system_msg 應被呼叫"
    return call.kwargs.get("context", "") or (call.args[1] if len(call.args) > 1 else "")


@pytest.mark.asyncio
async def test_human_context_includes_recent_life():
    """真人點歌 → context 帶最近生活素材，讓 DJ 有得熬湯。"""
    cog = _make_cog(life_cores=["大肚在準備搬家", "【重點】狗與露要去環島"])
    await cog._fetch_dj_interjection_raw(_info(requester="大肚"))
    ctx = _ctx_str(cog)
    assert "大肚在準備搬家" in ctx, f"context 應含生活素材: {ctx!r}"
    assert "狗與露要去環島" in ctx


@pytest.mark.asyncio
async def test_context_has_no_life_line_when_no_material():
    """沒有生活素材 → 不硬塞空行（別讓 LLM 對著空標題編故事）。"""
    cog = _make_cog(life_cores=[])
    await cog._fetch_dj_interjection_raw(_info())
    ctx = _ctx_str(cog)
    assert "最近生活" not in ctx, f"無素材時不該有生活行: {ctx!r}"


# ── 3. autopilot 走 LLM 雞湯（不再是純模板）───────────────────────────────

@pytest.mark.asyncio
async def test_autopilot_uses_llm_with_life_context():
    """Marvin autopilot 點的歌也走 LLM 雞湯，且吃得到生活素材。"""
    cog = _make_cog(life_cores=["大肚在準備搬家"])
    dj = await cog._fetch_dj_interjection_raw(
        _info(requester="Marvin", _lane="liked", _spotlight="大肚")
    )
    cog.bot.router.generate_dynamic_system_msg.assert_awaited()
    assert dj["text"] == "搬家那種空蕩蕩的感覺，配這首剛剛好"
    assert "大肚在準備搬家" in _ctx_str(cog)


@pytest.mark.asyncio
async def test_autopilot_falls_back_to_template_when_llm_fails():
    """LLM 炸了 → 退回原本的 autopilot 模板，不是 fallback 報幕句。"""
    cog = _make_cog()
    cog.bot.router.generate_dynamic_system_msg = AsyncMock(side_effect=RuntimeError("boom"))
    dj = await cog._fetch_dj_interjection_raw(
        _info(title="周杰倫 - 夜曲", requester="Marvin", _lane="liked", _spotlight="大肚")
    )
    assert dj is not None and len(dj["text"]) >= 2
    assert "為你帶來" not in dj["text"], f"應走 autopilot 模板而非報幕 fallback: {dj['text']!r}"


@pytest.mark.asyncio
async def test_autopilot_falls_back_to_template_when_llm_returns_empty():
    cog = _make_cog()
    cog.bot.router.generate_dynamic_system_msg = AsyncMock(return_value="")
    dj = await cog._fetch_dj_interjection_raw(
        _info(requester="Marvin", _lane="liked", _spotlight="大肚")
    )
    assert dj is not None and len(dj["text"]) >= 2
    assert "為你帶來" not in dj["text"]


@pytest.mark.asyncio
async def test_themed_reason_still_skips_llm():
    """themed 策展理由是策展時 LLM 寫好的 → 這條不重複呼叫 LLM。"""
    cog = _make_cog()
    dj = await cog._fetch_dj_interjection_raw(
        _info(requester="Marvin", _lane="themed", _pick_reason="這首扣回你們今晚聊的搬家")
    )
    cog.bot.router.generate_dynamic_system_msg.assert_not_awaited()
    assert dj["text"] == "這首扣回你們今晚聊的搬家"


# ── 4. 掛名護欄：autopilot 改 LLM 後掛名不再是寫死的 ──────────────────────

@pytest.mark.asyncio
async def test_autopilot_context_carries_spotlight_for_attribution():
    """掛名鐵則：LLM 只能照脈絡掛名 → spotlight 必須進 context，它才掛得對。"""
    cog = _make_cog()
    await cog._fetch_dj_interjection_raw(
        _info(requester="Marvin", _lane="long_tail", _spotlight="狗與露")
    )
    assert "狗與露" in _ctx_str(cog)


def _dj_prompt_block() -> str:
    from pathlib import Path
    src = Path("gemini_router_content.py").read_text(encoding="utf-8")
    return src.split('"dj_interjection": (')[1].split(")\n")[0]


def test_dj_prompt_word_budget_targets_10_seconds():
    """2026-07-17 使用者：雞湯文改成 10 秒（減 1 秒沒差異）。

    真實 edge-tts ≈5.7 中文字/秒 → 10s ≈ 57-60 字。live 實測 LLM 會嚴重超寫
    （24 則有 9 則爆 gate 被截斷），所以字數規則要擺在最前面、講死上限。
    """
    blk = _dj_prompt_block()
    assert "50-60" in blk, "prompt 字數預算應為 50-60 中文字（真實≈10s）"
    assert "60-90" not in blk and "54-84" not in blk, "舊字數預算應已移除"


def test_dj_prompt_puts_length_rule_first():
    """live 實測 37.5% 超長被截斷：長度規則埋在第 6 條沒用，要擺第 1 條。"""
    blk = _dj_prompt_block()
    first_rule = blk.split("1. ")[1].split("2. ")[0]
    assert "50-60" in first_rule, f"字數規則應是第 1 條: {first_rule[:60]!r}"


def test_dj_prompt_forbids_human_first_person():
    """Marvin 不是人類：雞湯不得用第一人稱人類經驗（「我也搬過家」「我懂那種感覺」）。

    雞湯這個文體天生誘導「我也曾經⋯」，prompt 必須明文擋掉，改觀察者視角。
    """
    blk = _dj_prompt_block()
    assert "機器" in blk or "不是人類" in blk, "DJ prompt 應宣告 Marvin 非人類"
    assert "第一人稱" in blk, "DJ prompt 應明文禁止第一人稱人類經驗"


def test_dj_prompt_has_no_human_body_framing():
    """別再叫 LLM 扮『一邊喝飲料的朋友』——那是人類身體經驗的框架。"""
    blk = _dj_prompt_block()
    for bad in ("喝飲料", "坐在聽眾旁邊"):
        assert bad not in blk, f"prompt 殘留人類身體框架: {bad}"


def test_dj_prompt_forbids_inventing_attribution():
    """prompt 必須明文禁止 LLM 自己指定這首是誰點的（掛錯名比不掛名傷）。"""
    from pathlib import Path
    src = Path("gemini_router_content.py").read_text(encoding="utf-8")
    dj_block = src.split('"dj_interjection": (')[1].split(")\n")[0]
    assert "掛名" in dj_block and "脈絡" in dj_block, "DJ prompt 應有掛名護欄"


# ── 5. 日記讀檔不阻塞 event loop ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_life_cores_reads_diary_off_event_loop():
    """606K 日記檔的 read+parse 必須在 to_thread 內（Sink/pipeline async 安全規範）。"""
    import inspect

    from cogs.music_cog import MusicCog
    src = inspect.getsource(MusicCog._life_cores_async)
    assert "to_thread" in src, "日記讀檔必須走 asyncio.to_thread，不得阻塞 event loop"
