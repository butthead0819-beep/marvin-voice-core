"""使用者主動點歌 → 點歌台素材：解析、cover 對應、卡片渲染、curator.songs。"""
from PIL import Image

from diary_comic.song_requests import (
    parse_manual_requests, clean_title, video_id_from_url, build_title_index,
    thumb_url, dj_tally)
from diary_comic.layout import compose_song_card, append_song_card
from diary_comic.curator import curate, CurationPlan
from diary_comic.parser import DiaryEntry


_LOG = (
    "2026-06-22 23:34:02,709 [INFO] STTHistory: [點歌-手動] 使用者=狗與露 | 搜尋=阿杜 | 結果=江蕙 -夢中的情話(Official MV) / 阿爾發音樂\n"
    "2026-06-22 23:38:13,256 [INFO] STTHistory: [點歌-手動] 使用者=weakgogo | 搜尋=海波浪 | 結果=海波浪 / 郭桂彬 - Topic\n"
    "2026-06-23 09:00:00,000 [INFO] STTHistory: [點歌-手動] 使用者=showay | 搜尋=x | 結果=窗外的歌 / Y\n"
    "2026-06-22 23:40:00,000 [INFO] STTHistory: 一般 log 不該被當點歌\n"
)


def test_parse_manual_requests_window_and_full_title():
    since = __import__("datetime").datetime(2026, 6, 22, 22, 0).timestamp()
    until = __import__("datetime").datetime(2026, 6, 23, 5, 0).timestamp()
    reqs = parse_manual_requests(_LOG, since, until)
    assert reqs == [("狗與露", "江蕙 -夢中的情話(Official MV)"), ("weakgogo", "海波浪")]
    # 窗外那筆(09:00)被濾掉、雜訊行不算


def test_parse_includes_voice_requests():
    """[點歌-語音] 也要算進點歌台。昨日 53% 點歌走語音，parser 只抓 [點歌-手動]
    時整批語音點歌的歌名在日記裡完全不顯示（2026-06-23 incident）。
    含 (修正→) 修正標記的語音行也要正確擷取歌名。"""
    log = (
        "2026-06-23 23:10:00,000 [INFO] STTHistory: [點歌-語音] 使用者=狗與露 | 搜尋=關喆 (修正→關喆 想你的夜) | 結果=想你的夜 / 關喆\n"
        "2026-06-23 23:44:52,893 [INFO] STTHistory: [點歌-語音] 使用者=showay | 搜尋=隔壁老樊的多想 | 結果=隔壁老樊 - 多想在平庸的生活擁抱你 / EHPMusicChannel\n"
    )
    since = __import__("datetime").datetime(2026, 6, 23, 0, 0).timestamp()
    until = __import__("datetime").datetime(2026, 6, 24, 0, 0).timestamp()
    reqs = parse_manual_requests(log, since, until)
    assert reqs == [
        ("狗與露", "想你的夜"),
        ("showay", "隔壁老樊 - 多想在平庸的生活擁抱你"),
    ]


def test_clean_title_strips_suffix():
    assert clean_title("江蕙 -夢中的情話(Official MV)") == "江蕙 -夢中的情話"


def test_clean_title_strips_lyric_quote_and_pipe():
    """『歌詞引言』與 ｜/| 分隔的 YT 贅詞要砍掉，留歌名主體。"""
    assert clean_title(
        "隔壁老樊 - 多想在平庸的生活擁抱你『無力，是我們最後難免的結局。』"
    ) == "隔壁老樊 - 多想在平庸的生活擁抱你"
    assert clean_title("ECO ELEPHANT - 中文版 - 產品形象 ｜Official Video") == "ECO ELEPHANT - 中文版 - 產品形象"
    assert clean_title("某歌 | Official Audio") == "某歌"


def test_video_id_and_thumb_url():
    assert video_id_from_url("https://www.youtube.com/watch?v=Zg3SEaYLq5Y") == "Zg3SEaYLq5Y"
    assert video_id_from_url("https://youtu.be/abcdefghijk") == "abcdefghijk"
    assert video_id_from_url("bad") is None
    assert "Zg3SEaYLq5Y" in thumb_url("Zg3SEaYLq5Y")


def test_build_title_index_maps_title_to_vid():
    songs = {"https://www.youtube.com/watch?v=Zg3SEaYLq5Y":
             {"title": "江蕙 -夢中的情話(Official MV)", "webpage_url": "https://www.youtube.com/watch?v=Zg3SEaYLq5Y"}}
    idx = build_title_index(songs)
    assert idx["江蕙 -夢中的情話(Official MV)"] == "Zg3SEaYLq5Y"


def test_dj_tally_orders_by_count():
    reqs = [("a", "x"), ("b", "y"), ("a", "z")]
    assert dj_tally(reqs) == [("a", 2), ("b", 1)]


def test_compose_song_card_text_only():
    card = compose_song_card([("狗與露", "夢中的情話"), ("showay", "海波浪")])
    assert isinstance(card, Image.Image) and card.width == 1080 and card.height > 100


def test_compose_song_card_with_covers_taller():
    reqs = [("a", "歌一"), ("b", "歌二")]
    covers = [Image.new("RGB", (480, 360), (10, 10, 10)), None]  # 一張有圖一張缺
    card = compose_song_card(reqs, covers=covers)
    assert isinstance(card, Image.Image)


def test_append_song_card_grows_page_or_passthrough():
    page = Image.new("RGB", (1080, 1920))
    assert append_song_card(page, []) is page  # 無點歌→原圖
    out = append_song_card(page, [("a", "歌")])
    assert out.height > 1920  # 接了卡片變高


def test_curate_carries_song_requests():
    entries = [DiaryEntry(ts_str=f"2026-06-22 22:{i*3:02d}:00", core=f"主題{i}",
                          speakers=["a", "b"]) for i in range(6)]
    plan = curate([], entries, song_requests=[("狗與露", "夢中的情話")])
    assert isinstance(plan, CurationPlan)
    assert plan.songs == [("狗與露", "夢中的情話")]
