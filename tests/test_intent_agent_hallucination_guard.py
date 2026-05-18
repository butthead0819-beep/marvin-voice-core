"""TDD: HallucinationGuardAgent — 主動 bid 高分把幻覺 wake 吞掉。

5/18 夜場 audit 發現 24 個 wake 裡 8 個是「bus no_bids 但 LLM 硬接話」
（user 沒對 bot 講話，bot 用 conv_buffer 編 plausible 答案，噪音回應）。

設計：與其在 controller 加 fall-through-skip 邏輯，不如讓一個 agent
主動出價吞掉幻覺。優點：
- bus 行為不變，IntentBus 還是「加法」
- 零 controller 改動
- 守門員角色 explicit，可獨立 iterate

Bid 規約：
- 0.96 — 明確幻覺（壓過 music 0.95 / nemoclaw 0.95）
- 0.85 — 疑似幻覺（讓 music/nemoclaw 可以 override 如果真有點歌意圖）
- None — 正常 query

Handler: silent swallow (log only)。

刻意 NOT 解：raw 含 "Marvin" 但 query 跟 raw 完全脫節（#7 #13 #15 那類
LLM veto 從 STT 不同 chunk 抓的 wake）— 沒有可靠 sync 訊號可判斷。
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from intent_bus import IntentContext


def _ctx(query, raw_text=None, wake_intent=None, is_owner=False):
    raw = raw_text if raw_text is not None else query
    return IntentContext(
        speaker="Alice", raw_text=raw, query=query,
        original_raw=raw, wake_intent=wake_intent,
        stream_active=False, game_mode=False, is_owner=is_owner, now=100.0,
    )


def _agent():
    """Make a guard agent with controller mock carrying music kw lists
    so _has_play_keyword exemption works."""
    from cogs.voice_controller import VoiceController as _VC
    from intent_agents.hallucination_guard_agent import HallucinationGuardAgent
    ctrl = MagicMock()
    ctrl._STRONG_PLAY_KW = _VC._STRONG_PLAY_KW
    ctrl._WEAK_PLAY_KW = _VC._WEAK_PLAY_KW
    return HallucinationGuardAgent(ctrl)


# ── 該被攔下（高 conf bid） ─────────────────────────────────────────────────

@pytest.mark.parametrize("raw_text,query,wake_intent", [
    # 5/18 #20: Track B wake=1.0 但 raw 完全沒馬文
    ("3F D呀每天都去點", "3F D呀每天都去點", 1.0),
    # 5/18 #23: Track B wake=0.7 但 raw 沒馬文
    ("嫂嫂有成功啊成功率越高", "嫂嫂有成功啊成功率越高", 0.7),
    # 5/18 #10: Track B wake=1.0 但 raw 沒馬文
    ("幹打開小女兒哭", "幹打開小女兒哭", 1.0),
])
def test_track_b_no_wake_word_caught(raw_text, query, wake_intent):
    """Track B 高 wake_intent 但 raw 完全沒「馬文/Marvin」→ LLM veto false positive。"""
    agent = _agent()
    bid = agent.bid(_ctx(query=query, raw_text=raw_text, wake_intent=wake_intent))
    assert bid is not None, f"raw='{raw_text}' wake={wake_intent} 應該被攔"
    assert bid.confidence >= 0.90, f"明確幻覺 conf 應 ≥0.90，實際 {bid.confidence}"


@pytest.mark.parametrize("raw_text,query", [
    # 5/18 #11: STT wake-word loop
    ("Marvin, 馬文章 Marvin, 馬文章 Marvin, 馬文章 Marvin, 馬文章",
     "馬文，馬文，馬文，馬文"),
    # 5/18 #18: Hey, 馬文, 艾馬文 ×N
    ("Hey, 馬文, 艾馬文, Hi Marvin, 艾馬文, Hi Marvin, 艾馬文, Hi Marvin, Hi Marvin.",
     "馬文，艾馬文，Hi Marvin，艾馬文"),
])
def test_wake_word_repetition_caught(raw_text, query):
    """連續複誦喚醒詞 (≥3 次) — 明確 STT loop hallucination。"""
    agent = _agent()
    bid = agent.bid(_ctx(query=query, raw_text=raw_text))
    assert bid is not None, f"wake repetition '{raw_text[:30]}' 應該被攔"
    assert bid.confidence >= 0.90


@pytest.mark.parametrize("raw_text,query", [
    # 5/18 #21: Hi Marvin × N + 韓文 + 越南文 + 雜訊
    ("Hi Marvin, Hi Marvin, Hi passa My Marvin, Hi Marvin 있거든요 Maintenant Hi Roma Hi opposition Hiрみ hig hig ing",
     "Hi Marvin, Hi Marvin, Hi passa My Marvi"),
])
def test_multi_script_gibberish_caught(raw_text, query):
    """3+ 種文字系統混雜（拉丁 + CJK + 韓 + 西里爾...）→ 幻覺。"""
    agent = _agent()
    bid = agent.bid(_ctx(query=query, raw_text=raw_text))
    assert bid is not None, f"multi-script gibberish 應該被攔"
    assert bid.confidence >= 0.80


# ── 該放行（不出價，讓 LLM/music 接） ────────────────────────────────────

@pytest.mark.parametrize("raw_text,query,wake_intent", [
    # 5/18 #3: 正常點歌
    ("馬文播放陶喆的普通朋友", "播放陶喆的普通朋友", None),
    # 5/18 #4: weak_play 但歌名 OK
    ("麻煩播放幹大事", "麻煩播放幹大事", 1.0),
    # 5/18 #14: 中文獨白（user 接受的 case）
    ("人格壽格壽司杯壽司北宋格壽司北宋靠腰還要幫你收好",
     "人格壽格壽司杯壽司北宋格壽司北宋靠腰還要幫你收好", 0.7),
    # 5/18 #19: 中文獨白（user 接受的 case，showay 訂房）
    ("化膿是四個大跟後來然後澳門然後要訂珠海",
     "化膿是四個大跟後來然後澳門然後要訂珠海", 0.7),
])
def test_valid_queries_no_bid(raw_text, query, wake_intent):
    agent = _agent()
    bid = agent.bid(_ctx(query=query, raw_text=raw_text, wake_intent=wake_intent))
    assert bid is None, f"正常 query '{query[:30]}' 不該被擋（實際 bid={bid}）"


@pytest.mark.parametrize("query", [
    "你今天好嗎",
    "馬文你覺得呢",
    "幫我查天氣",
    "播放陶喆的天天",
    "為什麼",
    "Marvin tell me a joke",
])
def test_normal_chat_no_bid(query):
    agent = _agent()
    bid = agent.bid(_ctx(query=query, raw_text=f"馬文，{query}"))
    assert bid is None


# ── Handler: silent swallow ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_handler_is_silent_swallow():
    """guard 勝出時 handler 不該觸發 TTS / LLM / 任何 controller 副作用。"""
    from intent_agents.hallucination_guard_agent import HallucinationGuardAgent
    ctrl = MagicMock()
    agent = HallucinationGuardAgent(ctrl)

    bid = agent.bid(_ctx(query="3F D呀每天都去點", raw_text="3F D呀每天都去點", wake_intent=1.0))
    assert bid is not None
    await bid.handler()

    # ctrl 上的任何 method 都不該被叫
    for name in ("_safe_music_command", "_ask_music_followup",
                 "_handle_nemoclaw_query", "stream_fast_response"):
        attr = getattr(ctrl, name, None)
        if attr is not None and hasattr(attr, "assert_not_called"):
            attr.assert_not_called()


# ── Confidence > music/nemoclaw 0.95（壓過去）─────────────────────────────

def test_guard_outranks_music_strong_play():
    """明確幻覺場景（wake_loop），guard conf 必須 > 0.95（壓過 music/nemoclaw 0.95）。"""
    agent = _agent()
    bid = agent.bid(_ctx(
        query="馬文，馬文，馬文，馬文",
        raw_text="Marvin, Marvin, Marvin, Marvin",
    ))
    assert bid is not None
    assert bid.confidence > 0.95


# ── 邊界：raw 含 Marvin/馬文 但 query 是聊天獨白（#7 #13 #15）──────────

@pytest.mark.parametrize("raw_text,query", [
    ("Marvin, 李宗盛", "魔術那個時候他是最早的多少然後還有三國的版本對啊然後那個"),
    ("Marvin, 李宗盛", "在洗碗的時候哭哭"),
    ("Marvin, 李宗盛", "他很快10分鐘"),
])
def test_marvin_in_raw_but_query_disconnected_no_bid(raw_text, query):
    """raw 含 Marvin 但 query 跟 raw 脫節 — 暫時放行（沒可靠 sync 訊號判斷）。

    這是 P2 待解 case（需要 wake_event_time vs query_time gap 訊號）。
    """
    agent = _agent()
    bid = agent.bid(_ctx(query=query, raw_text=raw_text))
    assert bid is None, "raw 含 Marvin 的 case 暫不攔（已知 limitation）"


# ── 邊界：超短 query ──────────────────────────────────────────────────────

@pytest.mark.parametrize("raw_text,query", [
    ("Marvin, Hi Marvin!", "Hi Marvin!"),
    ("馬文，嗯", "嗯"),
    ("馬文", ""),
])
def test_super_short_or_empty_caught(raw_text, query):
    agent = _agent()
    bid = agent.bid(_ctx(query=query, raw_text=raw_text))
    assert bid is not None, f"超短 query '{query}' 應該攔"
