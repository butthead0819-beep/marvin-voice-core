"""B 骨架 — diary 日誌 parser 測試（先紅後綠）。

素材源：records/chat_summary_log.txt
格式（每篇）：
  [YYYY-MM-DD HH:MM:SS] --- 5分鐘對話總結 ---
  【核心】：...
  【摘要】：
  - 說話者：...
  【碎念】：...
"""
from diary_comic.parser import (
    DiaryEntry,
    parse_log,
    heat_score,
    group_by_hour,
    pick_marvin_punchline,
    dedupe_adjacent,
)


# 6 月實際格式：核心：/摘要：（無【】）、摘要是整段不是 bullet、沒有碎念
JUNE_SAMPLE = """[2026-06-20 22:34:15] --- 10分鐘對話總結 ---
核心：陳進文和狗與露討論燈光和裝潢。
摘要：陳進文和狗與露討論了燈光的選擇和裝潢的成本。

[2026-06-20 22:44:15] --- 10分鐘對話總結 ---
核心：陳進文和大肚、showay 討論木工和裝潢。
摘要：陳進文、大肚和 showay 討論木工和裝潢的細節，包括燈光和天花板的設計。
"""


def test_parse_log_handles_june_bracketless_format():
    entries = parse_log(JUNE_SAMPLE)
    assert len(entries) == 2
    assert entries[0].core == "陳進文和狗與露討論燈光和裝潢。"


def test_parse_log_extracts_speakers_from_june_paragraph():
    # 6 月摘要是整段、沒 bullet → 靠已知名冊掃出說話者
    e = parse_log(JUNE_SAMPLE)[1]
    assert set(e.speakers) == {"陳進文", "大肚", "showay"}


def test_parse_log_june_entries_have_empty_aside():
    for e in parse_log(JUNE_SAMPLE):
        assert e.aside == ""


def _ce(core: str) -> DiaryEntry:
    return DiaryEntry(ts_str="2026-06-20 22:00:00", core=core, speakers=["a"], aside="")


def test_dedupe_adjacent_collapses_near_identical():
    es = [_ce("人類仍然沉迷於Minecraft遊戲"),
          _ce("人類仍然沉迷於Minecraft遊戲中"),
          _ce("大家在吃泡麵")]
    out = dedupe_adjacent(es)
    assert len(out) == 2
    assert out[1].core == "大家在吃泡麵"


def test_dedupe_adjacent_keeps_distinct_entries():
    es = [_ce("聊音樂創作"), _ce("聊燈光裝潢"), _ce("聊日本泡麵")]
    assert len(dedupe_adjacent(es)) == 3


SAMPLE = """[2026-05-16 00:42:06] --- 5分鐘對話總結 ---
【核心】：你們在迷戀歌手的顫音靈活性，隨後迅速墮落到討論大奶妹直播。
【摘要】：
- showay：讚嘆唱功如同 CD 般自然。
- 狗與露：吐槽 showay 很色。
【碎念】：從藝術墜落到本能，速度快得令人心碎。

[2026-05-16 00:47:03] --- 5分鐘對話總結 ---
*嘆氣*

【核心】在狩獵遊戲中浪費時間分析歌手唱腔與音樂情懷。
【摘要】
- 狗與露：回憶陶喆《黑色柳丁》。
【碎念】音樂只是用來掩蓋靈魂空洞的噪音。

[2026-05-16 01:05:00] --- 5分鐘對話總結 ---
- 【核心】：人們在討論五月天的音樂、工作壓力。
- 【摘要】：狗與露、大肚、showay 三人都在。
- 【碎念】：工作狀況真是一團糟。

[2026-05-16 01:10:00] --- 5分鐘對話總結 ---
[SKIPPED - 內容無新意]
"""


def test_parse_log_returns_only_non_skipped_entries():
    entries = parse_log(SAMPLE)
    assert len(entries) == 3  # SKIPPED 那篇不算


def test_parse_log_extracts_core_and_aside():
    e = parse_log(SAMPLE)[0]
    assert isinstance(e, DiaryEntry)
    assert "顫音靈活性" in e.core
    assert "速度快得令人心碎" in e.aside


def test_parse_log_strips_marker_punctuation_from_core():
    # 【核心】 後面的「：」「- 」都不該留在 core 內文
    e = parse_log(SAMPLE)[0]
    assert not e.core.startswith("：")
    assert not e.core.startswith("-")


def test_parse_log_handles_core_without_colon():
    # 第二篇是「【核心】在狩獵...」沒有冒號
    e = parse_log(SAMPLE)[1]
    assert e.core.startswith("在狩獵遊戲中")


