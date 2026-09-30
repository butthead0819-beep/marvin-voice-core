"""9/30：老朋友的個性＝開口就切重點、省話。用個性引導而非字數限制（mistral 不理 45-55 字）。
A/B（各 10 次）：字數中位 80→40、有講歌名 7/10→7/10。拿掉鼓勵講長的三處：三段串接、兩拍轉折、多維度串接。"""
from dj_prompt_builder import build_dj_interjection_prompt


def _p():
    return build_dj_interjection_prompt("歌曲：陳綺貞 - 旅行的意義")


def test_persona_is_terse_old_friend_landing_on_song_name():
    p = _p()
    assert "話不多的老朋友" in p
    assert "說出歌名，就把舞台交給歌" in p
    assert "最後落在歌名上" in p


def test_no_longer_asks_for_three_part_chain_or_two_beat_tone():
    p = _p()
    assert "串成一條線" not in p
    assert "語氣要有轉折" not in p
    assert "多維度串接" not in p
