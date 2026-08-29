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
import os


class MarvinTalkMixin:
    def _init_talk_manager(self) -> None:
        from marvin_talk import TalkSessionManager

        self.talk_manager = TalkSessionManager(
            # 免費 key：優先用專屬 MARVIN_TALK_API_KEY（獨立額度，不跟 cleaner 搶）；
            # 沒設就退回 router 共用的 GOOGLE_API_KEY（但那把常被 cleaner 打到 429）。
            # 免費全掛才退付費 key。
            free_client_provider=self._talk_free_client,
            paid_client_provider=lambda: getattr(
                getattr(self.bot, "router", None), "google_paid_client", None
            ),
            play_tts=self._talk_say,
            send_text=self._talk_send_text,
            pause_music=lambda: self._talk_set_music_paused(True),
            resume_music=lambda: self._talk_set_music_paused(False),
            persona_provider=self._talk_persona,
            is_voice_connected=lambda: self.voice_client is not None
            and self.voice_client.is_connected(),
            # VAD 切走你的話後立刻出個短音「嗯」——讓你知道聽到了、正在想（LLM+TTS 還要幾秒）
            heard_cue=lambda: self._play_ack("filler"),
        )

    def _talk_free_client(self):
        key = os.getenv("MARVIN_TALK_API_KEY")
        if key:
            cached = getattr(self, "_talk_dedicated_client", None)
            if cached is None:
                from google import genai
                cached = genai.Client(api_key=key)
                self._talk_dedicated_client = cached
            return cached
        return getattr(getattr(self.bot, "router", None), "google_client", None)

    def _talk_active(self) -> bool:
        """回合制對談進行中 → 主動發話 / 嘲諷 / 插話一律讓路（獨佔）。"""
        mgr = getattr(self, "talk_manager", None)
        return mgr is not None and mgr.active

    async def _talk_say(self, text: str) -> None:
        """對談回覆出聲。比照 marvin_say：
        - 對談中使用者本來就一直在講話 → _tts_interrupted 會被設 True，不清掉回覆會被
          [TTS Interrupt Guard] 整段跳過（11:40 實測 bug）。
        - play_tts 只讀 self._tts_protected、不讀 kwarg（[[p1_conflicts_clarification]] 死參數坑）
          → 必須手動拉旗標，否則回覆被 [TTS Silence Gate] 當非保護 TTS 丟掉。
        - force_macos：本機 say（Meijia，已調音），避 edge-tts 限流失聲 [[tts_edge_ratelimit_and_say_fallback]]
        - 對談回覆要滿音量：清掉 mixer 的 player-speech duck（使用者剛講完話會把它壓到
          10%，但回合制就是「你講完他答」，不該讓路）。
        """
        self._tts_interrupted = False
        mixer = getattr(self, "_mixer", None)
        if mixer is not None:
            mixer._player_speech_until = 0.0
            mixer._tts_player_duck_cur = 1.0
        _prev = self._tts_protected
        self._tts_protected = True
        try:
            await self.play_tts(
                text, already_in_channel=True, protected=True, force_macos=True
            )
        finally:
            self._tts_protected = _prev

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
        """對談期間壓住音樂。

        ⚠️ 不能用 _mixer.set_paused()——Plan12 mixer 是「音樂＋TTS」單一泵，
        整台暫停時 read() 直接回 silence，馬文的回覆 TTS 也被卡住，只在對談結束
        解暫停時才一次噴出（11:46 實測「聽不到、結束才有聲音、已講一半」的根因）。
        改成只把音樂層音量壓到 0（TTS 層走獨立 _tts_gain 不受影響）。
        代價：歌曲位置照跑，對談中會前進——短對談可接受。
        """
        mixer = getattr(self, "_mixer", None)
        if mixer is None:
            return
        if paused:
            self._talk_saved_music_vol = getattr(mixer, "_volume_target", 1.0)
            mixer.set_volume(0.0)
        else:
            mixer.set_volume(getattr(self, "_talk_saved_music_vol", 1.0))