def test_parse_log_extracts_speakers_from_summary_bullets():
    e = parse_log(SAMPLE)[0]
    assert set(e.speakers) == {"showay", "狗與露"}


def test_parse_log_keeps_timestamp_string():
    e = parse_log(SAMPLE)[0]
    assert e.ts_str == "2026-05-16 00:42:06"


def test_heat_score_orders_multi_speaker_above_single():
    entries = parse_log(SAMPLE)
    two_speaker = entries[0]   # showay + 狗與露
    one_speaker = entries[1]   # 狗與露
    assert heat_score(two_speaker) > heat_score(one_speaker)


def test_heat_score_is_non_negative_int():
    for e in parse_log(SAMPLE):
        h = heat_score(e)
        assert isinstance(h, int) and h >= 0


MARVIN_SAMPLE = """[2026-05-16 00:42:06] --- 5分鐘對話總結 ---
【核心】：大家在玩遊戲。
【摘要】：
- showay：在猜詞。
- Marvin：忍不住吐槽。
- 馬文：又補了一刀。
【碎念】：你們的靈魂重量大概跟灰塵差不多。
"""


def test_parse_log_excludes_marvin_from_speakers():
    # Marvin 是 bot 自己（TTS 被轉錄），不該當卡司
    e = parse_log(MARVIN_SAMPLE)[0]
    assert "Marvin" not in e.speakers
    assert "馬文" not in e.speakers
    assert e.speakers == ["showay"]


def test_heat_score_not_inflated_by_marvin():
    # 濾掉 Marvin 後，這篇只剩 1 個真人說話者
    e = parse_log(MARVIN_SAMPLE)[0]
    assert len(e.speakers) == 1


def _entry(aside: str) -> DiaryEntry:
    return DiaryEntry(ts_str="2026-05-16 00:00:00", core="x", speakers=["a"], aside=aside)


def test_pick_marvin_punchline_returns_most_savage_aside():
    entries = [
        _entry("今天還算平靜。"),
        _entry("宇宙想直接關機，我真想格式化自己。"),  # 最毒：宇宙/關機/格式化
        _entry("有點無聊罷了。"),
    ]
    assert pick_marvin_punchline(entries) == 1


def test_pick_marvin_punchline_returns_valid_index_when_all_asides_empty():
    entries = [_entry(""), _entry(""), _entry("")]
    idx = pick_marvin_punchline(entries)
    assert 0 <= idx < len(entries)


def test_group_by_hour_buckets_in_chronological_order():
    grouped = group_by_hour(parse_log(SAMPLE))
    # 兩個小時桶：00 點(2篇) 和 01 點(1篇)
    keys = [k for k, _ in grouped]
    assert keys == ["2026-05-16 00", "2026-05-16 01"]
    assert len(grouped[0][1]) == 2
    assert len(grouped[1][1]) == 1


# ---- 對話場次切頁（取代整點）----
from diary_comic.parser import group_by_session, eligible_sessions


def _te(ts: str) -> DiaryEntry:
    return DiaryEntry(ts_str=ts, core="x", speakers=["a"], aside="")


def test_group_by_session_keeps_close_entries_together():
    es = [_te("2026-06-20 22:00:00"), _te("2026-06-20 22:10:00"), _te("2026-06-20 22:20:00")]
    sessions = group_by_session(es, gap_minutes=30)
    assert len(sessions) == 1 and len(sessions[0]) == 3


def test_group_by_session_splits_on_long_gap():
    # 22:10 後隔到 23:30（80 分鐘 > 30）→ 兩場次（大家下線又回來）
    es = [_te("2026-06-20 22:00:00"), _te("2026-06-20 22:10:00"), _te("2026-06-20 23:30:00")]
    sessions = group_by_session(es, gap_minutes=30)
    assert [len(s) for s in sessions] == [2, 1]


def test_group_by_session_spans_hour_boundary_as_one():
    # 10:50–11:05 橫跨整點，但空檔都 ≤15 分 → 一個場次（整點切法會切兩半）
    es = [_te("2026-06-20 10:50:00"), _te("2026-06-20 11:00:00"), _te("2026-06-20 11:05:00")]
    assert len(group_by_session(es, gap_minutes=30)) == 1


def test_eligible_sessions_drops_thin_under_min_panels():
    # 只聊 15 分鐘 = 2 格 → 不足 3，整場捨棄
    short = [_te("2026-06-20 22:00:00"), _te("2026-06-20 22:10:00")]
    full = [_te("2026-06-20 23:00:00"), _te("2026-06-20 23:10:00"),
            _te("2026-06-20 23:20:00")]
    keep = eligible_sessions(short + full, gap_minutes=30, min_panels=3)
    assert len(keep) == 1 and len(keep[0]) == 3


