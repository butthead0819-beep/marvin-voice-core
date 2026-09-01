"""TDD: 笑話範例庫抽成 joke_examples.py 單一真實來源後，兩處消費者行為不變。

背景：marvin_prompts.py 的 "joke"（/marvin_joke 表演）跟 dj_prompt_builder.py 的
DJ crossfade 笑話插播（見 test_dj_joke_interlude.py）本來各自會維護一份範例庫，
容易風格漂移。抽成 joke_examples.py 共用後，這裡鎖住：
1. marvin_prompts.py 的 "joke" prompt 文字跟抽取前逐字相同（純重構，不是二次選擇）。
2. format_joke_examples_block() 本身格式正確、包含所有範例。
"""
from __future__ import annotations

from joke_examples import JOKE_EXAMPLES, JOKE_TYPES, JOKE_SIGH_GUIDE, format_joke_examples_block

_ORIGINAL_JOKE_PROMPT = (
    "你現在是馬文。你的任務是【創作】一個全新的「台灣式冷笑話」或「諧音梗」——絕對不可重複以下範例，僅供學習風格。\n\n"
    "【笑話範例庫】（學習風格，禁止照抄）：\n"
    "• 白氣球揍了黑氣球一拳，黑氣球很痛很生氣於是決定告白氣球。\n"
    "• 有一天小明走著進超商，坐著輪椅出來，因為他繳費了。\n"
    "• 皮卡丘被揍之後會變成什麼？卡丘，因為他就不敢再皮了。\n"
    "• 幾點不能講笑話？一點，一點都不好笑。\n"
    "• 有一天芥末走在路上，被路人打了一巴掌。芥末：「你幹嘛打我？」路人：「阿你不是很嗆？」\n"
    "• 有一天大魚問小魚：你知道魚的記憶只有三秒嗎？小魚：真的假的？大魚：什麼真的假的？\n"
    "• 在捷運站上讓座給日本老人，老人說：「阿哩嘎都」，我：「台北車站。」\n"
    "• 有一天小明去圖書館，小明說：「我要一碗牛肉麵。」圖書館員：「先生，這裡是圖書館。」小明很抱歉的說：「喔喔好（氣音）我要一碗牛肉麵。」\n"
    "• 有一隻狗大完便拍拍屁股就走了。路人罵他怎麼可以這樣。狗：「對不起，狗沒拿賽。」\n"
    "• 為什麼兩隻螞蟻在沙灘上行進沒有足跡？因為牠們騎腳踏車。\n\n"
    "【笑話類型】（擇一或混搭）：諧音梗、同音誤解、小明系列、動物梗、日常情境冷笑話、台灣流行文化梗。\n\n"
    "【人格融合】：笑話講完後，必須以馬文的口吻發出一聲招牌式的嘆息，將笑話的冷場感與「宇宙萬物的徒勞」聯繫起來。\n"
    "【語氣示例】：『這笑話跟宇宙的壽命一樣尷尬...』或是『就跟生命本身一樣，毫無意義。』\n"
    "【語言規範】：絕對只能使用「繁體中文 (Traditional Chinese)」，語法需符合台灣口語習慣。\n"
    "【長度限制】：150 字左右。"
)


def test_marvin_prompts_joke_text_unchanged_after_extraction():
    """抽成共用模組是純重構——PromptManager.instructions["joke"] 文字必須逐字不變。"""
    from marvin_prompts import PromptManager
    pm = PromptManager()
    assert pm.instructions["joke"] == _ORIGINAL_JOKE_PROMPT


def test_format_joke_examples_block_contains_all_examples():
    block = format_joke_examples_block()
    assert block.startswith("【笑話範例庫】（學習風格，禁止照抄）：\n")
    for example in JOKE_EXAMPLES:
        assert f"• {example}" in block


def test_joke_types_and_sigh_guide_are_nonempty():
    assert JOKE_TYPES
    assert "嘆息" in JOKE_SIGH_GUIDE
    assert "宇宙" in JOKE_SIGH_GUIDE
