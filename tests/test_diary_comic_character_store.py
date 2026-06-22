"""B — character bible（speaker → 固定動物）測試。

跨格/跨天角色一致性的地基：每個常客一個固定動物 + 一致外型描述。
新人沒在冊上 → fallback 一隻通用動物。
"""
from diary_comic.character_store import describe, cast_description, Character


def test_describe_known_speaker_returns_its_animal():
    assert "beaver" in describe("陳進文").lower()
    assert "owl" in describe("showay").lower()
    assert "cat" in describe("大肚").lower()


def test_describe_dog_variants_map_to_same_character():
    # 狗與露 / 狗與鹿 是同一人的 STT 變體 → 同一隻狗
    assert describe("狗與露") == describe("狗與鹿")
    assert "dog" in describe("狗與露").lower()


def test_describe_unknown_speaker_returns_fallback():
    d = describe("某個路人ABC")
    assert d == describe("另一個沒見過的人")  # 未知都對到同一隻 fallback
    assert "duck" in d.lower()


def test_cast_description_includes_all_speakers():
    text = cast_description(["陳進文", "showay", "大肚"])
    assert "beaver" in text.lower()
    assert "owl" in text.lower()
    assert "cat" in text.lower()


def test_cast_description_empty_speakers_is_empty_string():
    assert cast_description([]) == ""


def test_character_is_dataclass_with_animal_and_appearance():
    c = Character(animal="fox", appearance="a sly red fox")
    assert c.animal == "fox" and "fox" in c.appearance


# ---- 接 impression_engine 人設 ----
from diary_comic.character_store import persona, persona_brief


def test_persona_pulls_speech_dna_for_known_speaker():
    p = persona("showay")
    assert p["style_summary"] and p["catchphrases"]  # 有人設 + 口頭禪


def test_persona_unknown_speaker_is_empty_not_crash():
    p = persona("路人甲不存在")
    assert p["style_summary"] == "" and p["catchphrases"] == []


def test_persona_brief_has_animal_and_catchphrase():
    b = persona_brief("大肚")
    assert "cat" in b and ("你知不知道" in b or "我聽說" in b)
