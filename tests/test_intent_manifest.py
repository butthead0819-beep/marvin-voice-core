"""IntentBus.build_intent_manifest() — 給 cheap classifier 看的 agent 能力地圖。

設計重點：
- 只收 DeclarativeIntentAgent（有 declare_intents() method 且非空）
- state-checking agent（declare_intents 回 []，如 busted/turtle）自動排除
- 裸 IntentAgent（如 NemoClawAgent）沒 declare_intents → 排除
- 每日 cache（同日同 instance 回同 dict，跨日重建）
"""
from intent_agents.base import DeclarativeIntentAgent, IntentSchema
from intent_bus import IntentBus


class _MusicLikeAgent(DeclarativeIntentAgent):
    name = "music_like"
    mode_compatible = frozenset({"normal", "stream"})

    def declare_intents(self):
        return [
            IntentSchema(
                name="play_song",
                confidence=0.9,
                patterns=[r"播放?(?P<song_choice>.+)"],
                required_slots=["song_choice"],
                reason_template="play_song:{song_choice}",
            ),
            IntentSchema(
                name="skip",
                confidence=0.85,
                patterns=[r"下一首|跳過"],
                reason_template="skip",
            ),
        ]


class _StateCheckingAgent(DeclarativeIntentAgent):
    """像 busted99_agent / turtle_soup_agent — 沒 declarative pattern，靠 cog state。"""
    name = "state_only"
    mode_compatible = frozenset({"game"})

    def declare_intents(self):
        return []


class _BareAgent:
    """像 NemoClawAgent — 沒繼承 base，自己寫 bid()。"""
    name = "bare"

    def bid(self, ctx):
        return None


def test_manifest_includes_declarative_agent_with_intents():
    bus = IntentBus([_MusicLikeAgent()])
    manifest = bus.build_intent_manifest(today="2026-05-27")

    assert manifest["version"] == "2026-05-27"
    assert len(manifest["agents"]) == 1
    entry = manifest["agents"][0]
    assert entry["name"] == "music_like"
    assert len(entry["intents"]) == 2
    play = entry["intents"][0]
    assert play["name"] == "play_song"
    assert play["required_slots"] == ["song_choice"]
    assert play["reason_template"] == "play_song:{song_choice}"


def test_manifest_excludes_state_checking_agent_with_empty_intents():
    """declare_intents 回 [] → 不入 manifest（busted/turtle 模式）。"""
    bus = IntentBus([_MusicLikeAgent(), _StateCheckingAgent()])
    manifest = bus.build_intent_manifest(today="2026-05-27")

    names = [a["name"] for a in manifest["agents"]]
    assert "music_like" in names
    assert "state_only" not in names


def test_manifest_excludes_bare_agent_without_declare_intents():
    """NemoClawAgent / 其他裸 class 沒 declare_intents method → 不入 manifest。"""
    bus = IntentBus([_MusicLikeAgent(), _BareAgent()])
    manifest = bus.build_intent_manifest(today="2026-05-27")

    names = [a["name"] for a in manifest["agents"]]
    assert "bare" not in names


def test_manifest_caches_within_same_day():
    """同日多次呼叫 → 同一個 dict (identity)，沒重建。"""
    bus = IntentBus([_MusicLikeAgent()])
    m1 = bus.build_intent_manifest(today="2026-05-27")
    m2 = bus.build_intent_manifest(today="2026-05-27")
    assert m1 is m2


def test_manifest_invalidates_on_new_day():
    """跨日 → 重建新 dict。"""
    bus = IntentBus([_MusicLikeAgent()])
    m1 = bus.build_intent_manifest(today="2026-05-27")
    m2 = bus.build_intent_manifest(today="2026-05-28")
    assert m1 is not m2
    assert m2["version"] == "2026-05-28"


def test_manifest_swallows_declare_intents_exception():
    """一個 agent 的 declare_intents 炸不該影響其他 agent（對齊 bus dispatch 慣例）。"""

    class _BrokenAgent(DeclarativeIntentAgent):
        name = "broken"
        mode_compatible = frozenset({"normal"})

        def declare_intents(self):
            raise RuntimeError("intentional")

    bus = IntentBus([_BrokenAgent(), _MusicLikeAgent()])
    manifest = bus.build_intent_manifest(today="2026-05-27")
    names = [a["name"] for a in manifest["agents"]]
    assert "broken" not in names
    assert "music_like" in names
