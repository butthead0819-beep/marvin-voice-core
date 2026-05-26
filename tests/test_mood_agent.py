"""MoodAgent — Week 3 tests.

職責（per docs/social_catalyst_plan.md）：
  - 讀 mood_sensor.current_vibe() → group_mood
  - 讀 temperature_monitor.temperature → group_temperature
  - 計算 time_bucket（morning / afternoon / evening / late_night）
  - 寫進 RoomMoodState（不發話）
  - 提供 action_tier 給其他 agent（none / light / mid / heavy）

不變式：
  - 不繼承 SpeakAgent，不自己發話
  - mood_sensor / temperature_monitor 為 None 時退化預設值（never raise）
  - 不寫 bot 自己的 mood，只寫房間整體
"""
from __future__ import annotations

import pytest

from mood_agent import MoodAgent
from room_mood_state import RoomMoodStateStore


# ── stub: mood_sensor ─────────────────────────────────────────────────────────


class _StubVibe:
    def __init__(self, mood: str, engagement: float = 0.5) -> None:
        self.mood = mood
        self.engagement = engagement


class _StubMoodSensor:
    def __init__(self, mood: str = "放鬆", engagement: float = 0.5, fail: bool = False) -> None:
        self._mood = mood
        self._engagement = engagement
        self._fail = fail
        self.call_count = 0

    async def current_vibe(self, guild_id: int, force_refresh: bool = False):
        self.call_count += 1
        if self._fail:
            raise RuntimeError("mood_sensor down")
        return _StubVibe(self._mood, self._engagement)


class _StubTempMonitor:
    def __init__(self, temperature: float = 0.5) -> None:
        self.temperature = temperature


class _Clock:
    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def mood_store(tmp_path) -> RoomMoodStateStore:
    return RoomMoodStateStore(dump_path=str(tmp_path / "mood.json"))


# ── 1. observe writes group state ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_observe_writes_group_mood_to_store(mood_store):
    agent = MoodAgent(
        mood_store=mood_store,
        mood_sensor=_StubMoodSensor(mood="低落", engagement=0.3),
        temperature_monitor=_StubTempMonitor(0.3),
    )
    await agent.observe(channel_id=100, guild_id=1)
    state = mood_store.get(100)
    assert state.group_mood == "低落"
    assert state.group_temperature == pytest.approx(0.3)


@pytest.mark.asyncio
async def test_observe_uses_temperature_monitor_over_engagement(mood_store):
    """temperature_monitor 是真實值（VibeLabel.engagement 已是它的 mirror）。
    優先 temperature_monitor 確保 source-of-truth 一致。"""
    agent = MoodAgent(
        mood_store=mood_store,
        mood_sensor=_StubMoodSensor(mood="興奮", engagement=0.5),
        temperature_monitor=_StubTempMonitor(0.9),
    )
    await agent.observe(channel_id=100, guild_id=1)
    assert mood_store.get(100).group_temperature == pytest.approx(0.9)


@pytest.mark.asyncio
async def test_observe_gracefully_handles_mood_sensor_failure(mood_store):
    agent = MoodAgent(
        mood_store=mood_store,
        mood_sensor=_StubMoodSensor(fail=True),
        temperature_monitor=_StubTempMonitor(0.5),
    )
    # 不該 raise
    await agent.observe(channel_id=100, guild_id=1)
    # 失敗 → 用 RoomMoodState 預設 mood
    assert mood_store.get(100).group_mood == "放鬆"


@pytest.mark.asyncio
async def test_observe_works_without_temperature_monitor(mood_store):
    agent = MoodAgent(
        mood_store=mood_store,
        mood_sensor=_StubMoodSensor(mood="興奮"),
        temperature_monitor=None,
    )
    await agent.observe(channel_id=100, guild_id=1)
    state = mood_store.get(100)
    assert state.group_mood == "興奮"
    assert state.group_temperature == pytest.approx(0.0)  # default


@pytest.mark.asyncio
async def test_observe_works_without_mood_sensor(mood_store):
    agent = MoodAgent(
        mood_store=mood_store,
        mood_sensor=None,
        temperature_monitor=_StubTempMonitor(0.5),
    )
    await agent.observe(channel_id=100, guild_id=1)
    # 沒 sensor → 不該寫 group_mood，保留 default
    assert mood_store.get(100).group_mood == "放鬆"


# ── 2. time_bucket ───────────────────────────────────────────────────────────


def test_time_bucket_morning():
    # 8 AM 本地時間（CST，UTC+8）= 0 UTC = unix_ts 寫一個 8 點的本地時間
    # 用 _Clock 控制就行：8 * 3600 = 28800（unix ts 28800 ≒ 1970-01-01 08:00 UTC）
    clock = _Clock(t=8 * 3600)  # 08:00 UTC
    agent = MoodAgent(mood_store=RoomMoodStateStore(), clock=clock)
    assert agent.time_bucket() in {"morning", "afternoon", "evening", "late_night"}


