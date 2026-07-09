"""今夜歌單文字貼文：主題 + 每首歌名與策展理由（使用者要理由+歌曲也貼文字聊天室）。"""
from cogs.music_cog import MusicCog

_build = MusicCog._build_themed_announcement


def test_lists_songs_with_reasons():
    infos = [
        {"title": "人生百態", "_pick_reason": "呼應你們聊的市井人生"},
        {"title": "蝦米攏總來", "_pick_reason": "台語草根、跟今晚台語話題呼應"},
    ]
    msg = _build("人生百態，蝦米攏總來", infos)
    assert "《人生百態，蝦米攏總來》" in msg
    assert "共 2 首" in msg
    assert "人生百態" in msg and "呼應你們聊的市井人生" in msg
    assert "蝦米攏總來" in msg and "台語草根" in msg


def test_song_without_reason_still_listed():
    msg = _build("X", [{"title": "無理由歌"}])
    assert "無理由歌" in msg


def test_truncates_to_discord_limit():
    infos = [{"title": f"歌{i}", "_pick_reason": "理" * 300} for i in range(8)]
    msg = _build("很長", infos)
    assert len(msg) <= 2000
