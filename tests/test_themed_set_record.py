"""主題歌單 Step 5：入隊後落 records/themed_sets.jsonl（日記「今夜歌單」用）。

只記「實際入隊」的歌：resolved title + LLM 選歌理由 + webpage_url（縮圖用）。
"""
import json

from themed_playlist import build_themed_set_record, record_themed_set


def _info(title, reason, url=""):
    return {"title": title, "_pick_reason": reason, "webpage_url": url}


def test_build_record_keeps_title_reason_url():
    infos = [
        _info("周杰倫 - 開不了口", "今晚聊到溝通困難，這首最對味",
              "https://www.youtube.com/watch?v=Zg3SEaYLq5Y"),
        _info("陶喆 - 普通朋友", "延續那個欲言又止的氣氛"),
    ]
    rec = build_themed_set_record("溝通卡卡，但總有解方", infos, ts=123.0)
    assert rec["ts"] == 123.0
    assert rec["theme_title"] == "溝通卡卡，但總有解方"
    assert rec["picks"] == [
        {"title": "周杰倫 - 開不了口", "reason": "今晚聊到溝通困難，這首最對味",
         "url": "https://www.youtube.com/watch?v=Zg3SEaYLq5Y"},
        {"title": "陶喆 - 普通朋友", "reason": "延續那個欲言又止的氣氛", "url": ""},
    ]


def test_build_record_skips_info_without_title():
    rec = build_themed_set_record("X", [{"_pick_reason": "r"}, _info("歌", "理由")], ts=1.0)
    assert [p["title"] for p in rec["picks"]] == ["歌"]


def test_record_themed_set_appends_jsonl(tmp_path):
    path = tmp_path / "themed_sets.jsonl"
    record_themed_set("主題A", [_info("歌一", "理由一", "u1")], ts=1.0, path=str(path))
    rec2 = record_themed_set("主題B", [_info("歌二", "理由二", "u2")], ts=2.0, path=str(path))
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["theme_title"] == "主題A"
    assert json.loads(lines[1])["picks"][0]["title"] == "歌二"
    assert rec2["theme_title"] == "主題B"


def test_record_themed_set_noop_when_no_picks(tmp_path):
    path = tmp_path / "themed_sets.jsonl"
    rec = record_themed_set("空", [{"_pick_reason": "no title"}], ts=1.0, path=str(path))
    assert rec["picks"] == []
    assert not path.exists()  # 沒 picks 不寫檔
