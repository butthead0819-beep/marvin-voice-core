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
import io
import wave
import aiohttp
from array import array
from xml.sax.saxutils import escape as xml_escape

logger = logging.getLogger(__name__)

_EDGE_BREAKER_S = 3600.0
_EDGE_VOICE_BREAKER_S = 3600.0
_AZURE_BREAKER_S = 600.0
_AZURE_TIMEOUT_S = 15.0

_AZURE_SPLIT_MIN = 8
_AZURE_SPLIT_MAX = 30
_STRONG_BREAKS = "。！？!?；;\n"
_WEAK_BREAKS = "，,、：:"


def _split_first_chunk(text: str) -> tuple[str, str]:
    """Azure F0 幾乎整段合成完才回首 byte → 先合成短首段（~1s 出聲），其餘一段在首段播放時合成。
    回 (first, rest)；不切時 rest == ""。只切兩段（F0 每 60s 20 次請求上限，別切碎）。"""
    if len(text) <= _AZURE_SPLIT_MAX:
        return text, ""

    cut = None
    for i in range(_AZURE_SPLIT_MIN - 1, _AZURE_SPLIT_MAX):
        if i < len(text) and text[i] in _STRONG_BREAKS:
            cut = i + 1
            break
    if cut is None:
        for i in range(_AZURE_SPLIT_MIN - 1, _AZURE_SPLIT_MAX):
            if i < len(text) and text[i] in _WEAK_BREAKS:
                cut = i + 1
                break
    if cut is None:
        return text, ""

    rest = text[cut:].strip()
    if not re.search(r'[一-鿿぀-ヿA-Za-z0-9]', rest):
        return text, ""

    return text[:cut], rest


def _is_edge_blocked(e: BaseException) -> bool:
    """微軟拒絕連線（401/403）＝驗證機制變了，短時間內重試無用。"""
    return isinstance(e, aiohttp.WSServerHandshakeError) and e.status in (401, 403)

# 🎭 [Emotion] edge-tts 沒有 SSML style/express-as 可用（Communicate 只吃
# rate/pitch/volume；zh-TW-YunJheNeural 在 Azure 語音清單裡也沒有 StyleList），
# 真情緒風格這條路是死路。這裡改用「有效的旋鈕」：依情緒微調 rate/pitch，疊加在
# base self.rate/self.pitch 上——不是真的表情演出，只是語速/音高的輕微起伏。
# (rate_delta_pct, pitch_delta_hz)
_EMOTION_ADJUST: dict[str, tuple[int, int]] = {
    "upbeat": (5, 10),   # 生活/興趣類話題：稍快、稍高，聽起來更有精神
    "calm": (-5, -8),    # 氛圍/情緒沉澱類：稍慢、稍低，貼氣氛
    "normal": (0, 0),
}

_RATE_RE = re.compile(r"^([+-]?\d+)%$")
_PITCH_RE = re.compile(r"^([+-]?\d+)Hz$")


def _apply_rate_delta(base_rate: str, delta_pct: int) -> str:
    """base_rate（如 "-20%"）疊加 delta_pct → 新的 rate 字串。解析不出 base 時當 0%。"""
    if not delta_pct:
        return base_rate
    m = _RATE_RE.match(base_rate)
    base = int(m.group(1)) if m else 0
    return f"{base + delta_pct:+d}%"


def _apply_pitch_delta(base_pitch: str, delta_hz: int) -> str:
    """base_pitch（如 "-15Hz"）疊加 delta_hz → 新的 pitch 字串。解析不出 base 時當 0Hz。"""
    if not delta_hz:
        return base_pitch
    m = _PITCH_RE.match(base_pitch)
    base = int(m.group(1)) if m else 0
    return f"{base + delta_hz:+d}Hz"


