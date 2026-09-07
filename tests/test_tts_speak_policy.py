"""tts_speak_policy.decide() 窮舉契約測試。

見 docs/tts_speak_policy_spec.md。純函式，無 fixture。
"""
from __future__ import annotations

import pytest

from tts_speak_policy import POLICY, RoomState, SpeakKind, Verdict, decide, load_limit_s

COMMITTED = {
    SpeakKind.JOIN_GREETING, SpeakKind.SUMMON_INTRO, SpeakKind.GAME_HOST,
    SpeakKind.DJ_NARRATION, SpeakKind.SELF_SAY, SpeakKind.EXTERNAL_RELAY,
    SpeakKind.SYSTEM_ALERT,
}
PROACTIVE = {
    SpeakKind.PROACTIVE_TOPIC, SpeakKind.PROACTIVE_MANZAI, SpeakKind.PROACTIVE_MOCK,
    SpeakKind.SOCIAL_FILLER, SpeakKind.STANDUP, SpeakKind.JOKE, SpeakKind.IMITATE,
}


def test_policy_covers_every_kind():
    assert set(POLICY) == set(SpeakKind)


# ── committed：任何逆境都 PLAY ────────────────────────────────────────────────

@pytest.mark.parametrize("kind", sorted(COMMITTED, key=lambda k: k.name))
def test_committed_plays_through_everything(kind):
    hostile = RoomState(
        stream_mode=True, hot_chat=True, last_interrupted=True,
        mixer_load_s=999.0, game_mode=True, user_speaking=True,
    )
    assert decide(kind, hostile).verdict is Verdict.PLAY_OVER  # stream → duck 音樂
    assert decide(kind, RoomState(user_speaking=True, game_mode=True)).verdict is Verdict.PLAY


# ── 非 committed：game_mode 一律 DROP ────────────────────────────────────────

@pytest.mark.parametrize("kind", [k for k in SpeakKind if k not in COMMITTED])
def test_non_committed_dropped_in_game_mode(kind):
    assert decide(kind, RoomState(game_mode=True)).verdict is Verdict.DROP


# ── PROACTIVE：stream / hot_chat 直接 DROP ───────────────────────────────────

@pytest.mark.parametrize("kind", sorted(PROACTIVE, key=lambda k: k.name))
def test_proactive_dropped_on_stream_or_hotchat(kind):
    assert decide(kind, RoomState(stream_mode=True)).verdict is Verdict.DROP
    assert decide(kind, RoomState(hot_chat=True)).verdict is Verdict.DROP
    # 安靜房間、沒等過空檔 → DEFER（尊重 silence gate）
    assert decide(kind, RoomState()).verdict is Verdict.DEFER
    # 等過、有空檔 → PLAY
    assert decide(kind, RoomState().with_silence(True)).verdict is Verdict.PLAY
    # 等過、沒空檔 → DROP（自發不補文字）
    assert decide(kind, RoomState().with_silence(False)).verdict is Verdict.DROP


def test_standup_joke_imitate_are_demoted_not_committed():
    """使用者定案：STANDUP/JOKE/IMITATE 降級成主動類，人講話時不硬蓋。"""
    for kind in (SpeakKind.STANDUP, SpeakKind.JOKE, SpeakKind.IMITATE):
        assert POLICY[kind].committed is False
        assert decide(kind, RoomState().with_silence(False)).verdict is Verdict.DROP


# ── WAKE_REPLY：串流 hotswap、被打斷放棄、講不成補文字 ──────────────────────

def test_wake_reply_paths():
    k = SpeakKind.WAKE_REPLY
    assert decide(k, RoomState(stream_mode=True)).verdict is Verdict.HOTSWAP
    assert decide(k, RoomState(hot_chat=True)).verdict is Verdict.DEFER          # hot_chat 不擋 reply，往下到 silence gate
    assert decide(k, RoomState(last_interrupted=True)).verdict is Verdict.DROP
    assert decide(k, RoomState(mixer_load_s=99.0)).verdict is Verdict.DROP_TO_TEXT
    assert decide(k, RoomState()).verdict is Verdict.DEFER
    assert decide(k, RoomState().with_silence(True)).verdict is Verdict.PLAY
    assert decide(k, RoomState().with_silence(False)).verdict is Verdict.DROP_TO_TEXT   # 使用者定案：DEFER 逾時 → 補文字


def test_wake_ack_is_ephemeral():
    """ack 是 thinking filler：等不到空檔就 DROP（不補文字），佇列淺就丟。"""
    k = SpeakKind.WAKE_ACK
    assert decide(k, RoomState().with_silence(False)).verdict is Verdict.DROP
    assert decide(k, RoomState(mixer_load_s=4.0)).verdict is Verdict.DROP_TO_TEXT  # load_limit 3s
    assert load_limit_s(k) == 3.0


# ── NEWS / MEMORY_CALLBACK 的變體 ───────────────────────────────────────────

def test_news_drops_to_text_when_busy():
    k = SpeakKind.NEWS
    assert decide(k, RoomState(stream_mode=True)).verdict is Verdict.DROP
    assert decide(k, RoomState().with_silence(False)).verdict is Verdict.DROP_TO_TEXT


def test_memory_callback_waits_out_hot_chat():
    k = SpeakKind.MEMORY_CALLBACK
    assert decide(k, RoomState(hot_chat=True)).verdict is Verdict.DEFER
    assert decide(k, RoomState(hot_chat=True).with_silence(True)).verdict is Verdict.PLAY
    assert decide(k, RoomState(hot_chat=True).with_silence(False)).verdict is Verdict.DROP


# ── DEFER 一定能被 with_silence 解析掉（不會無限 DEFER）─────────────────────

@pytest.mark.parametrize("kind", list(SpeakKind))
def test_defer_always_resolves_after_silence_known(kind):
    for silence_ok in (True, False):
        v = decide(kind, RoomState(hot_chat=True, stream_mode=False).with_silence(silence_ok))
        assert v.verdict is not Verdict.DEFER
        v2 = decide(kind, RoomState().with_silence(silence_ok))
        assert v2.verdict is not Verdict.DEFER
