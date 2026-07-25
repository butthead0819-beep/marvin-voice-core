"""車載模式開場候選池 wiring 測試（見 memory: main_satellite 誤抓 MusicCog.mm 而非 bot.music_memory）。"""
from types import SimpleNamespace

from main_satellite import resolve_car_owner_pool


def _fake_music_memory(songs: dict):
    return SimpleNamespace(all_songs=lambda: songs)


def test_resolve_car_owner_pool_reads_bot_music_memory_not_musiccog_attr():
    """候選池必須從 vc.bot.music_memory 取資料，不是 MusicCog 物件的 mm/_music_memory 屬性。"""
    songs = {
        "abc123": {
            "title": "晴天",
            "uploader": "周杰倫",
            "requesters": {"狗與露": 5},
            "likes": {},
        }
    }
    # MusicCog 存在，但沒有 mm / _music_memory 屬性（真實現況）——不該讓池子變空。
    fake_music_cog = SimpleNamespace()
    vc = SimpleNamespace(
        bot=SimpleNamespace(
            cogs=SimpleNamespace(get=lambda name: fake_music_cog),
            music_memory=_fake_music_memory(songs),
        )
    )

    pool = resolve_car_owner_pool(vc, "狗與露", now=0.0)

    assert len(pool) == 1
    assert pool[0].anchor_title == "晴天"


def test_resolve_car_owner_pool_empty_when_no_music_memory():
    vc = SimpleNamespace(
        bot=SimpleNamespace(cogs=SimpleNamespace(get=lambda name: None), music_memory=None)
    )

    assert resolve_car_owner_pool(vc, "狗與露", now=0.0) == []