def peak_normalize_wav_bytes(wav_bytes: bytes, target_peak_ratio: float = 0.9) -> bytes:
    """把 mono/stereo 16-bit WAV 峰值正規化到 target_peak_ratio×滿幅。

    用途：macOS say 終極備援的輸出振幅天生比 edge-tts 低，疊上 mixer
    tts_gain=0.5 後蓋在音樂下偏小聲（2026-06-14 限流 incident）。拉到接近
    滿幅補償後段 gain，讓退備援時仍清楚可聽。

    Fail-open：非 16-bit / 壞 WAV / 全靜音一律原樣回傳，絕不丟例外中斷 TTS。
    """
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as r:
            nchannels = r.getnchannels()
            sampwidth = r.getsampwidth()
            framerate = r.getframerate()
            frames = r.readframes(r.getnframes())
        if sampwidth != 2:          # 只處理 16-bit PCM
            return wav_bytes
        samples = array("h")        # signed short；macOS-only 備援，固定 LE
        samples.frombytes(frames)
        peak = max((abs(s) for s in samples), default=0)
        if peak == 0:               # 全靜音 → 不可除零
            return wav_bytes
        scale = (target_peak_ratio * 32767.0) / peak
        out = array("h", (max(-32768, min(32767, int(s * scale))) for s in samples))
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(nchannels)
            w.setsampwidth(sampwidth)
            w.setframerate(framerate)
            w.writeframes(out.tobytes())
        return buf.getvalue()
    except Exception:
        return wav_bytes

# 防「一路講到底」：LLM 常寫出 15 字以上沒標點的長串（尤其夾歌名《》時），edge 會一口氣念完。
# 只在高把握的位置補逗號（歌名《》之後、強連接詞之前），找不到就不動——寧可不補也不要切錯詞。
# 在 2,676 句實際口白上試過：「」『』後補逗號常切錯（『兒童安全座椅』，抱怨聲），所以只認 》。
_BREATH_MAX_RUN = 14
# get_estimated_duration 每個句中長停頓（。！？ 實測 ≈1.5s）的估算秒數，外層還會 ×1.2
_LONG_PAUSE_EST_S = 1.25
_BREATH_SPLIT_RE = re.compile(r"([，,。.！!？?；;：:、…⋯—―─～~\s]+)")
_BREATH_STRIP_RE = re.compile(r"[《》「」『』【】\"'()（）*]")
_BREATH_TITLE_END_RE = re.compile(r"》(?=[^的之了嗎吧呢啊》」』，,]{3,})")
_BREATH_CONJ = ("但是", "可是", "不過", "所以", "因為", "然後", "而且", "結果", "如果", "甚至", "不然", "只好")


def _breath_comma_one(seg: str) -> str:
    for m in _BREATH_TITLE_END_RE.finditer(seg):
        if m.start() >= 4:
            return seg[:m.end()] + "，" + seg[m.end():]
    for conj in _BREATH_CONJ:
        i = seg.find(conj)
        if 5 <= i <= len(seg) - 4:
            return seg[:i] + "，" + seg[i:]
    return seg


def _insert_breath_commas(text: str) -> str:
    parts = _BREATH_SPLIT_RE.split(text)
    for i in range(0, len(parts), 2):  # 偶數 index 是片段，奇數是分隔標點
        if len(_BREATH_STRIP_RE.sub("", parts[i])) > _BREATH_MAX_RUN:
            parts[i] = _breath_comma_one(parts[i])
    return "".join(parts)