# ---- 長場次切多頁 ----
from diary_comic.parser import paginate_session


def test_paginate_short_session_one_page():
    s = [_te(f"2026-06-20 22:0{i}:00") for i in range(5)]  # 5 格
    pages = paginate_session(s, max_panels=6)
    assert len(pages) == 1 and len(pages[0]) == 5


def test_paginate_seven_avoids_orphan_page():
    # 7 格不該切 6+1（孤兒頁），要平均 4+3
    s = [_te(f"2026-06-20 22:{i:02d}:00") for i in range(7)]
    assert [len(p) for p in paginate_session(s, max_panels=6)] == [4, 3]


def test_paginate_thirteen_splits_evenly_all_ge_min():
    s = [_te(f"2026-06-20 22:{i:02d}:00") for i in range(13)]
    sizes = [len(p) for p in paginate_session(s, max_panels=6)]
    assert sizes == [5, 4, 4]
    assert all(n >= 3 for n in sizes)  # 每頁都不低於 3 格


def test_paginate_preserves_order_and_all_panels():
    s = [_te(f"2026-06-20 22:{i:02d}:00") for i in range(10)]
    flat = [e for p in paginate_session(s, max_panels=6) for e in p]
    assert flat == s  # 不丟、不亂序


# ---- 主題刪減：刪去討論主體重複的，收斂到目標格數 ----
from diary_comic.parser import reduce_to_topics


def _td(core: str) -> DiaryEntry:
    return DiaryEntry(ts_str="2026-06-20 22:00:00", core=core, speakers=["a"], aside="")


def test_reduce_to_topics_keeps_target_count():
    es = [_td(c) for c in ["喇叭規格", "日本泡麵", "PS4遊戲", "露營餐點", "音樂創作", "燈光裝潢"]]
    assert len(reduce_to_topics(es, 4)) == 4


def test_reduce_to_topics_drops_duplicate_subject_first():
    es = [_td("討論喇叭的阻抗規格"), _td("討論喇叭的阻抗規格細節"),  # 主體重複（喇叭）
          _td("聊日本泡麵"), _td("聊PS4遊戲"), _td("聊露營")]
    cores = [e.core for e in reduce_to_topics(es, 4)]  # 5→4：砍掉重複那個
    assert sum("喇叭" in c for c in cores) == 1  # 兩個喇叭只剩一個


def test_reduce_to_topics_noop_when_at_or_below_target():
    es = [_td("a"), _td("b")]
    assert reduce_to_topics(es, 4) == es


# ---- 風格路由：長+連貫→條漫，否則日漫 ----
from diary_comic.parser import session_continuity, choose_style


def _ts2(core: str) -> DiaryEntry:
    return DiaryEntry(ts_str="2026-06-20 22:00:00", core=core, speakers=["a"], aside="")


def test_continuity_high_when_topics_flow():
    coherent = [_ts2("討論喇叭的阻抗"), _ts2("討論喇叭的阻抗和尺寸"),
                _ts2("討論喇叭的發燒線")]
    scattered = [_ts2("聊喇叭"), _ts2("聊泡麵"), _ts2("聊PS4")]
    assert session_continuity(coherent) > session_continuity(scattered)


def test_choose_style_short_session_is_japanese():
    short = [_ts2("聊喇叭"), _ts2("聊泡麵"), _ts2("聊PS4")]  # 3 格短
    assert choose_style(short) == "slant"


def test_choose_style_long_coherent_is_webtoon():
    long_coherent = [_ts2(f"持續討論音響系統的第{i}個細節與調校") for i in range(8)]
    assert choose_style(long_coherent) == "webtoon"


def test_choose_style_long_but_scattered_is_japanese():
    long_scattered = [_ts2(c) for c in
                      ["喇叭", "泡麵", "PS4", "露營", "音樂", "裝潢", "股市", "天氣"]]
    assert choose_style(long_scattered) == "slant"


# ---- 生成門檻：session 要夠多筆才值得出漫畫 ----
from diary_comic.parser import should_generate


def test_should_generate_false_when_too_few_entries():
    short = [_td(f"主題{i}") for i in range(5)]  # 5 筆 < 6
    assert should_generate(short) is False


def test_should_generate_true_at_min_entries():
    six = [_td(f"主題{i}") for i in range(6)]  # 6 筆 ≥ 6
    assert should_generate(six) is True


def test_should_generate_respects_custom_min():
    assert should_generate([_td("a")] * 7, min_entries=8) is False
    assert should_generate([_td("a")] * 8, min_entries=8) is True