def test_time_bucket_returns_all_four_for_24h():
    """跑 24 小時，四個 bucket 全部出現過。"""
    seen = set()
    for hour in range(24):
        clock = _Clock(t=hour * 3600)
        agent = MoodAgent(mood_store=RoomMoodStateStore(), clock=clock)
        seen.add(agent.time_bucket())
    assert seen == {"morning", "afternoon", "evening", "late_night"}


# ── 3. action tier ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_action_tier_none_when_relaxed(mood_store):
    agent = MoodAgent(
        mood_store=mood_store,
        mood_sensor=_StubMoodSensor(mood="放鬆", engagement=0.7),
        temperature_monitor=_StubTempMonitor(0.7),
    )
    await agent.observe(channel_id=100, guild_id=1)
    assert agent.get_action_tier(channel_id=100, silence_seconds=0) == "none"


@pytest.mark.asyncio
async def test_action_tier_light_when_only_one_axis_down(mood_store):
    """單純 mood=低落 但 temperature 還不太低 → light。"""
    agent = MoodAgent(
        mood_store=mood_store,
        mood_sensor=_StubMoodSensor(mood="低落", engagement=0.6),
        temperature_monitor=_StubTempMonitor(0.6),
    )
    await agent.observe(channel_id=100, guild_id=1)
    assert agent.get_action_tier(channel_id=100, silence_seconds=10) == "light"


@pytest.mark.asyncio
async def test_action_tier_mid_when_mood_and_temperature_both_down(mood_store):
    agent = MoodAgent(
        mood_store=mood_store,
        mood_sensor=_StubMoodSensor(mood="低落"),
        temperature_monitor=_StubTempMonitor(0.4),
    )
    await agent.observe(channel_id=100, guild_id=1)
    assert agent.get_action_tier(channel_id=100, silence_seconds=10) == "mid"


@pytest.mark.asyncio
async def test_action_tier_mid_for_divergent_mood(mood_store):
    """分歧也是 mid signal（多人情緒不一致需要 bridge agent 調和）。"""
    agent = MoodAgent(
        mood_store=mood_store,
        mood_sensor=_StubMoodSensor(mood="分歧"),
        temperature_monitor=_StubTempMonitor(0.4),
    )
    await agent.observe(channel_id=100, guild_id=1)
    assert agent.get_action_tier(channel_id=100, silence_seconds=10) == "mid"


@pytest.mark.asyncio
async def test_action_tier_heavy_when_negative_and_silent(mood_store):
    """低落 + 低溫 + 群體靜默 ≥ 60s → heavy（bot 該退）。"""
    agent = MoodAgent(
        mood_store=mood_store,
        mood_sensor=_StubMoodSensor(mood="低落"),
        temperature_monitor=_StubTempMonitor(0.2),
    )
    await agent.observe(channel_id=100, guild_id=1)
    assert agent.get_action_tier(channel_id=100, silence_seconds=120) == "heavy"


@pytest.mark.asyncio
async def test_action_tier_uses_current_state_not_stale(mood_store):
    """observe 兩次 → 後值覆蓋前值，action_tier 反映最新。"""
    sensor = _StubMoodSensor(mood="低落")
    temp = _StubTempMonitor(0.2)
    agent = MoodAgent(mood_store=mood_store, mood_sensor=sensor, temperature_monitor=temp)
    await agent.observe(channel_id=100, guild_id=1)
    assert agent.get_action_tier(channel_id=100, silence_seconds=120) == "heavy"

    sensor._mood = "興奮"
    temp.temperature = 0.8
    await agent.observe(channel_id=100, guild_id=1)
    assert agent.get_action_tier(channel_id=100, silence_seconds=120) == "none"


# ── 4. invariants ────────────────────────────────────────────────────────────


def test_mood_agent_is_not_a_speak_agent(mood_store):
    """plan 不變式：MoodAgent 不發話。"""
    agent = MoodAgent(mood_store=mood_store)
    assert not hasattr(agent, "speak_bid"), "MoodAgent 不該有 speak_bid"


@pytest.mark.asyncio
async def test_observe_does_not_write_individual_mood_for_bot(mood_store):
    """plan 不變式：不寫 bot 自己的 mood。observe 只動 group_*，不動 individual_mood。"""
    agent = MoodAgent(
        mood_store=mood_store,
        mood_sensor=_StubMoodSensor(mood="低落"),
    )
    await agent.observe(channel_id=100, guild_id=1)
    assert mood_store.get(100).individual_mood == {}  # 沒被寫過


# ── 5. snapshot return ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_observe_returns_snapshot_dict(mood_store):
    agent = MoodAgent(
        mood_store=mood_store,
        mood_sensor=_StubMoodSensor(mood="興奮"),
        temperature_monitor=_StubTempMonitor(0.8),
    )
    snap = await agent.observe(channel_id=100, guild_id=1)
    assert snap["mood"] == "興奮"
    assert snap["temperature"] == pytest.approx(0.8)
    assert "time_bucket" in snap
