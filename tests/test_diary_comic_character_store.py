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


def test_cast_quirks_gives_expression_cue_for_known_speaker():
    from diary_comic.character_store import cast_quirks
    q = cast_quirks(["大肚"])
    assert "大肚" in q and ("好奇" in q or "自嘲" in q)  # 情緒風格進表情提示


def test_cast_quirks_unknown_is_empty():
    from diary_comic.character_store import cast_quirks
    assert cast_quirks(["路人不存在"]) == ""


def test_persona_includes_recent_topics_from_daily_dna(tmp_path, monkeypatch):
    import json
    import diary_comic.character_store as cs
    (tmp_path / "speech_dna_showay.json").write_text(
        json.dumps({"stress_topics": "tech（句長+11字）、work（句長+5字）、drinking（句長+2字）"}),
        encoding="utf-8")
    monkeypatch.setattr(cs, "_DNA_DIR", str(tmp_path))
    p = cs.persona("showay")
    assert p["recent_topics"] == "tech、work、drinking"  # 每日更新的近期興趣
    assert "近期" in cs.persona_brief("showay")            # brief 帶進近期愛聊


def test_persona_recent_topics_missing_file_empty(tmp_path, monkeypatch):
    import diary_comic.character_store as cs
    monkeypatch.setattr(cs, "_DNA_DIR", str(tmp_path))
    assert cs.persona("showay")["recent_topics"] == ""  # 無檔不爆
