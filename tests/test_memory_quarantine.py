"""TDD: 防線③ 記憶寫入 sanity 檢疫閘 — 擋 LLM 產出直寫持久層的污染。

失效模式（Tier 3 錯死型，錯誤會複利）：suki_memory 曾被 LLM 掰的時戳
污染到 2024（bot 2026-03 才誕生）；LLM 非答案（「未知」「無法確定」）
與超長垃圾值直接 merge 進 player record，之後每次 prompt 注入都放大。

quarantine(extracted, speaker) -> (clean, rejected)：
  - 空值/None 值剔除
  - LLM 非答案值剔除（未知/無法確定/N.A./null/沒有提到…）
  - 超長值剔除（記憶勾點應短；>80 字＝LLM 在倒垃圾）
  - 年份宣稱早於 bot 誕生（2026-03）的互動歷史型欄位剔除（掰時戳）
  - taboos 非字串項剔除
  - 全部剔除原因回傳供 shadow log
"""
from __future__ import annotations

import pytest

from memory_quarantine import quarantine, BOT_ORIGIN_YEAR


def test_clean_data_passes_through():
    data = {"personal_info": {"food": "肉圓", "birthday": "08-19"},
            "likes": ["周杰倫"], "taboos": ["前女友"]}
    clean, rejected = quarantine(data, speaker="阿明")
    assert clean == data
    assert rejected == []


def test_llm_non_answers_stripped():
    data = {"personal_info": {"food": "未知", "job": "無法確定", "car": "N/A",
                              "pet": "沒有提到", "home": "台中"}}
    clean, rejected = quarantine(data, speaker="阿明")
    assert clean["personal_info"] == {"home": "台中"}
    assert len(rejected) == 4


def test_empty_and_none_values_stripped():
    data = {"personal_info": {"a": "", "b": None, "c": "真的"}}
    clean, _ = quarantine(data, speaker="阿明")
    assert clean["personal_info"] == {"c": "真的"}


def test_overlong_values_stripped():
    data = {"personal_info": {"story": "很長" * 100, "food": "肉圓"}}
    clean, rejected = quarantine(data, speaker="阿明")
    assert "story" not in clean["personal_info"]
    assert clean["personal_info"]["food"] == "肉圓"
    assert any("story" in r for r in rejected)


def test_fabricated_interaction_year_stripped():
    """LLM 掰「2024 年第一次見面」型污染——bot 2026-03 才誕生。"""
    data = {"personal_info": {
        "first_met": "2024年在頻道認識",
        "birthday_year": "1990",          # 出生年是合法的過去年份 → 保留
    }}
    clean, rejected = quarantine(data, speaker="阿明")
    assert "first_met" not in clean["personal_info"]
    assert clean["personal_info"]["birthday_year"] == "1990"
    assert any("first_met" in r for r in rejected)


def test_taboos_non_string_items_stripped():
    data = {"taboos": ["前女友", 123, None, {"x": 1}, "薪水"]}
    clean, _ = quarantine(data, speaker="阿明")
    assert clean["taboos"] == ["前女友", "薪水"]


def test_non_dict_input_rejected_entirely():
    clean, rejected = quarantine("我是一串 LLM 幻覺", speaker="阿明")
    assert clean == {}
    assert rejected


def test_bot_origin_year_constant():
    assert BOT_ORIGIN_YEAR == 2026
