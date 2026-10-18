"""DJ 扭蛋加三個 mode：天氣提醒 / 提問 / 誇歌。"""
from __future__ import annotations

import json

import pytest

from dj_narration_orchestrator import MODES, DJMaterials
from dj_topic_selector import select_mode
from dj_weather import load_member_cities, weather_items, weather_line, weather_tip

from tests.test_dj_group_tone import _make_cog, _info, _weights


# ── weather_tip ──────────────────────────────────────────────────────────────

def test_weather_tip_rain_wins():
    assert weather_tip({"max": 25, "min": 20, "rain": 60}) == "記得帶傘"


def test_weather_tip_hot():
    assert weather_tip({"max": 34, "min": 25, "rain": 10}) == "注意防曬多喝水"


def test_weather_tip_cold():
    assert weather_tip({"max": 25, "min": 14, "rain": 10}) == "注意保暖"


def test_weather_tip_none():
    assert weather_tip({"max": 25, "min": 20, "rain": 10}) == ""


def test_weather_tip_rain_priority_over_hot():
    assert weather_tip({"max": 34, "min": 25, "rain": 60}) == "記得帶傘"


# ── weather_line ─────────────────────────────────────────────────────────────

def test_weather_line_format():
    assert weather_line(
        ["showay", "大肚"], "高雄", {"max": 31.4, "min": 25.6, "rain": 60}
    ) == "showay、大肚在高雄：今天26到31度，降雨機率60%（提醒：記得帶傘）"


# ── load_member_cities ────────────────────────────────────────────────────────

def test_load_member_cities_ok(tmp_path):
    p = tmp_path / "member_cities.json"
    p.write_text(json.dumps({"showay": "高雄"}), encoding="utf-8")
    assert load_member_cities(str(p)) == {"showay": "高雄"}


def test_load_member_cities_missing(tmp_path):
    assert load_member_cities(str(tmp_path / "nope.json")) == {}


def test_load_member_cities_not_dict(tmp_path):
    p = tmp_path / "member_cities.json"
    p.write_text(json.dumps(["showay", "高雄"]), encoding="utf-8")
    assert load_member_cities(str(p)) == {}


# ── weather_items ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_weather_items_groups_by_city_and_sorts_tip_first(monkeypatch):
    import dj_weather

    monkeypatch.setenv("MARVIN_DJ_WEATHER", "1")
    monkeypatch.setattr(dj_weather, "load_member_cities", lambda: {
        "showay": "高雄", "大肚": "高雄", "狗與露": "台中", "weakgogo": "桃園",
    })

    async def _fake_fetch(city):
        if city == "高雄":
            return {"max": 30, "min": 25, "rain": 10}
        if city == "台中":
            return {"max": 28, "min": 22, "rain": 70}
        return None

    monkeypatch.setattr(dj_weather, "fetch_daily", _fake_fetch)

    lines = await weather_items({"showay", "大肚", "狗與露"})
    assert len(lines) == 2
    assert lines[0].startswith("狗與露在台中")
    assert lines[1] == "showay、大肚在高雄：今天25到30度，降雨機率10%"
    assert "weakgogo" not in "".join(lines)


@pytest.mark.asyncio
async def test_weather_items_present_none_is_empty():
    assert await weather_items(None) == []


def _fake_weather(monkeypatch):
    import dj_weather
    monkeypatch.setattr(dj_weather, "load_member_cities", lambda: {"showay": "高雄", "狗與露": "台中"})

    async def _fake_fetch(city):
        return {"max": 30, "min": 25, "rain": 10}

    monkeypatch.setattr(dj_weather, "fetch_daily", _fake_fetch)


@pytest.mark.asyncio
async def test_weather_items_env_off_is_empty(monkeypatch):
    _fake_weather(monkeypatch)
    monkeypatch.setenv("MARVIN_DJ_WEATHER", "0")
    assert await weather_items({"showay"}) == []


@pytest.mark.asyncio
async def test_weather_items_skips_present_member_without_city(monkeypatch):
    _fake_weather(monkeypatch)
    monkeypatch.setenv("MARVIN_DJ_WEATHER", "1")
    lines = await weather_items({"showay", "陌生人"})
    assert lines == ["showay在高雄：今天25到30度，降雨機率10%"]


