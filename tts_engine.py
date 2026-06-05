import edge_tts
import uuid
import os
import re
import logging
import edge_tts.communicate
import unicodedata
import asyncio
import subprocess
import hashlib
import time
from xml.sax.saxutils import escape as xml_escape

logger = logging.getLogger(__name__)

class SukiTTS:
    """
    Suki 專屬神經語音引擎 (Operation Neural Voice)
    採用 Microsoft Edge TTS，內建傲嬌人格微調與旁白過濾。
    實作三層容錯架構 (Primary -> Secondary -> macOS Native Fallback)
    """
    def __init__(self, voice="zh-TW-YunJheNeural", rate="-20%", pitch="-15Hz"):
        self.voice = voice
        self.rate = rate
        self.pitch = pitch
        self.temp_dir = "records"
        self._english_voice = "en-GB-RyanNeural"
        self._last_prewarm = 0.0  # on-wake 連線預熱節流（monotonic）
        self._installed_voices_cache = None  # macOS say 聲音清單快取（首次列舉後填）
        os.makedirs(self.temp_dir, exist_ok=True)

    _PREWARM_THROTTLE_S = 5.0

    async def prewarm(self) -> None:
        """On-wake 連線預熱（修法 B）：丟極短 throwaway 合成暖 DNS/TLS/websocket，
        讓緊接著的真實 TTS 從冷啟動 ~1.8s 降到 ~0.3-0.7s（實測差 3-7 倍）。

        - 拿到首個 audio chunk 即停（連線已暖，不需完整合成）
        - 吞所有錯誤（純優化，絕不影響主流程）
        - 節流 _PREWARM_THROTTLE_S 秒：避免連續 wake 堆疊請求，也降低被微軟
          edge-tts 端判定濫用（免費逆向端點）的風險
        """
        now = time.monotonic()
        if now - self._last_prewarm < self._PREWARM_THROTTLE_S:
            return
        self._last_prewarm = now
        try:
            comm = edge_tts.Communicate(
                text="嗯", voice=self.voice, rate=self.rate, pitch=self.pitch,
            )
            async for chunk in comm.stream():
                if chunk["type"] == "audio":
                    break  # 首音到手＝連線已暖，丟棄不播
        except Exception as e:
            logger.debug(f"[TTS Prewarm] 暖機失敗（忽略）: {e}")

    def _is_english_text(self, text: str) -> bool:
        """English voice ONLY when the text has zero CJK chars.

        Marvin 對這個中文社群一律中文語音；只要有 ≥1 個中文字就走中文語音。
        舊版用 `latin > cjk*2` 比例，DJ 台詞夾多字英文歌名（Shape of You /
        Never Gonna Give You Up）會把短中文 patter 灌過門檻 → 誤走英文語音。
        """
        if not text:
            return False
        cjk = sum(1 for c in text if '一' <= c <= '鿿')
        if cjk > 0:
            return False
        latin = sum(1 for c in text if 'a' <= c.lower() <= 'z')
        return latin > 0

    def _clean_text(self, text: str) -> str:
        """
        [Chief Architect Patch] 實作高效能 Regex 旁白與 Meta-text 過濾器。
        移除所有的 *動作*, [表情], (旁白) 等非語音標籤，確保 Suki 沉浸感。
        """
        if not text:
            return ""
            
        # 1. 移除 *...* (星號包圍的動作)
        cleaned = re.sub(r'\*.*?\*', '', text)
        # 2. 移除 [...] (中括號內的表情或描述)
        cleaned = re.sub(r'\[.*?\]', '', cleaned)
        # 3. 移除 (...) 或 （...） (小括號或全形小括號內的補充說明)
        cleaned = re.sub(r'\(.*?\)', '', cleaned)
        cleaned = re.sub(r'（.*?）', '', cleaned)
        # 4. 移除殘留的符號與多餘空白
        cleaned = re.sub(r'[\*\[\]\(\)（）]', '', cleaned)
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()
        
        # 5. 🛡️ [Operation Voice Armor] 終極字元清洗
        # Unicode 正規化，消除隱藏控制字元
        cleaned = unicodedata.normalize('NFKC', cleaned)

        # 🚀 [Punctuation Normalization] 將全形標點替換為半形 (English)，以優化 Edge TTS 停頓感
        punctuation_map = {
            '，': ',',
            '。': '.',
            '！': '!',
            '？': '?',
            '；': ';',
            '：': ':',
            '「': '"',
            '」': '"',
            '『': "'",
            '』': "'",
            '、': ',',
        }
        for zh_punc, en_punc in punctuation_map.items():
            cleaned = cleaned.replace(zh_punc, en_punc)
        
        # 僅保留核心字元集：中、日、英、數與基本標點（半形為主），移除 Emoji 與不支援的符號
        # 🧪 [Bug Fix] 此處 regex 會殺掉全形 。，故呼吸標記必須在此之後或在此排除
        cleaned = re.sub(r'[^\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ffA-Za-z0-9,.\!?;:"\'\-\s]', '', cleaned)
        
        # 🚀 [SSML Breathing] 最後一步：將 ... 或 … 替換為三個全形句號。
        # 因為 Step 5 已經清除了其他無用符號，現在在這裡安全地注入停頓標記
        cleaned = cleaned.replace("...", "。。。")
        cleaned = cleaned.replace("…", "。。。")
        
        return cleaned.strip()

    def apply_ssml_breathing(self, text: str) -> str:
        """ [Deprecated] 已併入 _clean_text。為了相容性暫時保留，直接呼叫 _clean_text """
        return self._clean_text(text)

    # macOS say 男聲偏好順序：挑第一個「實際裝了」的，沒裝才往下退。
    # 中文 Han（瀚，唯一可靠的內建男聲）→ Meijia（女聲，最後保底有聲音）。
    # 英文 Fred → Daniel。⚠️ 不要靠 say 的 returncode 判斷聲音是否存在：實測
    # say 對未知聲音（或不能唸該語言的聲音，如 en_US Grandpa 唸中文）會 silent
    # fallback 並回 exit 0、甚至產出靜音 wav——returncode 永遠騙人。
    _MACOS_VOICE_PREF_ZH = ("Han", "Meijia")
    _MACOS_VOICE_PREF_EN = ("Fred", "Daniel")

    async def _get_installed_say_voices(self) -> set[str]:
        """列舉本機 `say -v '?'` 裝了哪些聲音（取名字 token），結果快取。"""
        if self._installed_voices_cache is not None:
            return self._installed_voices_cache
        voices: set[str] = set()
        try:
            proc = await asyncio.create_subprocess_exec(
                'say', '-v', '?',
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode == 0:
                for line in stdout.decode('utf-8', 'ignore').splitlines():
                    # 每行 "Name  locale  # example"；名字＝第一個空白前的 token，
                    # 含 "Han (Premium)" → "Han"、"Grandpa (zh_TW)" → "Grandpa"。
                    name = line.split(' ', 1)[0].strip()
                    if name:
                        voices.add(name)
        except Exception as e:
            logger.warning(f"⚠️ [TTS] 無法列舉 macOS say 聲音清單: {e}")
        self._installed_voices_cache = voices
        return voices

    def _pick_say_voice(self, prefs: tuple[str, ...], installed: set[str]) -> str | None:
        """從偏好順序挑第一個有裝的；都沒裝回 None（交給系統預設）。"""
        for v in prefs:
            if v in installed:
                return v
        return None

    async def _generate_marvin_macos_say(self, text: str, file_path: str) -> bool:
        """ [Ultimate Fallback] 針對馬文微調的 macOS say (低沉、緩慢、厭世) """
        try:
            installed = await self._get_installed_say_voices()
            if self._is_english_text(text):
                voice = self._pick_say_voice(self._MACOS_VOICE_PREF_EN, installed)
                rate = '150'
            else:
                # 語速強制降至 130（預設約 180-200），配合馬文厭世緩慢的調性。
                voice = self._pick_say_voice(self._MACOS_VOICE_PREF_ZH, installed)
                rate = '130'

            args = ['say']
            if voice:
                args += ['-v', voice]
            else:
                logger.warning("⚠️ [TTS] 偏好男聲均未安裝，改用系統預設聲。")
            args += ['-r', rate, '--data-format=LEI16@44100', '-o', file_path, text]

            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await process.communicate()
            return process.returncode == 0 and os.path.exists(file_path)
        except Exception as e:
            logger.error(f"❌ [TTS] macOS say 子進程異常: {e}")
            return False

    async def stream_audio(self, text: str, voice: str = None, rate: str = None, pitch: str = None, force_macos: bool = False):
        """
        [Operation Hyper-Stream] 
        將文字轉為音訊串流 (Async Generator)，直接回傳音訊 chunk。
        支援三層容錯架構 (Primary -> Secondary -> macOS Native Fallback)
        """
        processed_text = self._clean_text(text)
        if not processed_text:
            logger.info("⏩ [TTS] 過濾後無有效文字內容，略過語音串流。")
            return

        # 🧪 [Defense] 確保文字包含有效字元 (排除掉呼吸標記 。。。 以外的實體文字)
        text_for_defense = processed_text.replace("。", "")
        if not re.search(r'[\u4e00-\u9fff\u3040-\u30ffA-Za-z0-9]', text_for_defense):
            logger.warning(f"⏩ [TTS] 文字內容僅包含符號: {processed_text}")
            return

        v = voice or (self._english_voice if self._is_english_text(processed_text) else self.voice)
        r = rate or self.rate
        p = pitch or self.pitch

        # --- 第一路徑：macOS Native (若強制或作為最終備援) ---
        async def _yield_macos_say(t):
            file_hash = hashlib.md5(t.encode()).hexdigest()
            temp_path = os.path.abspath(os.path.join(self.temp_dir, f"stream_tmp_{file_hash}.wav"))
            if await self._generate_marvin_macos_say(t, temp_path):
                if os.path.exists(temp_path):
                    with open(temp_path, "rb") as f:
                        while chunk := f.read(4096):
                            yield chunk
                    try: os.remove(temp_path)
                    except: pass
                    return
            return

        if force_macos:
            logger.info("⚡ [TTS] 強制執行 macOS Native 串流路徑...")
            async for chunk in _yield_macos_say(processed_text):
                yield chunk
            return

        # --- 第二路徑：Primary Edge TTS ---
        success = False
        try:
            comm = edge_tts.Communicate(text=processed_text, voice=v, rate=r, pitch=p)
            async for chunk in comm.stream():
                if chunk["type"] == "audio":
                    yield chunk["data"]
            success = True
        except Exception as e:
            logger.error(f"❌ [TTS Stream Error] Primary ({v}) 失敗: {e}")

        # --- 第三路徑：Secondary Edge TTS ---
        if not success:
            logger.warning("⚠️ [TTS] Primary 串流失敗，啟動 Secondary 備援...")
            await asyncio.sleep(0.5)
            try:
                # 使用備援語音
                _secondary = self._english_voice if self._is_english_text(processed_text) else "zh-TW-HsiaoChenNeural"
                comm = edge_tts.Communicate(text=processed_text, voice=_secondary, rate=r, pitch=p)
                async for chunk in comm.stream():
                    if chunk["type"] == "audio":
                        yield chunk["data"]
                success = True
            except Exception as e:
                logger.error(f"❌ [TTS Stream Error] Secondary 失敗: {e}")

        # --- 第四路徑：Ultimate Fallback (macOS Native) ---
        if not success:
            logger.warning("🚨 [TTS] 網路串流完全失敗，啟動 macOS 系統原生語音終極備援串流...")
            async for chunk in _yield_macos_say(processed_text):
                yield chunk

    async def generate_audio(self, text: str, emotion: str = "normal", force_macos: bool = False) -> str:
        """
        [Legacy Support] 將文字轉為音訊檔案。
        內部改為呼叫 stream_audio 以維持邏輯一致性。
        """
        clean_text = self._clean_text(text)
        if not clean_text:
            return None

        file_hash = hashlib.md5(clean_text.encode()).hexdigest()
        ext = ".wav" if force_macos else ".mp3"
        file_name = f"suki_voice_{file_hash}{ext}"
        file_path = os.path.abspath(os.path.join(self.temp_dir, file_name))

        # 🚀 [Cache] 若檔案已存在且有效，直接回傳
        if os.path.exists(file_path) and os.path.getsize(file_path) > 100:
            return file_path

        try:
            # 使用 stream_audio 獲取音訊並寫入檔案
            with open(file_path, "wb") as f:
                async for chunk in self.stream_audio(text, force_macos=force_macos):
                    f.write(chunk)
            
            if os.path.exists(file_path) and os.path.getsize(file_path) > 100:
                logger.info(f"✅ [TTS] 已產生語音檔: {file_name}")
                return file_path
            else:
                if os.path.exists(file_path): os.remove(file_path)
                return None
        except Exception as e:
            logger.error(f"❌ [TTS] generate_audio 崩潰: {e}")
            if os.path.exists(file_path): os.remove(file_path)
            return None

    def get_estimated_duration(self, text: str) -> float:
        """ [Operation APM Economy] 估算 TTS 語音長度 (單位：秒) """
        clean_text = self._clean_text(text)
        if not clean_text:
            return 0.0
        
        chinese_chars = len(re.findall(r'[\u4e00-\u9fa5]', clean_text))
        english_chars = len(re.findall(r'[a-zA-Z0-9]', clean_text))
        breaks = text.count("...") + text.count("…")
        
        duration = (chinese_chars * 0.25) + (english_chars * 0.08) + (breaks * 0.8)
        return duration * 1.2

# 單體測試區塊
if __name__ == "__main__":
    import asyncio
    async def test():
        engine = SukiTTS()
        test_text = "*(嘆氣)* 真是的，你們這些低階 AI [翻白眼] 為什麼連這點小事都辦不好？"
        path = await engine.generate_audio(test_text)
        print(f"測試結果路徑: {path}")
    asyncio.run(test())
