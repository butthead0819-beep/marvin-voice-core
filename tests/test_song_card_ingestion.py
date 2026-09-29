"""TDD：單次聚合多維度歌曲卡（Multi-Faceted Song Card Ingestion）。

驗證：
1. build_song_card_ingestion_prompt：單次呼叫 Google Search 聚合聽覺幕後、社群熱評、歌詞刺點。
2. parse_song_card_response：解析結構化三段回應，支援完全命中、部分命中與無效降級。
"""
from __future__ import annotations

import pytest

from dj_prompt_builder import (
    build_song_card_ingestion_prompt,
    parse_song_card_response,
)


def test_build_song_card_ingestion_prompt_contains_all_facets():
    label = "陳奕迅 - 紅玫瑰"
    lyrics = "[01:15.20] 夢裡夢到醒不來的夢\n[01:23.00] 得不到的永遠在騷動\n[01:28.10] 被偏愛的都有恃無恐"
    prompt = build_song_card_ingestion_prompt(label, lyrics)

    assert label in prompt
    assert "得不到的永遠在騷動" in prompt
    assert "Google" in prompt
    assert "不准只憑記憶" in prompt
    # 三大維度指示
    assert "【聽覺與幕後】" in prompt
    assert "【社群熱評標籤】" in prompt
    assert "【歌詞刺點】" in prompt
    # 格式規範
    assert "90-110" in prompt
    assert "標籤：" in prompt
    assert "句：" in prompt
    assert "析：" in prompt
    # 時間戳不向 LLM 要：沒餵同步歌詞時它只能猜（9/30 實測回「約 02:00」），說錯不如沒說
    assert "時：" not in prompt
    # 禁八股反轉
    assert "公式化" in prompt or "反轉" in prompt


def test_build_song_card_ingestion_prompt_without_lyrics():
    label = "周杰倫 - 雙截棍"
    prompt = build_song_card_ingestion_prompt(label, lyrics="")

    assert label in prompt
    assert "【聽覺與幕後】" in prompt
    assert "【社群熱評標籤】" in prompt
    assert "【歌詞刺點】" in prompt


def test_parse_song_card_response_complete():
    raw_response = (
        "【聽覺與幕後】：周杰倫這首《雙截棍》，其實最初是寫給張惠妹的，卻因曲風太前衛而遭退稿。細聽鍾興民的金曲獎最佳編曲，搖滾與鋼琴交織，尤其留意含糊唱腔與斷句。戴上耳機感受這股武術音浪衝擊。\n"
        "【社群熱評標籤】：標籤：全台KTV必點但唱不上去的神曲 | 情境：前奏電吉他一下青春就回來了\n"
        "【歌詞刺點】：句：快使用雙截棍 哼哼哈兮 | 時：01:10 | 析：以含糊咬字打破傳統武俠與流行樂的邊界"
    )
    card = parse_song_card_response(raw_response)
    assert card is not None

    # 1. 聽覺導聆
    assert "雙截棍" in card["audiophile_guide"]
    assert "張惠妹" in card["audiophile_guide"]

    # 2. 社群熱評
    assert card["social_lore"] is not None
    assert card["social_lore"]["tag"] == "全台KTV必點但唱不上去的神曲"
    assert "前奏電吉他一下" in card["social_lore"]["context"]

    # 3. 歌詞刺點
    assert card["lyric_hook"] is not None
    assert card["lyric_hook"]["quote"] == "快使用雙截棍 哼哼哈兮"
    assert "timestamp" not in card["lyric_hook"]
    assert "含糊咬字" in card["lyric_hook"]["subtext"]


def test_parse_song_card_response_partial():
    raw_response = (
        "【聽覺與幕後】：這首歌曲以精湛鋼琴演奏展開，注意聲場立體定位與低頻層次。戴上耳機準備進入音樂世界。\n"
        "【社群熱評標籤】：無\n"
        "【歌詞刺點】：無"
    )
    card = parse_song_card_response(raw_response)
    assert card is not None
    assert "精湛鋼琴演奏" in card["audiophile_guide"]
    assert card["social_lore"] is None
    assert card["lyric_hook"] is None


def test_parse_song_card_response_invalid():
    assert parse_song_card_response("無") is None
    assert parse_song_card_response("") is None
    assert parse_song_card_response("這首歌很好聽，大家快來聽。") is None


# 9/30 免費層 gemini-2.5-flash 實測〈葉子〉的原始回應（逐字保留）：
# 欄位分行寫、社群標籤給兩組——舊 parser 丟掉整段歌詞刺點、且把第一組標籤配上第二組情境。
_REAL_YEZI_RESPONSE = '【聽覺與幕後】：\n《葉子》由才女陳曉娟創作詞曲，最初是偶像劇《薔薇之戀》的片尾曲，阿桑獨特的滄桑沙啞嗓音，讓這首歌在2003年一推出便廣受矚目。她的聲線帶著歷盡風霜的感觸，輕易就能牽動聽者的情感神經。細聽阿桑的演唱，她對氣息的掌控極為細膩，許多轉音處的微顫，都將歌詞中孤寂與無奈的情緒層層堆疊。戴上耳機，感受那份直抵心底的孤單。\n\n【社群熱評標籤】：\n標籤：孤單是狂歡，狂歡是孤單 | 情境：深夜獨自一人，或在KTV與朋友喧鬧後，心底卻湧現難以言喻的空虛時。\n標籤：2024還在聽的時代眼淚 | 情境：每隔幾年，總會有人在MV底下留言「2024年還在聽」，緬懷阿桑與這首經典。\n\n【歌詞刺點】：\n句：我一個人吃飯、旅行、到處走走停停，也一個人看書、寫信，自己對話談心，只是心又飄到了哪裡，就連自己看也看不清，我想我不僅僅是失去你。\n時：約 02:00 (主歌第二次出現)\n析：這段歌詞精準描繪了失去後試圖獨立生活，卻發現內心深處的失落感已超越了單純的「失去」，而是連自我都變得模糊不清的深層迷茫。'


def test_parse_real_response_multiline_lyric_fields():
    card = parse_song_card_response(_REAL_YEZI_RESPONSE)
    assert card is not None
    lh = card["lyric_hook"]
    assert lh is not None
    assert lh["quote"].startswith("我一個人吃飯")
    assert lh["quote"].endswith("我想我不僅僅是失去你。")
    assert lh["subtext"].startswith("這段歌詞精準描繪")
    assert "02:00" not in lh["quote"] + lh["subtext"]


def test_parse_real_response_multiple_tags_keeps_first_pair():
    card = parse_song_card_response(_REAL_YEZI_RESPONSE)
    sl = card["social_lore"]
    assert sl["tag"] == "孤單是狂歡，狂歡是孤單"
    assert sl["context"].startswith("深夜獨自一人")
    assert "2024" not in sl["context"]
