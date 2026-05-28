"""rephrase_proactive_script 輸出 metadata strip — 修 5/27 6 筆「【改寫腳本】」
prefix / 「(留意：...)」suffix 漏網 bug。

實際 5/27 production 樣本：
- '【改寫腳本】\n\n@提及，你說...'
- '【改寫腳本】\n\n嗚嗚，聽說你...'
- '【改寫腳本】：\n\n狗友，你昨天...'
- '【改寫腳本】：昨天你提到...'
- '(留意：語氣維持憂鬱、疲憊並看透世事的基調。)'
- '(注意：改寫腳本以更適合馬文的語氣...)'
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from gemini_router_content import _strip_rephraser_metadata


def test_strip_prefix_no_colon_with_double_newline():
    out = _strip_rephraser_metadata("【改寫腳本】\n\n@提及，你說要在田邊小路遇到人。")
    assert out == "@提及，你說要在田邊小路遇到人。"


def test_strip_prefix_with_colon_no_newline():
    out = _strip_rephraser_metadata("【改寫腳本】：昨天你提到在田邊小路被擠。")
    assert out == "昨天你提到在田邊小路被擠。"


def test_strip_prefix_with_colon_and_newlines():
    out = _strip_rephraser_metadata("【改寫腳本】：\n\n狗友，你昨天都在忙改版啊。")
    assert out == "狗友，你昨天都在忙改版啊。"


def test_strip_trailing_meta_留意():
    body = "人類就是這樣。\n\n(留意：語氣維持憂鬱、疲憊並看透世事的基調。)"
    assert _strip_rephraser_metadata(body) == "人類就是這樣。"


def test_strip_trailing_meta_注意():
    body = "撞在一起，結束得如此無聊。\n\n(注意：改寫腳本以更適合馬文的語氣，表現出他那種憂鬱。)"
    assert _strip_rephraser_metadata(body) == "撞在一起，結束得如此無聊。"


def test_strip_trailing_meta_full_width_paren():
    body = "宇宙的本質。（注意：保持簡潔）"
    assert _strip_rephraser_metadata(body) == "宇宙的本質。"


def test_strip_combined_prefix_and_suffix():
    body = (
        "【改寫腳本】\n\n"
        "嗚嗚，聽說你昨天忙著大改版。\n\n"
        "(留意：語氣維持憂鬱、疲憊。)"
    )
    assert _strip_rephraser_metadata(body) == "嗚嗚，聽說你昨天忙著大改版。"


def test_pass_through_clean_response():
    body = "人類的本性就是這樣，給了你們安全感就忘了怎麼共存。"
    assert _strip_rephraser_metadata(body) == body


def test_preserves_legitimate_inline_parens():
    """合法 inline 括號（非 metadata）不該被誤刪。"""
    body = "宇宙（無論我們做什麼）都漏光了。"
    assert _strip_rephraser_metadata(body) == body


def test_preserves_legitimate_trailing_paren_short():
    """末尾括號內容不是 (注意/留意：...) 不該動。"""
    body = "都是這樣的（嘆氣）。"
    assert _strip_rephraser_metadata(body) == body


def test_empty_string():
    assert _strip_rephraser_metadata("") == ""


def test_only_metadata():
    """整段都是 metadata → strip 後變空（caller 該 fallback raw_script）。"""
    out = _strip_rephraser_metadata("【改寫腳本】：\n\n(注意：保持馬文語氣。)")
    assert out == ""
