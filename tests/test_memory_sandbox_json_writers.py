"""沙盒下整檔 JSON 覆寫者全部 no-op（並行最危險的一類寫入）。

涵蓋：music_memory._save / taste_profile.write_profile / room_mood_state.dump /
gemini_router_content.save_dna（DNA 性格演化）。整檔覆寫並行會 nuke live bot 的成果，
沙盒下絕不落盤。
"""
import os

import pytest

import memory_sandbox


@pytest.fixture(autouse=True)
def _clean():
    memory_sandbox.deactivate()
    yield
    memory_sandbox.deactivate()


def test_music_memory_save_noop(tmp_path):
    from music_memory import MusicMemory
    path = str(tmp_path / "music_memory.json")
    mm = MusicMemory(path=path)
    # 正本先有內容
    mm._data = {"songs": {"seed": {}}, "recommendations": {}}
    mm._save()
    assert os.path.exists(path)
    mtime = os.path.getmtime(path)

    memory_sandbox.activate()
    mm2 = MusicMemory(path=path)
    mm2._data["songs"]["ghost"] = {}  # RAM 改動
    mm2._save()  # no-op
    # 檔案內容/時間戳沒被碰
    assert os.path.getmtime(path) == mtime
    # RAM 內連貫
    assert "ghost" in mm2._data["songs"]


def test_taste_profile_write_noop(tmp_path):
    from taste_profile import write_profile
    path = str(tmp_path / "taste_profile.json")
    memory_sandbox.activate()
    write_profile(path, "狗與露", {"seed_video_ids": ["x"]})
    assert not os.path.exists(path)


def test_room_mood_dump_noop(tmp_path):
    from room_mood_state import RoomMoodStateStore
    path = str(tmp_path / "room_mood_state.json")
    store = RoomMoodStateStore(dump_path=path)
    store.set_individual_mood(1, "A", "happy")
    memory_sandbox.activate()
    store.dump()
    assert not os.path.exists(path)


def test_save_dna_noop(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from gemini_router_content import GeminiRouterContentMixin
    obj = GeminiRouterContentMixin.__new__(GeminiRouterContentMixin)
    obj.dna_file = str(tmp_path / "suki_dna.json")
    obj.dna = {}
    memory_sandbox.activate()
    obj.save_dna({"warmth": 0.9})
    assert not os.path.exists(obj.dna_file)
    # RAM 內仍更新（性格 session 連貫）
    assert obj.dna.get("warmth") == 0.9