class SukiTTS:
    """
    Suki 專屬神經語音引擎 (Operation Neural Voice)
    採用 Microsoft Edge TTS，內建傲嬌人格微調與旁白過濾。
    實作四層容錯架構 (Primary -> Secondary -> Azure -> macOS Native Fallback)
    """
    def __init__(self, voice="zh-TW-YunJheNeural", rate="-20%", pitch="-15Hz"):
        self.voice = voice
        self.rate = rate
        self.pitch = pitch
        self.temp_dir = "records"
        self._english_voice = "en-GB-RyanNeural"
        self._last_prewarm = 0.0  # on-wake 連線預熱節流（monotonic）
        self._installed_voices_cache = None  # macOS say 聲音清單快取（首次列舉後填）
        self._edge_down_until = 0.0  # edge-tts 斷路器解除時間（monotonic）
        self._edge_voice_down: dict[str, float] = {}  # 單一聲音在 edge 無音訊（NoAudioReceived）→ 該聲音斷路，不影響其他聲音
        self._azure_down_until = 0.0  # Azure 斷路器解除時間（monotonic）
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
        if not self._edge_usable(self.voice):
            return
        try:
            comm = edge_tts.Communicate(
                text="嗯", voice=self.voice, rate=self.rate, pitch=self.pitch,
            )
            async for chunk in comm.stream():
                if chunk["type"] == "audio":
                    break  # 首音到手＝連線已暖，丟棄不播
        except Exception as e:
            logger.debug(f"[TTS Prewarm] 暖機失敗（忽略）: {e}")

    def _edge_usable(self, voice: str) -> bool:
        now = time.monotonic()
        return now >= self._edge_down_until and now >= self._edge_voice_down.get(voice, 0.0)

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

        # 4.5 停頓標點保留：白名單 regex 會刪掉 ⋯ / 破折號 / 波浪號，先換成會產生停頓的等價標點
        cleaned = cleaned.replace("⋯", "…")              # U+22EF → U+2026，之後 NFKC 成 ... 再收成單一 。（見函式尾）
        cleaned = re.sub(r"[—―─]+", "，", cleaned)        # 破折號（U+2014/U+2015/U+2500，連續視為一個）→ 逗號
        cleaned = re.sub(r"[～~]+", "，", cleaned)         # 波浪號（全形/半形，連續視為一個）→ 逗號

        # 4.6 防一路講到底：超過 14 字沒標點的片段，在安全位置補一個逗號（見 _insert_breath_commas）
        cleaned = _insert_breath_commas(cleaned)

        # 5. 🛡️ [Operation Voice Armor] 終極字元清洗
        # Unicode 正規化，消除隱藏控制字元
        cleaned = unicodedata.normalize('NFKC', cleaned)

        # 🚀 [Punctuation Normalization] 短停頓標點轉半形（全形半形停頓一樣 ≈0.4s）。
        # 。！？ 刻意不轉：轉半形會把 ≈1.5s 句間停頓砍成 ≈0.4s（量測見 docs/tts_breath_plan.md）
        punctuation_map = {
            '，': ',',
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
        
        # 僅保留核心字元集：中、日、英、數與基本標點（含長停頓用的全形 。！？），移除 Emoji 與不支援的符號
        cleaned = re.sub(r'[^\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ffA-Za-z0-9,.\!?;:"\'\-\s。！？]', '', cleaned)

        # 🚀 [停頓] edge 只有兩檔停頓：全形 。！？ ≈1.5s、其餘標點 ≈0.4s（量測見 docs/tts_breath_plan.md）
        # 刪節號 → 單一 。（停頓上限 1.5s，避免聽眾以為講完了）；半形 !? → 全形（NFKC 會把全形轉半形）
        cleaned = re.sub(r"\.{2,}", "。", cleaned)
        cleaned = cleaned.replace("!", "！").replace("?", "？")
        # 連續長停頓只留第一個（「？！」「。。」「。,」→ 一個），上限同樣 1.5s
        cleaned = re.sub(r"([。！？])[。！？,\s]*", r"\1", cleaned)

        # 開頭的長停頓沒意義（「…好吧」→「。好吧」），去掉
        return cleaned.strip().lstrip("。！？,").strip()

    def apply_ssml_breathing(self, text: str) -> str:
        """ [Deprecated] 已併入 _clean_text。為了相容性暫時保留，直接呼叫 _clean_text """
        return self._clean_text(text)

    # macOS say 男聲偏好順序：挑第一個「實際裝了」的，沒裝才往下退。
    # 中文 Han（瀚，唯一可靠的內建男聲）→ Meijia（女聲，最後保底有聲音）。
    # 英文 Fred → Daniel。⚠️ 不要靠 say 的 returncode 判斷聲音是否存在：實測
    # say 對未知聲音（或不能唸該語言的聲音，如 en_US Grandpa 唸中文）會 silent
    # fallback 並回 exit 0、甚至產出靜音 wav——returncode 永遠騙人。
    _MACOS_VOICE_PREF_ZH = ("Meijia", "Han")  # Meijia=zh_TW 台灣腔；Han=zh_CN 大陸腔（避免）
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
            ok = process.returncode == 0 and os.path.exists(file_path)
            if ok:
                # 峰值正規化補償 say 天生小聲 + mixer tts_gain=0.5（fail-open）。
                try:
                    with open(file_path, "rb") as f:
                        boosted = peak_normalize_wav_bytes(f.read())
                    with open(file_path, "wb") as f:
                        f.write(boosted)
                except Exception:
                    pass
            return ok
        except Exception as e:
            logger.error(f"❌ [TTS] macOS say 子進程異常: {e}")
            return False

    async def _stream_azure(self, text: str, voice: str, rate: str, pitch: str, volume: str = None):
        """Azure Speech REST 備援（F0 免費層）。失敗一律 fail-open，不丟例外。"""
        key = os.getenv("AZURE_SPEECH_KEY", "").strip()
        region = os.getenv("AZURE_SPEECH_REGION", "").strip()
        if not key or not region:
            return
        if time.monotonic() < self._azure_down_until:
            return

        lang = "-".join(voice.split("-")[:2])
        vol_attr = f' volume="{volume}"' if volume else ""
        ssml = (
            '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
            f'xml:lang="{lang}"><voice name="{voice}">'
            f'<prosody rate="{rate}" pitch="{pitch}"{vol_attr}>{xml_escape(text)}</prosody>'
            '</voice></speak>'
        )
        url = f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1"
        headers = {
            "Ocp-Apim-Subscription-Key": key,
            "Content-Type": "application/ssml+xml",
            "X-Microsoft-OutputFormat": "audio-24khz-48kbitrate-mono-mp3",
            "User-Agent": "marvin-voice-bot",
        }
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=_AZURE_TIMEOUT_S)) as session:
                async with session.post(url, headers=headers, data=ssml) as resp:
                    status = resp.status
                    body = await resp.read()
        except Exception as e:
            logger.warning(f"⚠️ [TTS] Azure 備援請求異常: {e}")
            return

        if status in (401, 403, 429):
            self._azure_down_until = time.monotonic() + _AZURE_BREAKER_S
            logger.warning(f"⚠️ [TTS] Azure 回 {status}（額度/金鑰），{int(_AZURE_BREAKER_S/60)} 分鐘內跳過 Azure")
            return

        if status != 200 or not body:
            logger.warning(f"⚠️ [TTS] Azure 備援失敗 status={status} bytes={len(body)}")
            return

        logger.info(f"🔷 [TTS] Azure 備援 ok chars={len(text)} bytes={len(body)}")
        for i in range(0, len(body), 4096):
            yield body[i:i + 4096]

    async def stream_audio(self, text: str, voice: str = None, rate: str = None, pitch: str = None, volume: str = None, force_macos: bool = False):
        """
        [Operation Hyper-Stream]
        將文字轉為音訊串流 (Async Generator)，直接回傳音訊 chunk。
        支援四層容錯架構 (Primary -> Secondary -> Azure -> macOS Native Fallback)
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
        _vol_kw = {"volume": volume} if volume is not None else {}
        success = False
        if self._edge_usable(v):
            try:
                comm = edge_tts.Communicate(text=processed_text, voice=v, rate=r, pitch=p, **_vol_kw)
                _audio_chunks = 0
                async for chunk in comm.stream():
                    if chunk["type"] == "audio":
                        yield chunk["data"]
                        _audio_chunks += 1
                if _audio_chunks == 0:
                    # edge-tts 完成但未回傳任何 audio — 視為靜默失敗，走 fallback
                    logger.warning(f"⚠️ [TTS Stream] Primary ({v}) 零 audio chunk，啟動 Secondary 備援...")
                else:
                    success = True
            except Exception as e:
                logger.error(f"❌ [TTS Stream Error] Primary ({v}) 失敗: {e}")
                if _is_edge_blocked(e):
                    self._edge_down_until = time.monotonic() + _EDGE_BREAKER_S
                    logger.warning(f"🔌 [TTS] edge-tts 被微軟拒絕（{e.status}），{int(_EDGE_BREAKER_S/60)} 分鐘內跳過 edge 直接走備援")
                elif isinstance(e, edge_tts.exceptions.NoAudioReceived):
                    self._edge_voice_down[v] = time.monotonic() + _EDGE_VOICE_BREAKER_S
                    logger.warning(f"🔌 [TTS] edge 對 {v} 無音訊，{int(_EDGE_VOICE_BREAKER_S/60)} 分鐘內此聲音直接走備援")

        # --- 第三路徑：Secondary Edge TTS ---
        _secondary = self._english_voice if self._is_english_text(processed_text) else self.voice
        if not success and self._edge_usable(_secondary):
            await asyncio.sleep(0.5)
            try:
                # 使用備援語音（中文重試同聲音，0.5s 後暫時性失敗通常已恢復）
                comm = edge_tts.Communicate(text=processed_text, voice=_secondary, rate=r, pitch=p, **_vol_kw)
                _sec_chunks = 0
                async for chunk in comm.stream():
                    if chunk["type"] == "audio":
                        yield chunk["data"]
                        _sec_chunks += 1
                if _sec_chunks == 0:
                    logger.warning("⚠️ [TTS Stream] Secondary 也零 audio chunk，走 macOS 備援...")
                else:
                    success = True
            except Exception as e:
                logger.error(f"❌ [TTS Stream Error] Secondary 失敗: {e}")
                if _is_edge_blocked(e):
                    self._edge_down_until = time.monotonic() + _EDGE_BREAKER_S
                    logger.warning(f"🔌 [TTS] edge-tts 被微軟拒絕（{e.status}），{int(_EDGE_BREAKER_S/60)} 分鐘內跳過 edge 直接走備援")
                elif isinstance(e, edge_tts.exceptions.NoAudioReceived):
                    self._edge_voice_down[_secondary] = time.monotonic() + _EDGE_VOICE_BREAKER_S
                    logger.warning(f"🔌 [TTS] edge 對 {_secondary} 無音訊，{int(_EDGE_VOICE_BREAKER_S/60)} 分鐘內此聲音直接走備援")

        # --- 第四路徑：Azure Speech 備援 ---
        if not success:
            _az_chunks = 0
            _first, _rest = _split_first_chunk(processed_text)
            async for chunk in self._stream_azure(_first, v, r, p, volume):
                yield chunk
                _az_chunks += 1
            success = _az_chunks > 0
            if success and _rest:
                _rest_chunks = 0
                async for chunk in self._stream_azure(_rest, v, r, p, volume):
                    yield chunk
                    _rest_chunks += 1
                if _rest_chunks == 0:
                    logger.warning(f"⚠️ [TTS] Azure 第二段失敗，只播出首段: '{_rest[:30]}'")

        # --- 第五路徑：Ultimate Fallback (macOS Native) ---
        if not success:
            logger.warning(f"🚨 [TTS] edge-tts 與 Azure 都失敗，啟動 macOS say 終極備援（voice={self.voice}）")
            async for chunk in _yield_macos_say(processed_text):
                yield chunk

    async def generate_audio(self, text: str, emotion: str = "normal", force_macos: bool = False) -> str:
        """
        [Legacy Support] 將文字轉為音訊檔案。
        內部改為呼叫 stream_audio 以維持邏輯一致性。

        emotion：見 _EMOTION_ADJUST——只調 rate/pitch（edge-tts 沒有真情緒 style 可用），
        未知 emotion 值當 "normal"（0 delta，行為與原本不傳 emotion 完全一致）。
        """
        clean_text = self._clean_text(text)
        if not clean_text:
            return None

        rate_delta, pitch_delta = _EMOTION_ADJUST.get(emotion, (0, 0))
        rate = _apply_rate_delta(self.rate, rate_delta) if rate_delta else None
        pitch = _apply_pitch_delta(self.pitch, pitch_delta) if pitch_delta else None

        # emotion 進快取 key：同一句文字不同 emotion 會產生不同 rate/pitch，
        # 不能共用快取檔（否則第二次呼叫會誤用第一次的語速/音高）。
        file_hash = hashlib.md5(f"{clean_text}|{emotion}".encode()).hexdigest()
        ext = ".wav" if force_macos else ".mp3"
        file_name = f"suki_voice_{file_hash}{ext}"
        file_path = os.path.abspath(os.path.join(self.temp_dir, file_name))

        # 🚀 [Cache] 若檔案已存在且有效，直接回傳
        if os.path.exists(file_path) and os.path.getsize(file_path) > 100:
            return file_path

        try:
            # 使用 stream_audio 獲取音訊並寫入檔案
            with open(file_path, "wb") as f:
                async for chunk in self.stream_audio(text, rate=rate, pitch=pitch, force_macos=force_macos):
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
        # 句中長停頓（。！？ ≈1.5s；刪節號已在 _clean_text 轉成 。）；句尾那個 edge 本來就會補靜音，不算
        long_pauses = len(re.findall(r"[。！？]", clean_text.rstrip("。！？\"' ").rstrip("。！？")))

        duration = (chinese_chars * 0.25) + (english_chars * 0.08) + (long_pauses * _LONG_PAUSE_EST_S)
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
