"""TDD: 釘下「query 是否真的是 skip 指令」的判定規則。

2026-05-26 log review 發現 IBA-T0 + music_agent_v2 control_skip 用單純 substring
匹配，導致閒聊提到「下一首」就誤觸發 skip。實況：

  - 「為什麼你下一首」（問題）→ skip 觸發 ✗
  - 「不喜歡下一首歌」→ skip 觸發 ✗
  - 「應該下一首就是了」→ skip 觸發 ✗
  - 「沒有啊然後下一首就馬上出來了」→ skip 觸發 ✗

True positives（沒爭議）：
  - 「下一首」「換一首」「跳過」「換歌」「不要這首」單獨講 → skip ✓
  - 「下一首下一首」（強調）→ skip ✓
  - 「馬文下一首」「快下一首」（address/intensifier）→ skip ✓

設計：抽 pure predicate `is_short_skip_command(text, keywords) -> bool`，
給 IBA-T0 + music_agent_v2 共用，取代 substring match。
"""
from __future__ import annotations

import pytest

from intent_agents.constants import MUSIC_DIRECT_SKIP_KW
from intent_agents.skip_intent import is_short_skip_command


# ── True positives（必須回 True）─────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "下一首",
    "換一首",
    "跳過",
    "換歌",
    "不要這首",
    "下一首下一首",        # 強調重複
    "馬文下一首",          # 含 address 前綴
    "快下一首",            # intensifier
    "下一首啦",            # 尾助詞
    "下一首吧",
    # 2026-05-26 production miss: 雙語 address「Siri」/「Hey」等不在 allowlist，
    # 但同 kw 連講兩次 = 明確命令 → multi-occurrence 豁免位置檢查
    "Siri下一首下一首",
    "Hey下一首下一首",
    "ok下一首下一首",
])
def test_skip_command_true_positives(text):
    assert is_short_skip_command(text, MUSIC_DIRECT_SKIP_KW) is True, (
        f"應該被認為是 skip 指令: {text!r}"
    )


# ── False positives（過往實際誤觸發的句子）──────────────────────────────────

@pytest.mark.parametrize("text", [
    "為什麼你下一首",                            # 2026-05-26 18:16 狗與露
    "不喜歡下一首歌",                            # 2026-05-26 16:09 狗與露
    "應該下一首就是了",                          # 2026-05-26 00:41 大肚（截斷版）
    "沒有啊然後下一首就馬上出來了",              # 2026-05-26 16:34 showay
    "我應該有點出來了應該下一首就是了",          # 2026-05-26 00:41 大肚原句
    "好文下一首",                                # 2026-05-26 16:20 showay（疑似誤觸）
])
def test_skip_command_false_positives_no_match(text):
    assert is_short_skip_command(text, MUSIC_DIRECT_SKIP_KW) is False, (
        f"閒聊提及不該觸發 skip: {text!r}"
    )


# ── Edge cases ──────────────────────────────────────────────────────────────

def test_empty_or_whitespace_returns_false():
    assert is_short_skip_command("", MUSIC_DIRECT_SKIP_KW) is False
    assert is_short_skip_command("   ", MUSIC_DIRECT_SKIP_KW) is False


def test_no_keyword_match_returns_false():
    assert is_short_skip_command("這首歌不錯", MUSIC_DIRECT_SKIP_KW) is False
    assert is_short_skip_command("我喜歡這個", MUSIC_DIRECT_SKIP_KW) is False


def test_keyword_only_text_returns_true():
    """純關鍵字（沒任何其他字）肯定是指令。"""
    for kw in MUSIC_DIRECT_SKIP_KW:
        assert is_short_skip_command(kw, MUSIC_DIRECT_SKIP_KW) is True, (
            f"純關鍵字 {kw!r} 應該命中"
        )


def test_long_sentences_reject_even_if_keyword_present():
    """超過 expressivity-window 的長句一律拒絕（≥20 字，幾乎不可能是命令）。"""
    long_with_kw = "我剛剛想了一下其實下一首這個選擇也沒有什麼不可以的"
    assert is_short_skip_command(long_with_kw, MUSIC_DIRECT_SKIP_KW) is False


# ── 規則合理性檢查（cross-check 規格）────────────────────────────────────────

def test_starts_with_keyword_is_strongest_signal():
    """關鍵字在句首（容許 address/intensifier 前綴）= skip 指令。"""
    assert is_short_skip_command("下一首", MUSIC_DIRECT_SKIP_KW) is True
    assert is_short_skip_command("快下一首", MUSIC_DIRECT_SKIP_KW) is True
    assert is_short_skip_command("馬文下一首", MUSIC_DIRECT_SKIP_KW) is True


def test_keyword_after_negation_or_question_is_not_command():
    """關鍵字前有否定/疑問 → 不是命令。"""
    assert is_short_skip_command("不要下一首", MUSIC_DIRECT_SKIP_KW) is False
    assert is_short_skip_command("為什麼下一首", MUSIC_DIRECT_SKIP_KW) is False


# ── 2026-06-14 incident：放歌中喊「我要切歌」沒反應 ─────────────────────────
# 根因①「切歌」不在關鍵字表；根因②「我要」不在允許前綴 → 兩者都補才能命中。

@pytest.mark.parametrize("text", [
    "切歌",            # 台灣超常用 skip 講法，原本完全不在關鍵字表
    "我要切歌",        # 6/14 21:48:05 狗與露實況；「我要」是自然命令引導
    "我要換歌",
    "我要跳過",
    "我要下一首",
])
def test_incident_20260614_skip_phrasings_true_positives(text):
    assert is_short_skip_command(text, MUSIC_DIRECT_SKIP_KW) is True, (
        f"應該被認為是 skip 指令: {text!r}"
    )


@pytest.mark.parametrize("text", [
    "我要不要切歌",    # 自我商量（deliberation），關鍵字落在前綴之後 → 不是命令
    "我不要切歌",      # 否定：「我要」不該誤匹配「我不要」開頭
])
def test_incident_20260614_skip_false_positive_guards(text):
    assert is_short_skip_command(text, MUSIC_DIRECT_SKIP_KW) is False, (
        f"非命令不該觸發 skip: {text!r}"
    )
