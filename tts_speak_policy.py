"""tts_speak_policy — 用「話語是什麼」+ 房間狀態決定怎麼播，取代 play_tts 的旗標。

見 docs/tts_speak_policy_spec.md。純函式，無 I/O、無 VoiceController 依賴，方便窮舉測試。

呼叫端（voice_controller_playback._tts_suppressed）流程：
    room = RoomState(...現成的值...)
    v = decide(kind, room)
    if v is Verdict.DEFER:
        room = room.with_silence(await self._wait_for_user_silence())
        v = decide(kind, room)            # 這次 silence 已知 → 不會再回 DEFER
    → 依 v 播 / duck / 補文字 / 丟
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, replace
from typing import NamedTuple


class SpeakKind(enum.Enum):
    # ── committed 事件：決定要說就一定說，不被任何房間狀態擋 ──────────────────
    JOIN_GREETING = "join_greeting"      # 有人加入 → 司儀播報句
    SUMMON_INTRO = "summon_intro"        # /summon / auto-join 登場台詞
    GAME_HOST = "game_host"              # 遊戲主持台詞（busted99 / turtle soup / game_cog）
    DJ_NARRATION = "dj_narration"        # 歌曲間口白
    SELF_SAY = "self_say"                # 使用者 /say 叫 Marvin 唸一句
    EXTERNAL_RELAY = "external_relay"    # NemoClaw / Marmo / OpenClaw 轉述
    SYSTEM_ALERT = "system_alert"        # 額度耗盡撤離等關機前通知

    # ── 回應類：使用者喚醒後的答覆，尊重「人還在講話」但被打斷就放棄 ──────────
    WAKE_REPLY = "wake_reply"            # 喚醒問答的正式答覆
    WAKE_ACK = "wake_ack"                # 「嗯?」thinking filler，可有可無
    RESULT_NOTICE = "result_notice"     # 操作結果（音樂生成失敗等）
    RECALL_CONFIRM = "recall_confirm"   # 短確認（「好，不記了」）
    LONG_READ = "long_read"             # 朗讀長文（截斷過的）

    # ── 主動類：Marvin 自發，房間一忙就不硬講 ────────────────────────────────
    LEAVE_FAREWELL = "leave_farewell"       # 有人離開的道別（使用者定案：可略）
    PROACTIVE_TOPIC = "proactive_topic"     # 冷場找話題
    PROACTIVE_MANZAI = "proactive_manzai"   # 自發雙人吐槽
    PROACTIVE_MOCK = "proactive_mock"       # 延遲嘲諷
    MEMORY_CALLBACK = "memory_callback"     # 「你之前說要 X」
    NEWS = "news"                           # idle 報新聞
    SOCIAL_FILLER = "social_filler"         # 社交補位 / 頻率共鳴
    STANDUP = "standup"                     # 主動脫口秀（原本掛 protected，已降級）
    JOKE = "joke"                           # 主動講笑話（已降級）
    IMITATE = "imitate"                     # 模仿某人（已降級）


class Verdict(enum.Enum):
    PLAY = "play"                  # 立刻推 mixer，滿音量
    PLAY_OVER = "play_over"        # 立刻推，且 duck 音樂/背景（原 bypass_stream_mute）
    HOTSWAP = "hotswap"            # 串流中以短句注入
    DEFER = "defer"               # 呼叫端 await 空檔後重呼 decide()
    DROP_TO_TEXT = "drop_to_text"  # 不發聲，補到文字頻道
    DROP = "drop"                 # 靜默丟棄（仍寫 log）


class Decision(NamedTuple):
    verdict: Verdict
    reason: str   # 給 log 用的短標籤（committed / game / stream / hot_chat / interrupt / load / busy / gap）


@dataclass(frozen=True)
class RoomState:
    stream_mode: bool = False          # 正在放音樂 / 直播
    hot_chat: bool = False             # 房間內人類正熱烈對話
    last_interrupted: bool = False     # 上一句 TTS 被使用者打斷（且本句是續句）
    mixer_load_s: float = 0.0          # mixer TTS 佇列積壓秒數
    game_mode: bool = False            # 遊戲進行中
    user_speaking: bool | None = None  # None = 還沒等空檔；True/False = _wait_for_user_silence 結果

    def with_silence(self, silence_ok: bool) -> "RoomState":
        """套用 _wait_for_user_silence() 的結果（True = 有空檔）。"""
        return replace(self, user_speaking=not silence_ok)


# ── 每個 kind 的策略原型 ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class _Policy:
    committed: bool = False              # True → 一律 PLAY / PLAY_OVER
    on_stream: Verdict = Verdict.DROP    # stream_mode 時（committed 無視）
    on_hot_chat: Verdict = Verdict.DROP  # hot_chat 時
    drop_on_interrupt: bool = True       # 上一句被打斷 → 放棄本句
    respects_silence: bool = True        # 人還在講 → DEFER（逾時見 on_busy）
    on_busy: Verdict = Verdict.DROP      # DEFER 等不到空檔時
    load_limit_s: float = 8.0            # mixer 佇列超過 → DROP_TO_TEXT（inf = 不丟）


_COMMITTED = _Policy(committed=True)

_REPLY = _Policy(
    on_stream=Verdict.HOTSWAP, on_hot_chat=Verdict.PLAY,
    drop_on_interrupt=True, respects_silence=True, on_busy=Verdict.DROP_TO_TEXT,
    load_limit_s=8.0,
)
_REPLY_EPHEMERAL = _Policy(   # ack / 短確認：可有可無，不等空檔、不補文字
    on_stream=Verdict.HOTSWAP, on_hot_chat=Verdict.PLAY,
    drop_on_interrupt=True, respects_silence=True, on_busy=Verdict.DROP,
    load_limit_s=3.0,
)
_NOTICE = _Policy(            # 操作結果：講不成就補文字
    on_stream=Verdict.DROP_TO_TEXT, on_hot_chat=Verdict.DEFER,
    drop_on_interrupt=False, respects_silence=True, on_busy=Verdict.DROP_TO_TEXT,
    load_limit_s=8.0,
)
_PROACTIVE = _Policy(         # 自發：一忙就不講，不補文字
    on_stream=Verdict.DROP, on_hot_chat=Verdict.DROP,
    drop_on_interrupt=True, respects_silence=True, on_busy=Verdict.DROP,
    load_limit_s=3.0,
)
_PROACTIVE_PATIENT = _Policy(  # memory callback：熱聊時願意等
    on_stream=Verdict.DROP, on_hot_chat=Verdict.DEFER,
    drop_on_interrupt=True, respects_silence=True, on_busy=Verdict.DROP,
    load_limit_s=3.0,
)
_NEWS = _Policy(              # 報新聞：講不成補文字
    on_stream=Verdict.DROP, on_hot_chat=Verdict.DROP,
    drop_on_interrupt=True, respects_silence=True, on_busy=Verdict.DROP_TO_TEXT,
    load_limit_s=3.0,
)


POLICY: dict[SpeakKind, _Policy] = {
    SpeakKind.JOIN_GREETING: _COMMITTED,
    SpeakKind.SUMMON_INTRO: _COMMITTED,
    SpeakKind.GAME_HOST: _COMMITTED,
    SpeakKind.DJ_NARRATION: _COMMITTED,
    SpeakKind.SELF_SAY: _COMMITTED,
    SpeakKind.EXTERNAL_RELAY: _COMMITTED,
    SpeakKind.SYSTEM_ALERT: _COMMITTED,

    SpeakKind.WAKE_REPLY: _REPLY,
    SpeakKind.WAKE_ACK: _REPLY_EPHEMERAL,
    SpeakKind.RESULT_NOTICE: _NOTICE,
    SpeakKind.RECALL_CONFIRM: _REPLY_EPHEMERAL,
    SpeakKind.LONG_READ: replace(_REPLY, on_stream=Verdict.DROP_TO_TEXT, on_busy=Verdict.DROP_TO_TEXT),

    SpeakKind.LEAVE_FAREWELL: _PROACTIVE,
    SpeakKind.PROACTIVE_TOPIC: _PROACTIVE,
    SpeakKind.PROACTIVE_MANZAI: _PROACTIVE,
    SpeakKind.PROACTIVE_MOCK: _PROACTIVE,
    SpeakKind.MEMORY_CALLBACK: _PROACTIVE_PATIENT,
    SpeakKind.NEWS: _NEWS,
    SpeakKind.SOCIAL_FILLER: _PROACTIVE,
    SpeakKind.STANDUP: _PROACTIVE,
    SpeakKind.JOKE: _PROACTIVE,
    SpeakKind.IMITATE: _PROACTIVE,
}

# 窮舉守衛：漏一個 kind → import 就炸，不會 silent 走預設
_missing = set(SpeakKind) - set(POLICY)
if _missing:
    raise RuntimeError(f"tts_speak_policy: POLICY 漏了 {sorted(k.name for k in _missing)}")


def load_limit_s(kind: SpeakKind) -> float:
    """該 kind 的 mixer 佇列積壓上限（秒）。給呼叫端算 mixer_load 用。"""
    return POLICY[kind].load_limit_s


def is_committed(kind: SpeakKind) -> bool:
    """committed = 決定要說就一定說（join/leave 招呼、summon、遊戲主持、DJ 口白…）。"""
    return POLICY[kind].committed


def decide(kind: SpeakKind, room: RoomState) -> Decision:
    """依 kind + 房間狀態回一個 Decision(verdict, reason)。

    committed kind：一律 PLAY（stream 中 PLAY_OVER 以 duck 音樂）。
    其餘依序看 game_mode → stream → hot_chat → interrupt → load → silence。
    verdict==DEFER 表示呼叫端要先 await 空檔、用 room.with_silence(...) 重呼一次。
    """
    p = POLICY[kind]

    if p.committed:
        v = Verdict.PLAY_OVER if room.stream_mode else Verdict.PLAY
        return Decision(v, "committed")

    if room.game_mode:
        return Decision(Verdict.DROP, "game")

    if room.stream_mode and p.on_stream is not Verdict.PLAY:
        return Decision(p.on_stream, "stream")

    if room.hot_chat and p.on_hot_chat is not Verdict.PLAY:
        if p.on_hot_chat is not Verdict.DEFER:
            return Decision(p.on_hot_chat, "hot_chat")
        # on_hot_chat == DEFER：熱聊要等空檔
        if room.user_speaking is None:
            return Decision(Verdict.DEFER, "hot_chat")
        if room.user_speaking:
            return Decision(p.on_busy, "hot_chat_busy")
        # 有空檔 → 往下繼續判斷

    if p.drop_on_interrupt and room.last_interrupted:
        return Decision(Verdict.DROP, "interrupt")

    if p.load_limit_s != float("inf") and room.mixer_load_s > p.load_limit_s:
        return Decision(Verdict.DROP_TO_TEXT, "load")

    if p.respects_silence:
        if room.user_speaking is None:
            return Decision(Verdict.DEFER, "silence")
        if room.user_speaking:
            return Decision(p.on_busy, "busy")

    return Decision(Verdict.PLAY, "ok")
