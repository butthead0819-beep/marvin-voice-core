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

    async def _generate_marvin_macos_say(self, text: str, file_path: str) -> bool:
        """ [Ultimate Fallback] 針對馬文微調的 macOS say (低沉、緩慢、厭世) """
        try:
            # 英文文字改用 Alex（macOS 內建英文男聲）
            if self._is_english_text(text):
                process_en = await asyncio.create_subprocess_exec(
                    'say', '-v', 'Alex', '-r', '150', '--data-format=LEI16@44100', '-o', file_path, text,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
                await process_en.communicate()
                return process_en.returncode == 0 and os.path.exists(file_path)

            # 優先嘗試使用 Liao，並將語速強制降至 130 (預設約為 180-200)
            target_voice = 'Liao'
            words_per_minute = '130' 
            
            process = await asyncio.create_subprocess_exec(
                'say', '-v', target_voice, '-r', words_per_minute, 
                '--data-format=LEI16@44100', '-o', file_path, text,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            
            # 系統防呆：若 Liao 未安裝，say 會回傳非 0 錯誤碼。此時降級回安全的 Meijia。
            if process.returncode != 0:
                logger.warning(f"⚠️ [TTS] 找不到男聲 {target_voice}，降級使用安全備援 Meijia。")
                process_fallback = await asyncio.create_subprocess_exec(
                    'say', '-v', 'Han', '-o', file_path, text,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                await process_fallback.communicate()
                return process_fallback.returncode == 0 and os.path.exists(file_path)
            
            return True
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
