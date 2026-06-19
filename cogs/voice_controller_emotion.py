"""
EmotionMoodMixin — VoiceController 的情緒分類 / 心情貼圖 / 噪音提醒。

從 voice_controller.py 抽出（減肥），以 mixin 併入 VoiceController。self 仍是
VoiceController 實例，bot.router / user_emotion_cache / marvin_self_emotion /
active_text_channel / bot.sticker_manager 等沿用原本 self 存取，行為零改動。

  - _update_emotion_from_audio  : Gemini 音訊情緒 → user_emotion_cache
  - _classify_marvin_self_emotion: Groq 對 Marvin 自身回應做情緒分類（背景）
  - _classify_emotion           : 純函式，韻律 metadata → 情緒標籤
  - _send_noise_nudge           : 噪音擋喚醒時的文字提醒
  - _send_mood_sticker          : 依心情選 Clyde 貼圖
"""
from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger(__name__)


class EmotionMoodMixin:
    async def _update_emotion_from_audio(self, speaker: str, wav_bytes: bytes, text: str):
        """🎭 [Gemini Audio Emotion] 以實際語音音訊讓 Gemini 分析情緒，更新 user_emotion_cache。
        背景任務，失敗時靜默使用韻律情緒作為 fallback。"""
        try:
            from google.genai import types
            audio_part = types.Part.from_bytes(data=bytes(wav_bytes), mime_type="audio/wav")
            response = await asyncio.wait_for(
                self.bot.router.google_client.aio.models.generate_content(
                    model="gemini-2.0-flash-lite",
                    contents=[
                        audio_part,
                        f'說話者說：「{text}」。只輸出一個英文情緒詞：excited / frustrated / amused / sarcastic / neutral / sad / angry'
                    ],
                    config={"max_output_tokens": 5, "temperature": 0.0}
                ),
                timeout=3.0
            )
            if response and response.text:
                emotion = response.text.strip().lower().split()[0]
                if emotion in {"excited", "frustrated", "amused", "sarcastic", "neutral", "sad", "angry"}:
                    prev = self.user_emotion_cache.get(speaker, "neutral")
                    self.user_emotion_cache[speaker] = emotion
                    logger.info(f"🎭 [Audio Emotion] {speaker}: {prev} → {emotion} (Gemini)")
        except asyncio.TimeoutError:
            logger.debug(f"⏱️ [Audio Emotion] {speaker} 逾時，保留韻律情緒標籤。")
        except Exception as e:
            logger.debug(f"⚠️ [Audio Emotion] {speaker} 分析失敗: {e}")

    async def _classify_marvin_self_emotion(self, speaker: str, full_text: str):
        """🎭 [Approach B] 在背景對 Marvin 自己的回應文字做情緒分類，結果存入 marvin_self_emotion[speaker]。
        不阻塞 TTS 播放；失敗時靜默保留原值。"""
        _t0 = time.monotonic()
        try:
            groq = getattr(self.bot.router, 'groq_dedicated_client', None)
            model = getattr(self.bot.router, 'groq_simple_model', None)
            if not groq or not model:
                return
            prompt = (
                "只輸出一個英文情緒詞：frustrated / amused / sarcastic / sad / angry / neutral\n"
                + full_text[:300]
            )
            resp = await asyncio.wait_for(
                groq.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=5,
                    temperature=0.0,
                ),
                timeout=5.0,
            )
            _words = resp.choices[0].message.content.strip().lower().split()
            word = _words[0] if _words else ""
            _VALID = {"frustrated", "amused", "sarcastic", "sad", "angry", "neutral"}
            if word in _VALID:
                self.marvin_self_emotion[speaker] = word
                elapsed = (time.monotonic() - _t0) * 1000
                logger.info(f"🎭 [Approach B] {speaker}: Marvin self-emotion={word} ({elapsed:.0f}ms)")
        except asyncio.TimeoutError:
            logger.warning(f"⏱️ [Approach B] {speaker} 情緒分類逾時，跳過。")
        except Exception as e:
            logger.warning(f"⚠️ [Approach B] {speaker} 情緒分類失敗: {e}")

    def _classify_emotion(self, prosody_data: dict) -> str:
        """
        🎭 [Operation Emotion Inference]
        根據韻律元數據推測說話者的情緒狀態。
        輸入：prosody_data dict（來自 VoiceMetaAnalyzer.calculate_prosody）
        回傳：單一情緒標籤字串
          - excited   : 高語速 + 高音量起伏 → 興奮/激動
          - impatient : 高語速 + 低音量起伏 → 急躁/緊張
          - depressed : 低語速 + 低音量起伏 → 沮喪/疲憊
          - hesitant  : 低語速 + 高音量起伏 → 猶豫/掙扎
          - robotic   : 正常語速 + 極低起伏 → 機械感（同類共鳴）
          - neutral   : 其他情況
        """
        if not prosody_data:
            return "neutral"

        wps = prosody_data.get("wps", 0)
        variance = prosody_data.get("energy_variance", 0)
        duration = prosody_data.get("physical_duration", 0)
        char_count = prosody_data.get("char_count", 0)

        # 防止語音過短造成的雜訊（少於 0.8s 或少於 3 個字）
        if duration < 0.8 or char_count < 3:
            return "neutral"

        # 情緒推測優先順序（越具體的判斷越優先）
        if wps > 6.0 and variance > 50:
            return "excited"           # 快 + 起伏大 = 興奮/激動
        elif wps > 6.0:
            return "impatient"         # 快 + 平穩 = 急躁/緊張
        elif wps < 1.5 and variance < 30:
            return "depressed"         # 慢 + 平穩 = 沮喪/疲憊
        elif wps < 1.5:
            return "hesitant"          # 慢 + 起伏 = 猶豫/掙扎
        elif 0 < variance < 20:
            return "robotic"           # 正常速度 + 極平穩 = 機械共鳴
        else:
            return "neutral"

    async def _send_noise_nudge(self, speaker: str) -> None:
        """🔇 [Noise Nudge] 環境噪音害喚醒被擋 → 文字頻道一句溫和提醒（每 speaker 每 session 一次）。"""
        if not self.active_text_channel:
            return
        try:
            await self.active_text_channel.send(
                f"🔇 {speaker} 我有聽到你在叫我，但你那邊背景有點吵聽不太清楚 😅 "
                f"開一下 Discord 的 Krisp 噪音抑制（設定 → 語音與視訊 → 噪音抑制），"
                f"或 Apple 裝置的人聲隔離，會清楚很多。"
            )
        except Exception as e:
            logger.debug(f"[Noise Nudge] 發送失敗：{e}")

    async def _send_mood_sticker(self, response_text: str, speaker: str = "", context: str = "") -> None:
        """🎭 [Sticker] 依 Marvin 心情選一張 Clyde 貼圖發送至 active_text_channel。"""
        if not self.active_text_channel:
            return
        if not hasattr(self.bot, "sticker_manager"):
            return
        from sticker_manager import infer_mood
        if context == "greeting":
            mood = "greeting"
        elif context == "farewell":
            mood = "farewell"
        else:
            toxicity = self.bot.router.dna.get("toxicity", 5)
            user_emotion = self.user_emotion_cache.get(speaker, "neutral")
            mood = infer_mood(response_text, toxicity, user_emotion)
        await self.bot.sticker_manager.send(self.active_text_channel, mood)
