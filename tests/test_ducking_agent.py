"""TDD: DuckingAgent — week 2 of social-catalyst plan.

DuckingAgent **是壓制器，不是發話者**：
  - 不繼承 SpeakAgent
  - 偵測「兩個 speaker 15s 內交替 ≥3 次」=「熱聊狀態」
  - 命中時呼叫 SpeakBus.set_global_multiplier(0.2, ttl_s=30) 壓制其他 agent
  - 自己 confidence 永遠 0（不發話）

Pure core 紀律：detection 是 pure function（吃 buffer + clock，回 bool），
IO 動作（call bus.set_global_multiplier）在 thin shell。
"""
from __future__ import annotations

from ducking_agent import DuckingAgent
from speak_bus import SpeakBus


class _FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _agent(bus: SpeakBus | None = None, *, clock: _FakeClock | None = None, **kwargs):
    return DuckingAgent(
        bus or SpeakBus(),
        clock=clock or _FakeClock(),
        **kwargs,
    )


# ── 偵測規則（從 plan 偽碼來）─────────────────────────────────────────────────

def test_no_hot_chat_when_buffer_short():
    """少於 3 turns → 不熱聊。"""
    a = _agent()
    a.on_utterance("Alice", ts=0.0)
    a.on_utterance("Bob", ts=1.0)
    assert a._detect_hot_chat() is False


def test_hot_chat_two_speakers_alternating_three_turns_within_15s():
    """Alice/Bob/Alice 在 15s 內 → 熱聊。"""
    a = _agent()
    a.on_utterance("Alice", ts=0.0)
    a.on_utterance("Bob", ts=3.0)
    a.on_utterance("Alice", ts=6.0)
    assert a._detect_hot_chat() is True


def test_no_hot_chat_when_three_speakers():
    """三個 speaker 不算（plan 規則）。"""
    a = _agent()
    a.on_utterance("Alice", ts=0.0)
    a.on_utterance("Bob", ts=3.0)
    a.on_utterance("Carol", ts=6.0)
    assert a._detect_hot_chat() is False


def test_no_hot_chat_when_last_two_same_speaker():
    """speakers[-1] == speakers[-2] → 不算（無交替）。"""
    a = _agent()
    a.on_utterance("Bob", ts=0.0)
    a.on_utterance("Alice", ts=3.0)
    a.on_utterance("Alice", ts=6.0)  # 連兩個 Alice 中斷交替
    assert a._detect_hot_chat() is False


def test_window_drops_old_turns():
    """超出 15s 視窗的 turns 不算進去。"""
    a = _agent()
    a.on_utterance("Alice", ts=0.0)
    a.on_utterance("Bob", ts=20.0)    # > 15s 之後才講，Alice 已 out of window
    a.on_utterance("Alice", ts=22.0)  # window=[7s,22s] 內只有 Bob+Alice = 2 turns
    assert a._detect_hot_chat() is False


def test_hot_chat_uses_last_three_within_window():
    """有更早 turn 不影響——只看 window 內最後 3 個。"""
    a = _agent()
    a.on_utterance("Carol", ts=0.0)   # 早期路人
    a.on_utterance("Carol", ts=2.0)
    a.on_utterance("Alice", ts=10.0)
    a.on_utterance("Bob", ts=12.0)
    a.on_utterance("Alice", ts=14.0)
    # window 內全部 5 個，但 last 3 是 [Alice, Bob, Alice] → 熱聊
    # 但 set(all in window) = 3 個 speaker！plan 規則 len(set(speakers[-3:])) == 2
    assert a._detect_hot_chat() is True


# ── 行動：set_global_multiplier ───────────────────────────────────────────────

def test_hot_chat_triggers_bus_multiplier():
    bus = SpeakBus()
    clock = _FakeClock()
    a = _agent(bus, clock=clock)
    a.on_utterance("Alice", ts=0.0); clock.advance(3.0)
    a.on_utterance("Bob", ts=3.0); clock.advance(3.0)
    a.on_utterance("Alice", ts=6.0); clock.advance(0.1)
    # 命中後 multiplier 應已被壓低
    assert bus.get_global_multiplier() == 0.2


def test_cold_room_does_not_touch_multiplier():
    """沒熱聊就不動 multiplier（保持 1.0）。"""
    bus = SpeakBus()
    a = _agent(bus)
    a.on_utterance("Alice", ts=0.0)
    a.on_utterance("Alice", ts=1.0)  # 自言自語
    assert bus.get_global_multiplier() == 1.0


def test_suppress_cooldown_prevents_re_trigger():
    """命中後 cooldown 期內再講話不重新壓制（避免每 turn 一次 multiplier reset）。"""
    bus = SpeakBus()
    clock = _FakeClock()
    a = _agent(bus, clock=clock, suppress_cooldown_s=5.0)
    # 觸發第一次
    a.on_utterance("Alice", ts=0.0); clock.t = 0.0
    a.on_utterance("Bob", ts=3.0); clock.t = 3.0
    a.on_utterance("Alice", ts=6.0); clock.t = 6.0
    first_expiry = bus._multiplier_expiry
    # cooldown 內再來一輪——bus expiry 不變
    a.on_utterance("Bob", ts=7.0); clock.t = 7.0
    a.on_utterance("Alice", ts=8.0); clock.t = 8.0
    assert bus._multiplier_expiry == first_expiry  # 沒被重設


# ── 不變式：不發話 ────────────────────────────────────────────────────────────

def test_ducking_agent_is_not_a_speak_agent():
    """DuckingAgent 不該有 speak_bid method（plan invariant）。"""
    a = _agent()
    assert not hasattr(a, "speak_bid"), "DuckingAgent 不該有 speak_bid——它是壓制器不是發話者"
