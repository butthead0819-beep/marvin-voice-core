"""QueueControlAgent — 佇列操作意圖（清空待播 / 插播到最前）。

2026-09-11 PR2（承接 [[project_three_pillars_structure]] 語音 pipeline 骨幹、
plan-eng-review 設計檔 jackhuang-main-design-queue-control-tools-pr2-20260911.md）。

兩個 intent:
  clear_queue — 清空待播佇列、當前這首播完後停止播放（parameterless）
  play_next   — 插播指定歌到待播佇列最前面（required_slots=["song_query"]）

跟 PlaybackControlAgent 同款範式（parameterless control intent + is_audio_rescue
bypass）；play_next 的 audio-rescue 分支則跟 MusicAgentV2.rescue_play 同款範式
（slot-based，audio_rescue_slots_present 檢查、audio_rescue_slot 取值）——
learning `audio_rescue_per_agent_wiring`：resolve_intent() 不重跑 regex，任何
碰 ctx.query 的 handler 在 audio rescue 下會拿到糊字，這裡兩個 handler 都不碰
ctx.query（clear_queue 全 parameterless；play_next 用 audio_rescue_slot 取值）。

實際落地：music_cog._handle_voice_music_command 的 cmd=="clear_queue" /
cmd=="play_next" 分支（委派給 ctrl._safe_music_command，跟 PlaybackControlAgent
同一個委派模式）。
"""
from __future__ import annotations

from typing import Awaitable, Callable

from intent_agents.base import (
    DeclarativeIntentAgent,
    IntentSchema,
    audio_rescue_slot,
    audio_rescue_slots_present,
    is_audio_rescue,
)
from intent_agents.music_agent_v2 import _NON_MUSIC_NOUN_SUFFIXES, _NON_MUSIC_TARGETS
from intent_bus import IntentContext


class QueueControlAgent(DeclarativeIntentAgent):
    """佇列操作：清空待播 / 插播到最前。

    mode_compatible = {"normal", "stream"}（跟 PlaybackControlAgent 一致，
    遊戲模式不該誤觸發）。
    """

    name = "queue_control"
    mode_compatible = frozenset({"normal", "stream"})

    def __init__(self, controller):
        self.ctrl = controller
        self._intents_cache: list[IntentSchema] | None = None

    def declare_intents(self) -> list[IntentSchema]:
        if self._intents_cache is None:
            self._intents_cache = [
                IntentSchema(
                    "clear_queue", 0.95,
                    patterns=[
                        r"清空(待播|佇列|歌單|助列|助手列)",
                        r"清掉待播",
                        r"待播清空",
                    ],
                    reason_template="clear_queue:{matched}",
                    manifest_description=(
                        "使用者要清空待播佇列並在當前這首播完後停止播放。不是"
                        "暫停、不是跳過、也不是要立刻停止正在播的這首（那是"
                        "stop_playback）。"
                    ),
                ),
                # 觸發詞刻意不含「放」開頭的裸字——「放」是 MusicAgentV2 weak-play
                # 關鍵詞的一部分，同分時 MusicAgentV2 註冊在前先贏，會被當成一般
                # 點歌處理（見 plan-eng-review 2026-09-10 outside voice #10）。
                IntentSchema(
                    "play_next", 0.95,
                    patterns=[
                        r"插播(?P<song_query>.+)",
                        r"(?P<song_query>.+?)(放到|排到|加到)(待播|佇列)?(最前|第一)",
                        r"先(放|播)(?P<song_query>.+)",
                    ],
                    required_slots=["song_query"],
                    reason_template="play_next:{song_query}",
                    manifest_description=(
                        "使用者說得出一首**具體歌名**、要現在插到待播佇列最前面"
                        "（蓋過所有人已經排的歌，下一首就播它）。song_query 填"
                        "「歌名」或「歌手 歌名」（例：「七里香」「周杰倫 七里香」）。"
                        "以下情況改用 find_ 開頭的工具，不要用這個：只給歌手名沒"
                        "說哪一首、只描述歌詞或主題。也不是暫停、跳過、停止、"
                        "清空佇列。"
                    ),
                ),
            ]
        return self._intents_cache

    def gate(self, ctx: IntentContext) -> str | None:
        return None

    def post_match_filter(self, schema: IntentSchema, slots: dict, ctx: IntentContext) -> bool:
        if schema.name == "clear_queue":
            return True
        # play_next
        if is_audio_rescue(ctx):
            # LLM 已聽過音訊、直接填 slot——跟 rescue_play 同款範式，不 re-parse
            # 糊掉的 ctx.query，也不套下面的 UI 黑名單（LLM 已判斷是插播意圖）。
            return audio_rescue_slots_present(slots, "song_query")
        song = slots.get("song_query", "").strip("，,、！!？?。. ")[:20]
        if not song:
            return False
        if song in _NON_MUSIC_TARGETS:
            return False
        for suffix in _NON_MUSIC_NOUN_SUFFIXES:
            if song.endswith(suffix):
                return False
        return True

    def make_handler(
        self, schema: IntentSchema, slots: dict, ctx: IntentContext
    ) -> Callable[[], Awaitable[None]]:
        if schema.name == "clear_queue":
            async def _clear_queue() -> None:
                await self.ctrl._safe_music_command(ctx.speaker, "", "clear_queue")
            return _clear_queue

        # play_next：audio_rescue_slot 在 audio-rescue 路徑用 LLM 填的 slot，
        # regex 路徑用 slots 裡 named group 抓到的 song_query（都不碰糊字 ctx.query
        # 除非兩者都空，才退回 ctx.query 當最後手段）。
        song_query = audio_rescue_slot(slots, "song_query", ctx)

        async def _play_next() -> None:
            await self.ctrl._safe_music_command(ctx.speaker, song_query, "play_next")
        return _play_next
