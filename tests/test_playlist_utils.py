"""TDD: playlist_utils 歌單解析與匯出格式化工具測試。"""
from __future__ import annotations

import json
import pytest
from playlist_utils import (
    format_playlist_text,
    format_playlist_json,
    format_playlist_csv,
    format_playlist_export,
    parse_playlist_content,
    is_youtube_playlist_url,
)


@pytest.fixture
def sample_songs():
    return [
        {
            "title": "晴天",
            "uploader": "周杰倫",
            "webpage_url": "https://www.youtube.com/watch?v=DYptgVvkVLQ",
            "user_plays": 5,
            "total_plays": 10,
            "liked": True,
        },
        {
            "title": "稻香",
            "uploader": "周杰倫",
            "webpage_url": "https://www.youtube.com/watch?v=sHD_z90E44U",
            "user_plays": 3,
            "total_plays": 6,
            "liked": False,
        },
    ]


def test_format_playlist_text(sample_songs):
    text = format_playlist_text(sample_songs)
    assert "晴天" in text
    assert "周杰倫" in text
    assert "https://www.youtube.com/watch?v=DYptgVvkVLQ" in text
    assert "1." in text
    assert "2." in text


def test_format_playlist_json(sample_songs):
    json_str = format_playlist_json(sample_songs)
    data = json.loads(json_str)
    assert len(data) == 2
    assert data[0]["title"] == "晴天"
    assert data[0]["liked"] is True


def test_format_playlist_csv(sample_songs):
    csv_str = format_playlist_csv(sample_songs)
    assert "title,uploader,url,user_plays,total_plays,liked" in csv_str.lower()
    assert "晴天" in csv_str
    assert "周杰倫" in csv_str


def test_format_playlist_export(sample_songs):
    summary, buf, ext = format_playlist_export(sample_songs, "json", "小明")
    assert "小明" in summary
    assert ext == "json"
    assert len(buf) > 0


def test_parse_playlist_json():
    raw_json = json.dumps([
        {"title": "夜曲", "uploader": "周杰倫", "webpage_url": "https://www.youtube.com/watch?v=aaa"},
        {"title": "七里香", "uploader": "周杰倫", "url": "https://www.youtube.com/watch?v=bbb"},
    ])
    parsed = parse_playlist_content(raw_json, "json")
    assert len(parsed) == 2
    assert parsed[0]["title"] == "夜曲"
    assert parsed[1]["title"] == "七里香"


def test_parse_playlist_csv():
    raw_csv = (
        "title,uploader,url\n"
        "夜曲,周杰倫,https://www.youtube.com/watch?v=aaa\n"
        "七里香,周杰倫,https://www.youtube.com/watch?v=bbb\n"
    )
    parsed = parse_playlist_content(raw_csv, "csv")
    assert len(parsed) == 2
    assert parsed[0]["title"] == "夜曲"
    assert parsed[1]["webpage_url"] == "https://www.youtube.com/watch?v=bbb"


def test_parse_playlist_txt_lines():
    raw_txt = (
        "1. 周杰倫 - 夜曲 (https://www.youtube.com/watch?v=aaa)\n"
        "https://www.youtube.com/watch?v=bbb\n"
        "告五人 - 愛人錯過\n"
    )
    parsed = parse_playlist_content(raw_txt, "txt")
    assert len(parsed) == 3
    assert parsed[0]["webpage_url"] == "https://www.youtube.com/watch?v=aaa"
    assert parsed[1]["webpage_url"] == "https://www.youtube.com/watch?v=bbb"
    assert "愛人錯過" in parsed[2]["title"]


def test_is_youtube_playlist_url():
    assert is_youtube_playlist_url("https://www.youtube.com/playlist?list=PL12345") is True
    assert is_youtube_playlist_url("https://music.youtube.com/playlist?list=PL12345") is True
    assert is_youtube_playlist_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL12345") is True
    assert is_youtube_playlist_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ") is False
    assert is_youtube_playlist_url("周杰倫 晴天") is False
