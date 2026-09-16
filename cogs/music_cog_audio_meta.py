"""
MusicAudioMetaMixin — MusicCog 的音訊分析/本機檔案 metadata 提取/暫存檔清理方法。

從 music_cog.py 抽出（減肥，比照 voice_controller.py 拆解先例），以 mixin 形式
併入 MusicCog：
    class MusicCog(..., MusicAudioMetaMixin, ..., commands.Cog): ...
因此 self 仍是 MusicCog 實例，bot.router / bot.music_memory /
_current_stream_info / _stream_norm_gain 等全部沿用原本的 self 存取，
行為零改動。

_SONG_BPM_STORE / _BPM_SAMPLE_SR 是純字面常數，跟主檔（其他地方也用得到）各自
定義一份，不搬移、不 import——避免為了兩個純資料常數製造跨檔依賴。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import tempfile
import time

import discord

from music_memory import extract_video_id

logger = logging.getLogger(__name__)

_SONG_BPM_STORE = "records/song_bpm.json"
_BPM_SAMPLE_SR = 11025


class MusicAudioMetaMixin:
    # ── 🎵 Song metadata / fetch helpers（音訊分析/檔案清理子集）───────────────

    async def _analyze_song_reactions(self, info: dict, song_start_time: float, lyrics: str):
        """歌曲結束後掃描對話，分析聆聽反應並寫入音樂記憶。"""
        if not hasattr(self.bot, 'music_memory'):
            return
        conv = self.bot.engine.conv_buffer
        elapsed = time.time() - song_start_time
        harvest = conv.get_harvest(song_start_time, before=5.0, after=elapsed + 2.0)
        if not harvest.strip():
            return

        lyrics_hint = lyrics[:400] if lyrics else "無歌詞資料"
        prompt = (
            f"歌曲《{info['title']}》剛才播放完畢。\n\n"
            f"播放期間的對話：\n{harvest}\n\n"
            f"歌詞片段：{lyrics_hint}\n\n"
            "請分析每位成員對這首歌的反應，**只記錄有明顯感受的人**。\n"
            "輸出 JSON（不加 markdown）：\n"
            '{"reactions": {"成員名": {"feelings": ["情緒詞"], "quotes": ["他說的具體語句"], '
            '"lyric_match": "歌詞與他的話的呼應描述，無則空字串"}}}'
        )
        try:
            import json as _json
            raw = await self.bot.router._call_llm(
                system_prompt="你是音樂聆聽反應分析助手，只記錄有明顯情感的成員，不過度推測。",
                user_prompt=prompt,
                is_json=True,
                tier="simple",
            )
            reactions = _json.loads(raw).get("reactions", {})
            if reactions:
                self.bot.music_memory.record_reactions(info, reactions)
                logger.info(f"🎵 [MusicMemory] 記錄 {len(reactions)} 人的反應: {info['title']}")
                try:
                    from bridge_emitters import emit_music_reaction_to_bridge
                    for username, r in reactions.items():
                        feelings = r.get("feelings", []) or []
                        tag = "love" if feelings else "silent"
                        asyncio.create_task(emit_music_reaction_to_bridge(
                            self.bot, username, info, tag
                        ))
                except Exception as e:
                    logger.debug(f"⚠️ [Companion_Bridge] music_reaction hook skipped: {e}")
        except Exception as e:
            logger.debug(f"⚠️ [MusicMemory] 反應分析失敗: {e}")

    async def _get_audio_duration(self, path: str) -> float:
        """使用 ffprobe 取得本地音訊檔案的時長（秒）。"""
        try:
            import json as _json
            ffprobe = "/opt/homebrew/bin/ffprobe" if os.path.exists("/opt/homebrew/bin/ffprobe") else "ffprobe"
            proc = await asyncio.create_subprocess_exec(
                ffprobe, '-v', 'quiet', '-print_format', 'json', '-show_streams', path,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            data = _json.loads(stdout)
            for stream in data.get('streams', []):
                if stream.get('codec_type') == 'audio':
                    return float(stream.get('duration', 3.0))
        except Exception:
            pass
        return 3.0

    async def _measure_norm_gain_bg(
        self,
        url: str,
        duration: float | None = None,
        highlight_start_s: float | None = None,
        info: dict | None = None,
        delay_s: float = 0.0,
    ):
        """[響度正規化] 背景取樣歌曲 25/50/75% 三點量整合響度 → 算常數增益存 _stream_norm_gain[url]。

        支援傳入 duration、highlight_start_s 與 info，避免在預載或預取時受當前播歌狀態干擾。
        順便在同一趟 ffmpeg（同取樣點、同一個 process 兩個輸出：ebur128→null 給
        stderr 響度統計、raw f32le mono→stdout 給 BPM 估算）取 PCM 估 BPM，落地存
        records/song_bpm.json（見 bpm_estimate.py）——BPM 分析不擋、不影響既有響度
        正規化行為，失敗只是沒存到 BPM。

        delay_s：起跑前先 sleep 這麼久，避開呼叫端（開播/preload）當下的解碼尖峰
        （2026-08-25：BPM 估算是同步 numpy，量測跟解碼撞在一起會卡 event loop 造成
        開頭斷續/加速，見 CLAUDE.md 對應討論）。呼叫端排程用；單元測試直呼此函式
        預設 0 不等。"""
        if not url or url in self._stream_norm_gain:
            return
        if delay_s > 0:
            await asyncio.sleep(delay_s)
        import numpy as np

        from bpm_estimate import estimate_bpm_from_pcm, median_bpm, write_bpm
        from loudness_norm import (
            sample_positions, parse_ebur128_integrated, average_lufs, compute_loudness_gain,
            DEFAULT_WINDOW_S,
        )
        song_info = info if info is not None else (self._current_stream_info or {})
        dur = float(duration if duration is not None else (song_info.get("duration") or 0))
        start_s = float(highlight_start_s if highlight_start_s is not None else (song_info.get("highlight_start_s") or 0.0))

        lufs_vals: list[float | None] = []
        bpm_vals: list[float | None] = []
        for pos in sample_positions(dur, start_s=start_s):
            try:
                proc = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-nostats", "-ss", f"{pos:.1f}", "-t", f"{DEFAULT_WINDOW_S:.0f}", "-i", url,
                    "-af", "ebur128", "-f", "null", "-",
                    "-vn", "-ac", "1", "-ar", str(_BPM_SAMPLE_SR), "-f", "f32le", "pipe:1",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
                lufs_vals.append(parse_ebur128_integrated(stderr.decode("utf-8", "ignore")))
                pcm = np.frombuffer(stdout, dtype=np.float32)
                bpm_vals.append(await asyncio.to_thread(estimate_bpm_from_pcm, pcm, _BPM_SAMPLE_SR))
            except Exception:
                lufs_vals.append(None)
                bpm_vals.append(None)
        video_id = extract_video_id(song_info.get("webpage_url") or song_info.get("url") or url)
        bpm = median_bpm(bpm_vals)
        if bpm is not None and video_id:
            write_bpm(_SONG_BPM_STORE, video_id, bpm)
            logger.info(f"🥁 [BPM] {url[:40]} 估計 {bpm:.0f} BPM → 存 {video_id}")
        avg = average_lufs(lufs_vals)
        if avg is None:
            logger.warning(f"⚠️ [LoudNorm] {url[:40]} 響度量測無結果，用 raw 音量")
            return
        gain = compute_loudness_gain(avg)
        self._stream_norm_gain[url] = gain
        logger.info(f"🎚️ [LoudNorm] 量測完成 I≈{avg:.1f} LUFS → 增益 {gain:.2f}x（每首套一次）")

    def _extract_song_metadata(self, file_path: str):
        """📻 [Marvin Radio] 使用 ffprobe 提取標題與演出者。"""
        try:
            ffprobe_path = "/opt/homebrew/bin/ffprobe" if os.path.exists("/opt/homebrew/bin/ffprobe") else "ffprobe"
            cmd = [ffprobe_path, "-v", "quiet", "-print_format", "json", "-show_format", file_path]
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            data = json.loads(result.stdout)
            tags = data.get("format", {}).get("tags", {})
            return {
                "title": tags.get("title", os.path.basename(file_path)),
                "artist": tags.get("artist", "未知藝術家")
            }
        except Exception as e:
            logger.error(f"⚠️ [Radio Metadata] 提取失敗: {e}")
            return {"title": os.path.basename(file_path), "artist": "未知藝術家"}

    def _extract_song_cover(self, file_path: str):
        """📻 [Marvin Radio] 使用 ffmpeg 提取封面至暫存檔。"""
        try:
            temp_fd, temp_path = tempfile.mkstemp(suffix=".jpg")
            os.close(temp_fd)
            ffmpeg_path = "/opt/homebrew/bin/ffmpeg" if os.path.exists("/opt/homebrew/bin/ffmpeg") else "ffmpeg"
            cmd = [ffmpeg_path, "-y", "-i", file_path, "-an", "-vcodec", "copy",
                   "-f", "image2", "-frames:v", "1", temp_path]
            subprocess.run(cmd, capture_output=True, check=True)
            if os.path.exists(temp_path) and os.path.getsize(temp_path) > 0:
                return temp_path
            if os.path.exists(temp_path):
                os.remove(temp_path)
            return None
        except Exception:
            if 'temp_path' in locals() and os.path.exists(temp_path):
                os.remove(temp_path)
            return None

    def _extract_dominant_color(self, cover_path: str) -> discord.Color:
        """📻 [Marvin Radio] 從封面圖提取主色調，回傳 discord.Color。"""
        try:
            from PIL import Image
            img = Image.open(cover_path).convert("RGB")
            img = img.resize((60, 60), Image.LANCZOS)
            quantized = img.quantize(colors=8)
            palette = quantized.getpalette()
            best_color = None
            best_score = -1.0
            for i in range(8):
                r, g, b = palette[i * 3], palette[i * 3 + 1], palette[i * 3 + 2]
                lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255.0
                if lum < 0.10 or lum > 0.90:
                    continue
                max_c = max(r, g, b) / 255.0
                min_c = min(r, g, b) / 255.0
                denom = 1.0 - abs(2.0 * lum - 1.0)
                sat = (max_c - min_c) / denom if denom > 0.001 else 0.0
                score = sat * 0.7 + (1.0 - abs(lum - 0.5) * 2) * 0.3
                if score > best_score:
                    best_score = score
                    best_color = (r, g, b)
            if best_color:
                return discord.Color.from_rgb(*best_color)
        except Exception as e:
            logger.debug(f"⚠️ [Cover Color] 提取失敗: {e}")
        return discord.Color.dark_grey()

    async def _delayed_cleanup(self, file_path: str, delay: float = 10.0):
        """📻 [Marvin Radio] 延後刪除暫存檔，確保 Discord 上傳完成。"""
        try:
            await asyncio.sleep(delay)
            if os.path.exists(file_path):
                os.remove(file_path)
        except Exception:
            pass