# ── select_mode 扭蛋池含新 mode ─────────────────────────────────────────────

def test_select_mode_picks_weather_when_only_weather_has_weight(monkeypatch):
    import dj_topic_selector

    monkeypatch.setattr(dj_topic_selector, "MODE_WEIGHTS", {"weather": 1.0})
    store = dj_topic_selector.TopicCooldownStore(__import__("tempfile").mktemp(suffix=".json"))
    topic, mode = select_mode([], [], store, weather_items=["台中那行"])
    assert (topic, mode) == ("台中那行", "weather")


def test_select_mode_picks_question_when_only_question_has_weight(monkeypatch):
    import dj_topic_selector

    monkeypatch.setattr(dj_topic_selector, "MODE_WEIGHTS", {"question": 1.0})
    store = dj_topic_selector.TopicCooldownStore(__import__("tempfile").mktemp(suffix=".json"))
    topic, mode = select_mode([], [], store)
    assert (topic, mode) == (None, "question")


def test_select_mode_picks_praise_when_only_praise_has_weight(monkeypatch):
    import dj_topic_selector

    monkeypatch.setattr(dj_topic_selector, "MODE_WEIGHTS", {"praise": 1.0})
    store = dj_topic_selector.TopicCooldownStore(__import__("tempfile").mktemp(suffix=".json"))
    topic, mode = select_mode([], [], store)
    assert (topic, mode) == (None, "praise")


# ── render ───────────────────────────────────────────────────────────────────

def test_render_weather_contains_topic_and_tip_instruction():
    lines = MODES["weather"].render("showay在高雄：今天26到31度，降雨機率60%（提醒：記得帶傘）", DJMaterials())
    joined = "\n".join(lines)
    assert "showay在高雄" in joined
    assert "提醒" in joined


def test_render_question_has_no_literal_example_sentence():
    lines = MODES["question"].render(None, DJMaterials())
    joined = "\n".join(lines)
    assert "不准用「我」" in joined


def test_render_praise_has_no_literal_example_sentence():
    lines = MODES["praise"].render(None, DJMaterials())
    joined = "\n".join(lines)
    assert "不准用「我」" in joined


# ── 接線：真的跑 _fetch_dj_interjection_raw，LLM context 含誇歌指令 ───────────

@pytest.mark.asyncio
async def test_praise_mode_wired_into_llm_context(monkeypatch):
    _weights(
        monkeypatch,
        life=0.0, interest=0.0, news=0.0, callback=0.0, activity=0.0, weather=0.0,
        guide=0.0, conversation=0.0, perspective=0.0, atmosphere=0.0, quick=0.0,
        question=0.0, praise=1.0,
    )
    cog = _make_cog(online_members=["大肚", "狗與露"])
    await cog._fetch_dj_interjection_raw(_info())
    call = cog.bot.router.generate_dynamic_system_msg.call_args
    assert call is not None
    ctx = call.kwargs.get("context", "") or (call.args[1] if len(call.args) > 1 else "")
    assert "熱情推這首歌" in ctx


@pytest.mark.asyncio
async def test_weather_mode_wired_into_llm_context(monkeypatch):
    """cog 抓到的天氣行要一路進 DJMaterials → 扭蛋 → LLM context。"""
    _fake_weather(monkeypatch)
    monkeypatch.setenv("MARVIN_DJ_WEATHER", "1")
    _weights(
        monkeypatch,
        life=0.0, interest=0.0, news=0.0, callback=0.0, activity=0.0, weather=1.0,
        guide=0.0, conversation=0.0, perspective=0.0, atmosphere=0.0, quick=0.0,
        question=0.0, praise=0.0,
    )
    cog = _make_cog(online_members=["showay", "狗與露"])
    await cog._fetch_dj_interjection_raw(_info())
    call = cog.bot.router.generate_dynamic_system_msg.call_args
    assert call is not None
    ctx = call.kwargs.get("context", "") or (call.args[1] if len(call.args) > 1 else "")
    assert "狗與露在台中：今天25到30度" in ctx or "showay在高雄：今天25到30度" in ctx
