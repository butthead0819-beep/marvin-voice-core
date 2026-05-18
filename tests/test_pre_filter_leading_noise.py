"""
TDD：P1 — pre_filter_speech 必須在打 _FAST_RE 前 strip STT 常見前綴雜訊。

5/16 log 大量真實喚醒被 demote 到 llm_verify：
- 「你好, 馬文, 艾馬文, Hi Marvin」
- 「On, 馬文, 艾馬文」
- 「Yeah, 馬文, 艾馬文, Hi Marvin」
- 「M. 馬文」
- 「ai, 馬文」
- 「嗯馬文」（無分隔符）

意圖層面：使用者明確在叫 Marvin，只是 STT 把 filler 或招呼語黏在前面。
不該因為「字面上 ^ 不是馬文」就降級到 LLM verify path。

修法：pre_filter 在 _FAST_RE 前先剝離已知前綴 noise：
- 中文 filler chars: 嗯啊哦喔呃唉嘿哼欸誒嗨
- 中文招呼: 你好、哈囉、哈嘍、喂
- 英文 filler: hi/hey/hello/on/ai/yeah/yo/ok/um/er/ah/oh/no
- STT 噪音字 + 句點: M. / N.
- 上述後接的標點 / 空白
"""
from __future__ import annotations

import pytest

from wake_detector import pre_filter_speech


# ── 中文 filler 前綴 → 應該 promote 到 fast_intervene ──────────────────────

@pytest.mark.parametrize("text", [
    "嗯馬文",                          # 無分隔
    "嗯，馬文你能不能",                # 有逗號分隔
    "啊馬文",
    "哦馬文，幫我查",
    "嗯嗯馬文",                        # 多個 filler
    "嗯，嗯，馬文",                    # 多個 filler + 分隔
])
def test_leading_chinese_filler_promotes_to_fast(text):
    result = pre_filter_speech(text)
    assert result["action"] == "fast_intervene", \
        f"'{text}' 應該 fast_intervene，實際={result['action']}"


# ── 中文招呼語前綴 ────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "你好, 馬文, 艾馬文",              # 5/16 真實 log
    "哈囉馬文",
    "喂 馬文你能聽到嗎",
])
def test_leading_chinese_greeting_promotes_to_fast(text):
    result = pre_filter_speech(text)
    assert result["action"] == "fast_intervene"


# ── 英文 filler / STT 噪音前綴 ───────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "On, 馬文, 艾馬文",                # 5/16 真實 log
    "Yeah, 馬文, 艾馬文, Hi Marvin",    # 5/16 真實 log
    "Ai, 馬文",                        # 5/16 真實 log
    "M. 馬文",                         # 5/16 真實 log (M dot)
    "hi 馬文",
    "hey 馬文幫我",
    "OK 馬文",
    "ok, 馬文",
    "Um 馬文",
])
def test_leading_english_filler_promotes_to_fast(text):
    result = pre_filter_speech(text)
    assert result["action"] == "fast_intervene", \
        f"'{text}' 應該 fast_intervene，實際={result['action']}"


# ── 既有 baseline 不變 ───────────────────────────────────────────────────

def test_marvin_alone_still_fast():
    """馬文單獨開頭：既有行為。"""
    assert pre_filter_speech("馬文")["action"] == "fast_intervene"
    assert pre_filter_speech("馬文，幫我查")["action"] == "fast_intervene"


def test_marvin_mid_sentence_remains_llm_verify():
    """中間出現的馬文：仍走 llm_verify（不該因 strip 邏輯被誤升）。"""
    result = pre_filter_speech("我剛才叫了馬文一聲")
    assert result["action"] == "llm_verify"


def test_unrelated_text_drops():
    """無喚醒詞、無 context trigger：drop。"""
    assert pre_filter_speech("今天天氣很好")["action"] == "drop"


def test_context_trigger_repeat_still_processes():
    """重複 context trigger 仍走 process（既有行為）。"""
    result = pre_filter_speech("完了完了")
    assert result["action"] == "process"


def test_empty_drops():
    assert pre_filter_speech("")["action"] == "drop"
    assert pre_filter_speech("   ")["action"] == "drop"


# ── strip 邏輯邊界：不該誤吃喚醒詞 ───────────────────────────────────────

def test_strip_does_not_eat_marvin_prefix():
    """喚醒詞本身不該被 strip。"M開頭句子' 不該因 strip 把 Marvin 的 M 吃掉。"""
    # Marvin 是完整 wake word，整個都該保留
    result = pre_filter_speech("Marvin, hello")
    assert result["action"] == "fast_intervene"


def test_strip_preserves_raw_text_in_payload():
    """fast_intervene 回傳的 text 應是 raw_text 原文（含 noise），不是 stripped 版本。"""
    raw = "嗯，馬文，幫我查"
    result = pre_filter_speech(raw)
    assert result["text"] == raw, \
        "payload['text'] 應該是原文，下游自己處理喚醒詞剝離"
