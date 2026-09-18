"""Tests for MemoryManager external-write detection + cache reload (lost-update fix).

Bug: bot 進程的 MemoryManager 啟動時把玩家讀進 self._cache，之後永不重讀 DB。
daily review（另一個進程/連線）把新 taste 寫進 marvin.db 後，bot 對同一玩家任何
_save_player() 都會把舊快取整筆寫回 DB，蓋掉 daily review 的寫入。
"""
import memory_sandbox
from suki_memory import MemoryManager


def test_external_write_lost_update_regression(tmp_path):
    db = str(tmp_path / "m.db")
    j = str(tmp_path / "m.json")

    bot = MemoryManager(db_path=db, json_compat_path=j)
    bot.get_player_memory("狗與露")  # 建檔

    review = MemoryManager(db_path=db, json_compat_path=j)
    review.replace_player_memory(
        "狗與露",
        {
            **review.get_player_memory("狗與露"),
            "taste": {
                "周杰倫": {
                    "score": 10.0,
                    "mentions": 3,
                    "first_seen": 1.0,
                    "last_update": 1.0,
                }
            },
        },
    )

    bot.record_taste_signal("狗與露", "法拉利", 1.0)

    fresh = MemoryManager(db_path=db, json_compat_path=j)
    taste = fresh.get_player_memory("狗與露")["taste"]
    assert "周杰倫" in taste, "周杰倫被蓋掉 → lost-update 沒修好"
    assert "法拉利" in taste
    assert "周杰倫" in fresh.get_player_memory("狗與露")["likes"]


def test_bot_sees_review_write_via_get_player_memory(tmp_path):
    db = str(tmp_path / "m.db")
    j = str(tmp_path / "m.json")

    bot = MemoryManager(db_path=db, json_compat_path=j)
    bot.get_player_memory("狗與露")

    review = MemoryManager(db_path=db, json_compat_path=j)
    review.replace_player_memory(
        "狗與露",
        {
            **review.get_player_memory("狗與露"),
            "taste": {
                "周杰倫": {
                    "score": 10.0,
                    "mentions": 3,
                    "first_seen": 1.0,
                    "last_update": 1.0,
                }
            },
        },
    )

    assert "周杰倫" in bot.get_player_memory("狗與露")["taste"]


def test_bot_sees_new_player_created_by_review(tmp_path):
    db = str(tmp_path / "m.db")
    j = str(tmp_path / "m.json")

    bot = MemoryManager(db_path=db, json_compat_path=j)
    bot.get_player_memory("狗與露")

    review = MemoryManager(db_path=db, json_compat_path=j)
    review.get_player_memory("阿明")  # 建新玩家

    assert bot.has_player("阿明") is True
    assert "阿明" in bot.list_players()


def test_no_reload_when_no_external_write(tmp_path):
    db = str(tmp_path / "m.db")
    j = str(tmp_path / "m.json")

    bot = MemoryManager(db_path=db, json_compat_path=j)
    p1 = bot.get_player_memory("狗與露")
    bot.record_taste_signal("狗與露", "法拉利", 1.0)

    assert bot.get_player_memory("狗與露") is p1


def test_sandbox_mode_does_not_reload(tmp_path):
    db = str(tmp_path / "m.db")
    j = str(tmp_path / "m.json")

    bot = MemoryManager(db_path=db, json_compat_path=j)
    bot.get_player_memory("狗與露")

    review = MemoryManager(db_path=db, json_compat_path=j)
    review.replace_player_memory(
        "狗與露",
        {
            **review.get_player_memory("狗與露"),
            "taste": {
                "周杰倫": {
                    "score": 10.0,
                    "mentions": 3,
                    "first_seen": 1.0,
                    "last_update": 1.0,
                }
            },
        },
    )

    memory_sandbox.activate()
    try:
        bot.add_song_history("狗與露", "沙盒歌")
        player = bot.get_player_memory("狗與露")
        assert "周杰倫" not in player.get("taste", {})
        assert "沙盒歌" in player.get("song_history", [])
    finally:
        memory_sandbox.deactivate()


def test_unrelated_table_write_does_not_replace_player_dicts(tmp_path):
    """transcripts/budget 等同庫其他表幾乎每句話都寫——不能因此重建玩家 dict。"""
    import sqlite3
    db = str(tmp_path / "m.db")
    j = str(tmp_path / "m.json")

    bot = MemoryManager(db_path=db, json_compat_path=j)
    p1 = bot.get_player_memory("狗與露")

    other = sqlite3.connect(db)
    other.execute("CREATE TABLE IF NOT EXISTS transcripts (id INTEGER PRIMARY KEY, text TEXT)")
    other.execute("INSERT INTO transcripts (text) VALUES ('hi')")
    other.commit()
    other.close()

    assert bot.get_player_memory("狗與露") is p1


def test_only_externally_changed_player_is_reloaded(tmp_path):
    db = str(tmp_path / "m.db")
    j = str(tmp_path / "m.json")

    bot = MemoryManager(db_path=db, json_compat_path=j)
    dog = bot.get_player_memory("狗與露")
    fat = bot.get_player_memory("大肚")

    review = MemoryManager(db_path=db, json_compat_path=j)
    review.record_taste_signal("狗與露", "周杰倫", 5.0)

    assert bot.get_player_memory("大肚") is fat
    new_dog = bot.get_player_memory("狗與露")
    assert new_dog is not dog
    assert "周杰倫" in new_dog["likes"]
