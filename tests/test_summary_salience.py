"""對話摘要 salience（顯著度）：讓策展繞「獨特/難忘話題」而非通用閒聊。

2026-07-09：實測策展一直繞「人生百態」通用傘、漏掉獨特話題(馬文實體化/琉璃蝦)。根因＝核心句
等權無重要性訊號。解＝summarizer 每句標顯著度(語意非聲量)→高顯著度標【重點】餵策展 LLM。
"""

from chat_summary_parser import DiaryEntry, parse_log


def _ts(dt_offset_min=0):
    import datetime
    t = datetime.datetime.now() - datetime.timedelta(minutes=dt_offset_min)
    return t.strftime("%Y-%m-%d %H:%M:%S")


# ── parser 抽 salience ────────────────────────────────────────────────

def test_parse_log_extracts_salience():
    text = (f"[{_ts()}] --- 10分鐘對話總結 ---\n"
            "核心：狗與露說要把馬文做成實體音箱\n摘要：講開發板+擴大機\n顯著度：高\n\n")
    e = parse_log(text)[0]
    assert e.core.startswith("狗與露")
    assert e.salience == "高"


def test_parse_log_salience_defaults_medium_for_old_format():
    # 舊 entry 無「顯著度」行 → 預設「中」（向後相容、日記不受影響）
    text = f"[{_ts()}] --- 10分鐘對話總結 ---\n核心：聊通勤\n摘要：講校車\n\n"
    assert parse_log(text)[0].salience == "中"


def test_diary_entry_salience_default():
    assert DiaryEntry(ts_str="x", core="c").salience == "中"
