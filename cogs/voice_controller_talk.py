"""MarvinTalkMixin — /marvin_talk 回合制語音對談的 VoiceController 接線。

會話邏輯本體在 marvin_talk.py（TalkSession / TalkSessionManager）；這裡只負責
把 VoiceController 的相依（Gemini client、play_tts、音樂暫停/恢復、qa_persona）
包成注入用的 callable。以 mixin 併入讓 voice_controller.py 不因新功能增行
（見 tests/test_voice_controller_size_budget.py 棘輪）。

/marvin_talk 指令本身在 MarvinCommandsMixin（voice_controller_commands.py）。
獨佔 guard 在 discord_voice_engine.process_audio_slice。
"""
from __future__ import annotations

import asyncio


class MarvinTalkMixin:
    def _init_talk_manager(self) -> None:
        from marvin_talk import TalkSessionManager

        self.talk_manager = TalkSessionManager(
            google_client_provider=lambda: getattr(
                getattr(self.bot, "router", None), "google_client", None
            ),
            # force_macos：對談中改用本機 say（Meijia，已照馬文調音）。非為更快
            # （實測與 edge-tts 相當），是為穩定——避開 edge-tts 限流「整個沒聲音」
            # 的失效模式，延遲也一致（無冷呼叫尖峰）。見 2026-08-29 對話 / [[tts_edge_ratelimit_and_say_fallback]]
            play_tts=lambda t: self.play_tts(
                t, already_in_channel=True, protected=True, force_macos=True
            ),
            send_text=self._talk_send_text,
            pause_music=lambda: self._talk_set_music_paused(True),
            resume_music=lambda: self._talk_set_music_paused(False),
            persona_provider=self._talk_persona,
        )

    def _talk_send_text(self, message: str):
        if self.active_text_channel is not None:
            return self.active_text_channel.send(message)
        return asyncio.sleep(0)

    def _talk_persona(self) -> str:
        try:
            from marvin_prompts import PromptManager

            return PromptManager().get_instruction("qa_persona")
        except Exception:
            return "你是馬文，一個厭世但博學的機器人。用中文口語回答。"

    def _talk_set_music_paused(self, paused: bool) -> None:
        if getattr(self, "_mixer", None) is not None:
            self._mixer.set_paused(paused)
        mc = self.bot.cogs.get("MusicCog")
        if mc is None:
            return
        if paused:
            if getattr(mc, "stream_mode", False):
                mc.stream_paused = True
            if getattr(mc, "radio_mode", False):
                mc.radio_paused = True
        else:
            mc.stream_paused = False
            mc.radio_paused = False
