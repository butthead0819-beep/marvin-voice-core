"""FindSongAgent — 以「找 + 音樂錨點」識別一首歌（Template A，宣告式）。

與 MusicAgentV2 分工：MusicAgentV2 動詞「播/放」→ 播放；本 agent 動詞「找」→ 識別歌名
後交給播放路徑（_handle_find_song → _safe_music_command play）。MusicAgentV2 刻意排除
歌詞/歌手識別（music_agent_v2.py 註解），本 agent 補上。

四模式（schema 順序 = 優先序，最具體錨點先）：
  find_lyrics (0.90)  找…歌詞…X        — 由歌詞識別
  find_theme  (0.85)  找…在講/關於 X…的歌 — 由主題識別
  find_album  (0.85)  找…X…專輯         — 由專輯
  find_artist (0.80)  找…X 的歌          — 由歌手（與 curation 接近，conf 最低）

誤觸防線：所有 pattern 都要求「找」+ 音樂錨點（歌/歌詞/專輯/在講）。STT log 的 367 個
對話「找」（找東西/找你/找工會）皆無錨點 → 不出價。
"""
from __future__ import annotations

import re

from intent_agents.base import DeclarativeIntentAgent, IntentSchema


# schema.name → 要傳給 controller 的 payload slot 名
_SLOT_BY_INTENT = {
    "find_lyrics": "lyrics",
    "find_theme": "theme",
    "find_album": "album",
    "find_artist": "artist",
}


# VAD 切尾常吸進尾巴語助詞或追問，要剝乾淨 — 否則 grounded search 搜
# 「天青色等煙雨啊」會 miss（mojim/genius 沒這條）。
_TRAILING_QUESTION_SUFFIXES = (
    "對不對", "是不是", "好聽嗎", "好不好", "對吧", "是吧",
)
_TRAILING_PARTICLES_RE = re.compile(r"[啊吧嗎呢喔欸耶哦呀阿喲]+$")


def _strip_trailing_particles(text: str) -> str:
    """剝末尾語助詞 + 常見追問尾。中段的助詞字不動（「啊不要走」→ 不變）。"""
    s = (text or "").strip()
    # 先剝整段追問（先長後短，避免短尾誤吃）
    for suffix in _TRAILING_QUESTION_SUFFIXES:
        if s.endswith(suffix):
            s = s[: -len(suffix)].rstrip()
            break
    # 再剝末尾連續助詞
    s = _TRAILING_PARTICLES_RE.sub("", s).rstrip()
    return s

# 各模式的識別 prompt 模板（{p} = payload）。LLM 只輸出「藝人 - 歌名」。
_PROMPT_BY_INTENT = {
    "find_lyrics": "以下歌詞出自哪一首歌？只回答一行「藝人 - 歌名」，不要解釋。\n歌詞：{p}",
    "find_theme": "推薦一首主題是「{p}」的知名華語歌曲。只回答一行「藝人 - 歌名」，不要解釋。",
    "find_album": "專輯「{p}」裡挑一首代表曲。只回答一行「藝人 - 歌名」，不要解釋。",
    "find_artist": "歌手「{p}」的一首代表作。只回答一行「藝人 - 歌名」，不要解釋。",
}


def find_song_prompt(mode: str, payload: str) -> str | None:
    """依模式 + payload 產生識別 prompt；mode 未知或 payload 空 → None。"""
    if not payload or not payload.strip():
        return None
    tmpl = _PROMPT_BY_INTENT.get(mode)
    return tmpl.format(p=payload.strip()) if tmpl else None


class FindSongAgent(DeclarativeIntentAgent):
    name = "find_song"
    mode_compatible = frozenset({"normal", "stream"})  # 遊戲模式不該誤觸發

    def __init__(self, controller):
        self.ctrl = controller
        self._intents_cache: list[IntentSchema] | None = None

    def declare_intents(self) -> list[IntentSchema]:
        if self._intents_cache is None:
            self._intents_cache = [
                IntentSchema(
                    "find_lyrics", 0.90,
                    patterns=[r"找.*?歌詞[有是裡含寫到]*(?P<lyrics>.+?)(?:的歌曲|的歌|$)"],
                    required_slots=["lyrics"],
                    reason_template="find_lyrics:{lyrics}",
                ),
                IntentSchema(
                    "find_theme", 0.85,
                    patterns=[r"找.*?(?:在講|關於|描述|講述)(?P<theme>.+?)的(?:歌曲|歌)"],
                    required_slots=["theme"],
                    reason_template="find_theme:{theme}",
                ),
                IntentSchema(
                    "find_album", 0.85,
                    patterns=[r"找.*?(?P<album>\S+?)(?:這張|那張)?專輯"],
                    required_slots=["album"],
                    reason_template="find_album:{album}",
                ),
                IntentSchema(
                    "find_artist", 0.80,
                    patterns=[r"找.*?(?P<artist>\S+?)的(?:歌曲|歌)"],
                    required_slots=["artist"],
                    reason_template="find_artist:{artist}",
                ),
            ]
        return self._intents_cache

    def make_handler(self, schema, slots, ctx):
        raw = slots.get(_SLOT_BY_INTENT.get(schema.name, ""), "").strip()
        payload = _strip_trailing_particles(raw) if schema.name == "find_lyrics" else raw
        speaker = ctx.speaker

        async def _handler():
            await self.ctrl._handle_find_song(schema.name, payload, speaker)

        return _handler
