"""
MusicSubsystemMixin — MusicCog 的電台（radio）與 stream loop 生命週期管理方法。

從 music_cog.py 抽出（減肥，比照 voice_controller.py 拆解先例），以 mixin 形式
併入 MusicCog：
    class MusicCog(..., MusicSubsystemMixin, ..., commands.Cog): ...
因此 self 仍是 MusicCog 實例，_vc / _stream_loop / _radio_loop /
_extract_song_metadata / _extract_song_cover / _extract_dominant_color /
_delayed_cleanup / _publish_now_playing_state / _cancel_stream_task 等全部
沿用原本的 self 存取，行為零改動。

_get_puck_client() 是 music_cog.py 模組層級的純函式（跟主檔核心區塊共用，非
self 方法），這裡在方法內 local import 取用，避免跟主檔互相 import 造成循環
（同一招在 music_cog_commands.py 對 cogs.voice_views.PlayControlView 已用過）。
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import os
import time

import discord

logger = logging.getLogger(__name__)


class MusicSubsystemMixin:
    # ── 🎵 Music subsystem methods ────────────────────────────────────────────

    async def start_radio(self, trigger: str = "未知觸發"):
        """📻 啟動電台：掃描歌單 → shuffle → 開始背景播放 Loop。"""
        import random
        if self.radio_mode:
            logger.warning("⚠️ [Radio] 電台已啟動，跳過重複啟動。")
            return

        songs_dir = "assets/songs"
        excluded = {"Oh Marvin.mp3"}
        try:
            all_songs = [
                os.path.join(songs_dir, f)
                for f in os.listdir(songs_dir)
                if f.endswith(".mp3") and f not in excluded
            ]
        except FileNotFoundError:
            logger.error(f"❌ [Radio] 找不到歌曲目錄: {songs_dir}")
            return

        if not all_songs:
            logger.warning("⚠️ [Radio] 歌單為空，無法啟動電台。")
            return

        random.shuffle(all_songs)
        self._radio_song_list = all_songs
        self.radio_mode = True
        logger.info(f"📻 [Radio] 電台啟動 (來源: {trigger})，共 {len(all_songs)} 首歌曲。")

        if self.radio_task and not self.radio_task.done():
            self.radio_task.cancel()
        self.radio_task = asyncio.create_task(self._radio_loop())

    async def stop_radio(self, reason: str = "未知原因"):
        """📻 停止電台：中斷播放 → 取消 Task → 重設狀態。"""
        if not self.radio_mode:
            return

        self.radio_mode = False
        self.radio_paused = False
        logger.info(f"📻 [Radio] 電台停止，原因: {reason}")

        if self.radio_task and not self.radio_task.done():
            self.radio_task.cancel()
            self.radio_task = None
        if self._radio_fade_task and not self._radio_fade_task.done():
            self._radio_fade_task.cancel()
            self._radio_fade_task = None
        self._radio_source = None

        guild_vc = next((v for v in self.bot.voice_clients if v.is_connected()), None)
        if guild_vc and guild_vc.is_playing():
            guild_vc.stop_playing()
            logger.info("📻 [Radio] 已立即停止當前播放的歌曲。")

    def _ensure_stream_loop(self) -> bool:
        """佇列有歌但 stream loop 沒在跑 → 叫醒它。回傳「是否剛叫醒」。

        2026-07-17 死鎖事故：/dismiss 會 cancel loop 但不清 stream_queue，使用者
        重點同一首歌撞上 _check_song_duplicate 早退 → 走不到重啟 loop 的程式碼 →
        佇列永遠卡著（唯一逃生出口是點一首不同的歌，但沒人會知道）。
        loop 活著時不動它——別把正在播的那首打斷。

        ⚠️ 判活看 **task**，不看 stream_mode：旗標會說謊。loop 被 cancel 時
        （非 stop_stream 那條）旗標停在 True 但沒人在播，只信旗標會 no-op →
        佇列卡死（2026-07-17 第二次事故）。
        """
        alive = self.stream_task is not None and not self.stream_task.done()
        if alive and self.stream_mode:
            return False
        if alive:
            self._cancel_stream_task("_ensure_stream_loop 收殘骸")   # 收尾中的殘骸 → 收掉重來
        self.stream_mode = True
        self.stream_volume = self._default_stream_volume
        self._stream_user_stopped = False  # 有人／有東西要它跑了，解除 watchdog 抑制
        self.stream_task = asyncio.create_task(self._stream_loop())
        logger.warning(f"🎵 [Stream] loop 不在跑（flag={self.stream_mode} task_alive={alive}）"
                       f"→ 叫醒，佇列 {len(self.stream_queue)} 首")
        return True

    def _cancel_stream_task(self, reason: str) -> None:
        """統一 stream_task.cancel() 出口 + 記錄呼叫來源。

        2026-08-01 事故：迴圈在歌曲自然轉場瞬間被取消，但沒有任何 log 留下是誰
        呼叫的 `.cancel()`，只能靠時間軸推理、抓不到真兇。統一出口讓已知的呼叫
        點都留痕；仍抓不到的話至少能從留痕排除法縮小範圍。
        """
        if self.stream_task is not None and not self.stream_task.done():
            logger.info(f"🎵 [Stream] stream_task.cancel() 呼叫來源: {reason}")
            self.stream_task.cancel()

    async def stop_stream(self, reason: str = "未知原因"):
        """🎵 停止串流播放，清空當前狀態。"""
        if not self.stream_mode:
            return
        vc = self._vc()
        self.stream_mode = False
        self._stream_user_stopped = True  # 主動停播 → watchdog 別自己復活它
        self._personal_shuffle = None  # 🎲 停播一併收掉個人歌單 session，避免之後復活
        if vc is not None:
            vc.last_marvin_speech_time = time.time()
        self._current_stream_info = None
        self.stream_paused = False
        self._publish_now_playing_state(None)
        logger.info(f"🎵 [Stream] 停止，原因: {reason}")
        if self.stream_task and not self.stream_task.done():
            self._cancel_stream_task(f"stop_stream reason={reason}")
            self.stream_task = None
        if self._radio_fade_task and not self._radio_fade_task.done():
            self._radio_fade_task.cancel()
            self._radio_fade_task = None
        self._radio_source = None
        if vc is not None and vc._mixer is not None:
            vc._mixer.clear_music()
        from cogs.music_cog import _get_puck_client
        puck_client = _get_puck_client()
        if puck_client is not None:
            asyncio.create_task(self._fire_puck_stop(puck_client))

    async def _radio_volume_fade_loop(self):
        """📻 動態音量漸變：有人說話 → duck to 1%；靜默 1.5s 後 → fade up to 10%。"""
        IDLE_VOL  = 0.10
        DUCK_VOL  = 0.01
        TICK      = 0.05
        DUCK_RATE = 0.012
        RISE_RATE = 0.003
        DUCK_HOLD = 1.5
        try:
            while self.radio_mode or self.stream_mode:
                src = self._radio_source
                if src is not None:
                    vc = self._vc()
                    silence = time.time() - (vc.last_player_speech_time if vc is not None else 0.0)
                    target = IDLE_VOL if silence > DUCK_HOLD else DUCK_VOL
                    current = src.volume
                    if current > target + 0.001:
                        src.volume = max(target, current - DUCK_RATE)
                    elif current < target - 0.001:
                        src.volume = min(target, current + RISE_RATE)
                await asyncio.sleep(TICK)
        except asyncio.CancelledError:
            pass

    async def _radio_loop(self):
        """📻 背景播放迴圈：依序播放歌單，播完後 shuffle 重複。"""
        import random
        logger.info("📻 [Radio Loop] 電台迴圈已啟動。")
        try:
            while self.radio_mode:
                if not self._radio_song_list:
                    songs_dir = "assets/songs"
                    excluded = {"Oh Marvin.mp3"}
                    try:
                        all_songs = [
                            os.path.join(songs_dir, f)
                            for f in os.listdir(songs_dir)
                            if f.endswith(".mp3") and f not in excluded
                        ]
                    except FileNotFoundError:
                        logger.error("❌ [Radio Loop] 重新掃描失敗，停止電台。")
                        self.radio_mode = False
                        break
                    random.shuffle(all_songs)
                    self._radio_song_list = all_songs
                    logger.info(f"📻 [Radio Loop] 歌單播完，重新洗牌 ({len(all_songs)} 首)。")

                next_song = self._radio_song_list.pop()
                song_name = os.path.basename(next_song)
                logger.info(f"📻 [Radio Loop] 即將播放: {song_name}")

                vc = self._vc()
                if vc is not None:
                    metadata = self._extract_song_metadata(next_song)
                    cover_path = self._extract_song_cover(next_song)
                    active_ch = vc.active_text_channel
                else:
                    metadata = {"title": song_name, "artist": "未知"}
                    cover_path = None
                    active_ch = None

                if active_ch:
                    accent_color = (
                        self._extract_dominant_color(cover_path)
                        if cover_path
                        else discord.Color.dark_grey()
                    )
                    embed = discord.Embed(
                        title="📻 馬文電台：正在播放",
                        description="「...」",
                        color=accent_color,
                        timestamp=datetime.datetime.now(),
                    )
                    embed.add_field(name="🎵 歌曲名稱", value=f"`{metadata['title']}`", inline=False)
                    embed.add_field(name="👤 演出者", value=f"`{metadata['artist']}`", inline=True)
                    embed.add_field(name="🔊 當前音量", value=f"`{int(self.radio_volume * 100)}%`", inline=True)

                    if cover_path:
                        file = discord.File(cover_path, filename="cover.jpg")
                        embed.set_thumbnail(url="attachment://cover.jpg")
                        sent_msg = await active_ch.send(file=file, embed=embed)
                        if vc is not None:
                            asyncio.create_task(self._delayed_cleanup(cover_path))
                    else:
                        sent_msg = await active_ch.send(embed=embed)

                    async def _update_radio_comment(msg, title, artist, color, song_path, _vc_ref=vc):
                        from utils import pick_lyrics_snippet
                        lyrics_path = os.path.splitext(song_path)[0] + ".md"
                        section_name, snippet = pick_lyrics_snippet(lyrics_path)
                        if snippet:
                            song_ctx = f"歌名：{title}，演出者：{artist}，段落：{section_name}，歌詞：{snippet}"
                        else:
                            song_ctx = f"歌名：{title}，演出者：{artist}"
                        try:
                            comment = await self.bot.router.generate_dynamic_system_msg(
                                "radio_now_playing", context=song_ctx
                            )
                        except Exception:
                            return
                        try:
                            updated = discord.Embed(
                                title="📻 馬文電台：正在播放",
                                description=f"「{comment}」",
                                color=color,
                                timestamp=msg.embeds[0].timestamp if msg.embeds else datetime.datetime.now(),
                            )
                            updated.add_field(name="🎵 歌曲名稱", value=f"`{title}`", inline=False)
                            updated.add_field(name="👤 演出者", value=f"`{artist}`", inline=True)
                            updated.add_field(name="🔊 當前音量", value=f"`{int(self.radio_volume * 100)}%`", inline=True)
                            if msg.embeds and msg.embeds[0].thumbnail:
                                updated.set_thumbnail(url=msg.embeds[0].thumbnail.url)
                            await msg.edit(embed=updated)
                        except Exception as e:
                            logger.warning(f"⚠️ [Radio] embed 更新失敗: {e}")

                    asyncio.create_task(
                        _update_radio_comment(sent_msg, metadata["title"], metadata["artist"], accent_color, next_song)
                    )

                await self.play_radio_song(next_song)

                if self.radio_mode:
                    await asyncio.sleep(1.0)

        except asyncio.CancelledError:
            logger.info("📻 [Radio Loop] 電台迴圈被取消。")
            self.radio_paused = False
        except Exception as e:
            logger.error(f"❌ [Radio Loop] 發生異常: {e}")
            self.radio_mode = False
            self.radio_paused = False

    async def play_radio_song(self, file_path: str):
        """📻 播放單首電台歌曲，透過 VC mixer。"""
        if not os.path.exists(file_path):
            logger.warning(f"⚠️ [Radio Song] 找不到檔案: {file_path}")
            return

        vc = self._vc()
        device = vc._resolve_playback_device() if vc is not None else None
        if device is None:
            logger.warning("⚠️ [Radio Song] 無可用播放裝置（Discord VC / 本機喇叭皆無），跳過播放。")
            self.radio_mode = False
            self.radio_paused = False
            return

        src = discord.FFmpegPCMAudio(file_path, options="-vn")
        await vc._mixer_play_music(
            device, src,
            still_active=lambda: self.radio_mode,
            volume_attr="radio_volume",
        )
