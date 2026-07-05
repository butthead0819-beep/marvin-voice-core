import os
import uuid
import logging
import asyncio
import aiohttp
from datetime import datetime

logger = logging.getLogger(__name__)

SUNO_API_BASE = "https://api.sunoapi.org"
SUNO_POLL_INTERVAL = 10   # seconds between status checks
SUNO_POLL_TIMEOUT = 300   # 5 minutes max wait

GENAI_AVAILABLE = True


def lyria_enabled() -> bool:
    """🪦 Lyria 永久退役（2026-07-05 使用者決策）：env 設 1 也不復活。

    產品型態定為音樂播放為主，不做生成備援、不浪費 API；Suno 失敗即優雅放棄。
    若未來要復活：先過付費記帳鐵則（呼叫前 guard.allow + 成功後 record）再拆這裡。
    """
    return False


class SukiMusicEngine:
    """
    Marvin 專屬音樂生成引擎 (Operation Paranoid Android)
    主力: Suno API — 備援: Google Lyria 3 Pro
    """
    def __init__(self, api_key: str = None):
        self.suno_api_key = os.getenv("SUNO_API_KEY")
        self.google_api_key = api_key or os.getenv("GOOGLE_API_KEY")
        self.lyria_client = None

        try:
            from google import genai
            from google.genai import types
            self._types = types
            if not lyria_enabled():
                logger.info("🎵 [Music Engine] Lyria 已永久退役；Suno 失敗即優雅放棄。")
            elif self.google_api_key:
                self.lyria_client = genai.Client(api_key=self.google_api_key)
                logger.info("🎵 [Music Engine] Lyria 3 Pro 備援核心已就緒。")
            else:
                logger.warning("⚠️ [Music Engine] 缺少 GOOGLE_API_KEY，Lyria 備援不可用。")
        except ImportError:
            logger.warning("⚠️ [Music Engine] 缺少 'google-genai' 套件，Lyria 備援不可用。")
        except Exception as e:
            logger.error(f"❌ [Music Engine] Lyria 初始化失敗: {e}")

        if self.suno_api_key:
            logger.info("🎵 [Music Engine] Suno 主力核心已就緒。")
        else:
            logger.warning("⚠️ [Music Engine] 缺少 SUNO_API_KEY，Suno 主力不可用。")

    # ─── Suno ───────────────────────────────────────────────────────────────

    async def _suno_submit(self, blueprint: dict) -> str | None:
        """提交 Suno 生成任務，回傳 taskId 或 None。"""
        lyrics = blueprint.get("lyrics", "")
        title = blueprint.get("title", "Marvin's Lament")
        style = blueprint.get("style", f"{blueprint.get('genre','Lo-fi')}, {blueprint.get('mood','Depressed')}, {blueprint.get('tempo','Slow')}")
        negative_tags = blueprint.get("negativeTags", "")
        vocal_gender = blueprint.get("vocalGender", "m")

        payload = {
            "customMode": True,
            "instrumental": False,
            "model": "V4_5ALL",
            "style": style[:1000],
            "title": title[:100],
            "prompt": lyrics[:5000],
            "vocalGender": vocal_gender,
            "callBackUrl": "https://example.com/noop",  # 必填但不使用，改用輪詢
        }
        if negative_tags:
            payload["negativeTags"] = negative_tags

        headers = {
            "Authorization": f"Bearer {self.suno_api_key}",
            "Content-Type": "application/json",
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{SUNO_API_BASE}/api/v1/generate",
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    data = await resp.json()
                    if data.get("code") == 200:
                        task_id = data["data"]["taskId"]
                        logger.info(f"🎤 [Suno] 任務已提交: {task_id}")
                        return task_id
                    else:
                        logger.error(f"❌ [Suno] 提交失敗: {data}")
                        return None
        except Exception as e:
            logger.error(f"❌ [Suno] 提交請求異常: {e}")
            return None

    async def _suno_poll(self, task_id: str) -> list[str] | None:
        """
        輪詢 Suno 任務狀態，回傳所有可用音訊 URL 的列表（最多 2 首）。
        偵測到 FIRST_SUCCESS 時立即回傳（20-Second Streaming Output），
        不等待完整 SUCCESS，降低等待時間從 2-3 分鐘到 30-40 秒。
        """
        headers = {"Authorization": f"Bearer {self.suno_api_key}"}
        elapsed = 0

        def _extract_urls(suno_data: list) -> list[str]:
            urls = []
            for item in suno_data:
                url = (
                    item.get("streamAudioUrl")
                    or item.get("sourceStreamAudioUrl")
                    or item.get("audioUrl")
                )
                if url:
                    urls.append(url)
            return urls

        async with aiohttp.ClientSession() as session:
            while elapsed < SUNO_POLL_TIMEOUT:
                await asyncio.sleep(SUNO_POLL_INTERVAL)
                elapsed += SUNO_POLL_INTERVAL
                try:
                    async with session.get(
                        f"{SUNO_API_BASE}/api/v1/generate/record-info",
                        params={"taskId": task_id},
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=15),
                    ) as resp:
                        data = await resp.json()
                        if data.get("code") != 200:
                            logger.warning(f"⚠️ [Suno] 輪詢回應異常: {data}")
                            continue

                        status = data["data"].get("status", "")
                        suno_data = data["data"].get("response", {}).get("sunoData", [])

                        if status in ("GENERATE_AUDIO_FAILED", "SENSITIVE_WORD_ERROR"):
                            logger.error(f"❌ [Suno] 生成失敗: {status}")
                            return None

                        # 20-Second Streaming Output：FIRST_SUCCESS 時優先回傳
                        if status == "FIRST_SUCCESS" and suno_data:
                            urls = _extract_urls(suno_data)
                            if urls:
                                logger.info(f"⚡ [Suno] FIRST_SUCCESS 取得 {len(urls)} 首串流 URL ({elapsed}s)")
                                return urls

                        # 完整 SUCCESS
                        if status == "SUCCESS" and suno_data:
                            urls = _extract_urls(suno_data)
                            if urls:
                                logger.info(f"✅ [Suno] SUCCESS 取得 {len(urls)} 首 URL ({elapsed}s)")
                                return urls

                        logger.debug(f"🔄 [Suno] 狀態: {status} ({elapsed}s)")
                except Exception as e:
                    logger.warning(f"⚠️ [Suno] 輪詢異常: {e}")

        logger.error(f"❌ [Suno] 輪詢超時 ({SUNO_POLL_TIMEOUT}s)")
        return None

    async def _suno_download(self, audio_url: str, file_path: str) -> bool:
        """下載 Suno 音訊到本地，回傳是否成功。"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(audio_url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                    if resp.status == 200:
                        with open(file_path, "wb") as f:
                            f.write(await resp.read())
                        logger.info(f"✅ [Suno] 音訊已下載: {file_path}")
                        return True
                    else:
                        logger.error(f"❌ [Suno] 下載失敗 HTTP {resp.status}")
                        return False
        except Exception as e:
            logger.error(f"❌ [Suno] 下載異常: {e}")
            return False

    async def _generate_with_suno(self, blueprint: dict, file_path: str) -> tuple[list[str] | None, str | None]:
        """Suno 完整生成流程，回傳 ([path1, path2, ...], error)。"""
        if not self.suno_api_key:
            return None, "缺少 SUNO_API_KEY"

        task_id = await self._suno_submit(blueprint)
        if not task_id:
            return None, "Suno 任務提交失敗"

        audio_urls = await self._suno_poll(task_id)
        if not audio_urls:
            return None, "Suno 生成超時或失敗"

        # 根據 URL 數量，生成對應的檔案路徑（無後綴 = 第 1 首，_2 = 第 2 首）
        base, ext = os.path.splitext(file_path)
        paths = [file_path] + [f"{base}_{i + 2}{ext}" for i in range(len(audio_urls) - 1)]

        downloaded = []
        for url, path in zip(audio_urls, paths):
            if await self._suno_download(url, path):
                downloaded.append(path)
            else:
                logger.warning(f"⚠️ [Suno] 跳過下載失敗的 URL: {url[:60]}")

        if downloaded:
            return downloaded, None
        return None, "Suno 音訊下載全部失敗"

    # ─── Lyria (備援) ────────────────────────────────────────────────────────

    async def _generate_with_lyria(self, blueprint: dict, file_path: str) -> tuple[str | None, str | None]:
        """Lyria 備援生成流程，回傳 (path, error)。"""
        if not self.lyria_client:
            return None, "Lyria 核心未就緒"

        types = self._types
        genre = blueprint.get("genre", "Lo-fi")
        tempo = blueprint.get("tempo", "Slow")
        mood = blueprint.get("mood", "Bored")
        lyrics = blueprint.get("lyrics", "")

        prompt = (
            f"[DURATION: EXACTLY 30 SECONDS (STRICT)]\n"
            f"[STRUCTURE: Short Intro -> Verse -> Chorus -> Brief Outro]\n"
            f"[GENRE: {genre}] [MOOD: {mood}] [TEMPO: {tempo}]\n"
            f"[Vocal: Deep, depressed, melancholic male voice]\n"
            f"[LYRICS]:\n{lyrics}"
        )

        try:
            logger.info(f"🎤 [Lyria] 備援啟動，生成中: {genre} / {mood}...")
            response = await asyncio.to_thread(
                self.lyria_client.models.generate_content,
                model="lyria-3-pro-preview",
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_modalities=["AUDIO", "TEXT"]
                )
            )

            audio_data = None
            for part in response.candidates[0].content.parts:
                if part.inline_data:
                    audio_data = part.inline_data.data
                    break

            if not audio_data:
                return None, "Lyria 未回傳音訊數據"

            with open(file_path, "wb") as f:
                f.write(audio_data)

            logger.info(f"✅ [Lyria] 備援生成完成: {file_path}")
            return file_path, None

        except Exception as e:
            error_str = str(e)
            logger.error(f"❌ [Lyria] 備援生成失敗: {error_str}")
            return None, error_str

    # ─── 主入口 ──────────────────────────────────────────────────────────────

    async def create_daily_single(self, blueprint: dict, custom_filename: str = None) -> tuple[list[str] | None, str | None]:
        """
        生成今日單曲。主力: Suno → 備援: Lyria。
        回傳 ([path1, path2, ...], error)，Suno 最多 2 首，Lyria 1 首。
        """
        if custom_filename:
            file_name = custom_filename
        else:
            today_str = datetime.now().strftime("%Y%m%d")
            file_name = f"marvin_single_{today_str}.mp3"

        file_path = os.path.abspath(os.path.join("records", file_name))
        os.makedirs("records", exist_ok=True)

        if not custom_filename and os.path.exists(file_path):
            logger.info(f"⏭️ [Music Engine] 今日單曲已存在: {file_name}，跳過生成。")
            return [file_path], None

        # 主力：Suno
        logger.info("🎵 [Music Engine] 主力 Suno 啟動...")
        paths, error = await self._generate_with_suno(blueprint, file_path)
        if paths:
            return paths, None

        # 備援：Lyria
        logger.warning(f"⚠️ [Music Engine] Suno 失敗 ({error})，切換備援 Lyria...")
        lyria_path, error = await self._generate_with_lyria(blueprint, file_path)
        if lyria_path:
            return [lyria_path], None

        return None, f"Suno 與 Lyria 均失敗：{error}"

    def get_estimated_duration(self) -> float:
        return 30.0

    async def generate_suki_song(self, lyrics: str, style_preset: str = "Lo-fi") -> tuple[str | None, str | None]:
        """舊版介面相容，直接組一個簡易 blueprint 走主流程。"""
        blueprint = {
            "genre": style_preset,
            "mood": "Depressed and weary",
            "tempo": "Slow",
            "lyrics": lyrics,
            "title": "Marvin's Lament",
        }
        file_name = f"marvin_song_{uuid.uuid4().hex}.mp3"
        file_path = os.path.abspath(os.path.join("records", file_name))
        os.makedirs("records", exist_ok=True)

        paths, error = await self._generate_with_suno(blueprint, file_path)
        if paths:
            return paths[0], None
        lyria_path, error = await self._generate_with_lyria(blueprint, file_path)
        return lyria_path, error
