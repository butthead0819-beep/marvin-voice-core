"""
TDD：Issue 8 — _NO_PATTERNS 必須 anchor 避免跨脈絡污染。

問題：原本 _NO_PATTERNS 沒 `^...$` anchor，導致：
- 「對啊不是我說的」含「不是」→ 命中 → 被當成 confirmation 的「不」
- 「我不要去買菜」含「不要」→ 命中 → 被當成「不」
- 「算了等等再說」含「算了」→ 命中 → 被當成「不」

_YES_PATTERNS 已有 `^...$` anchor 不受影響；_NO_PATTERNS 對齊處理：
要求整句（strip 後）就是短答覆詞，才視為對 confirmation 的回應。
"""
from __future__ import annotations

import pytest


# ── anchor 後：中間命中的長句不再被誤判 ──────────────────────────────────

@pytest.mark.parametrize("query", [
    "對啊不是我說的",      # 含「不是」但是肯定句
    "我不要去買菜",        # 含「不要」但是動作描述
    "算了等等再說",        # 含「算了」但有後綴
    "他不對勁但我們先試",  # 含「不對」但中間
    "我們不用這個方案",    # 含「不用」但是陳述
    "你不記得了？",        # 含「不記」但是疑問
])
def test_no_response_rejects_mid_sentence(query):
    from recall_handler import is_no_response
    assert is_no_response(query) is False, \
        f"'{query}' 不該被當成 no response（cross-context pollution）"


# ── anchor 後：短答覆仍照常命中 ───────────────────────────────────────────

@pytest.mark.parametrize("query", [
    "不用",
    "不要",
    "算了",
    "不是",
    "不對",
    "不記",
    "取消記",
    "不要記",      # 既有測試 case，必須保留
    "不用了",      # 含 sentence-final 語助詞
    "不用啊",
    "不要吧",
    " 不用 ",      # 前後 whitespace
])
def test_no_response_accepts_short_answer(query):
    from recall_handler import is_no_response
    assert is_no_response(query) is True, \
        f"'{query}' 應該命中 no response"


# ── 跟既有 is_yes_response 行為對齊（regression baseline） ──────────────

def test_yes_response_remains_anchored_baseline():
    """既有 _YES_PATTERNS 已 anchor，這條是 regression baseline。"""
    from recall_handler import is_yes_response
    assert is_yes_response("對") is True
    assert is_yes_response("好，記下去") is True
    assert is_yes_response("我去買菜") is False
    # 中間命中應該不算 yes
    assert is_yes_response("對啊我覺得他說的對") is False
