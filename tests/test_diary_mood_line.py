"""日記「氣氛」行（基調｜情緒事件）：給 DJ 口白讀空氣用，見 dj_mood_hint.py。

2026-10-18 使用者定：日記多一行氣氛，只內化成說話方式，不准講出來。
"""

import inspect

from chat_summary_parser import parse_log


def _ts(dt_offset_min=0):
    import datetime
    t = datetime.datetime.now() - datetime.timedelta(minutes=dt_offset_min)
    return t.strftime("%Y-%m-%d %H:%M:%S")


def test_parse_log_extracts_mood_with_event():
    text = (f"[{_ts()}] --- 10分鐘對話總結 ---\n"
            "核心：聊排球賽\n摘要：showay 跟狗與露討論週末賽程\n"
            "氣氛：熱絡｜showay 不爽隊友講不聽\n顯著度：中\n\n")
    e = parse_log(text)[0]
    assert e.mood == "熱絡｜showay 不爽隊友講不聽"


def test_parse_log_mood_missing_for_old_format():
    text = f"[{_ts()}] --- 10分鐘對話總結 ---\n核心：聊通勤\n摘要：講校車\n顯著度：中\n\n"
    assert parse_log(text)[0].mood == ""


def test_parse_log_mood_dash_becomes_empty():
    text = (f"[{_ts()}] --- 10分鐘對話總結 ---\n"
            "核心：聊通勤\n摘要：講校車\n氣氛：-\n顯著度：中\n\n")
    assert parse_log(text)[0].mood == ""


def test_mood_line_does_not_break_other_fields():
    text = (f"[{_ts()}] --- 10分鐘對話總結 ---\n"
            "核心：聊排球賽\n摘要：showay 跟狗與露討論週末賽程\n"
            "氣氛：熱絡｜showay 不爽隊友講不聽\n顯著度：高\nmeme：排球\n\n")
    e = parse_log(text)[0]
    assert e.core == "聊排球賽"
    assert e.salience == "高"
    assert e.meme_id == "排球"
    assert e.mood == "熱絡｜showay 不爽隊友講不聽"


def test_ambient_diary_prompt_mentions_mood_line():
    import marvin_prompts
    src = inspect.getsource(marvin_prompts)
    start = src.index('"ambient_diary": (')
    block = src[start:src.index("),", start)]
    assert "氣氛：" in block
    assert "｜" in block
    assert "五行" in block
