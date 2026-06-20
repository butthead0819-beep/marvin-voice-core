"""
MusicProxyMixin — VoiceController 對 MusicCog 的純委派 method shim（收尾 ④）。

從 voice_controller.py 抽出（減肥）。每個方法 body 只是
`mc = self.bot.cogs.get('MusicCog')` 後轉呼 mc.X()（不在則 no-op / 回預設）。
以 mixin 併入後仍在 VoiceController 實例上，外部呼叫者（vc.start_radio /
vc.play_stream_song / vc._auto_recommend 等）零影響。

這是把 shim「搬出 voice_controller」保留 facade，不是刪 facade 改呼叫端。
"""
from __future__ import annotations

import os

import discord


class MusicProxyMixin:
    def _build_recommendation_extras(self) -> dict:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            return mc._build_recommendation_extras()
        return {}

    async def _safe_music_command(self, speaker: str, query: str, cmd: str):
        """[Phase 7G stub] → MusicCog._safe_music_command"""
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            await mc._safe_music_command(speaker, query, cmd)

    async def _handle_voice_music_command(self, speaker: str, query: str, cmd: str):
        """[Phase 7G stub] → MusicCog._handle_voice_music_command"""
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            await mc._handle_voice_music_command(speaker, query, cmd)

    async def start_radio(self, trigger: str = "未知觸發"):
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            await mc.start_radio(trigger)

    async def stop_radio(self, reason: str = "未知原因"):
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            await mc.stop_radio(reason)

    async def _radio_volume_fade_loop(self):
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            await mc._radio_volume_fade_loop()

    async def _radio_loop(self):
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            await mc._radio_loop()

    async def play_radio_song(self, file_path: str):
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            await mc.play_radio_song(file_path)

    async def _resolve_yt_query(self, query: str) -> dict | None:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            return await mc._resolve_yt_query(query)
        return None

    async def _stream_loop(self):
        """🎵 [Phase 7D stub] → MusicCog._stream_loop"""
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            await mc._stream_loop()

    def _parse_song_title_artist(self, info: dict) -> tuple[str, str]:
        """[Phase 7E stub] → MusicCog._parse_song_title_artist"""
        mc = self.bot.cogs.get('MusicCog')
        return mc._parse_song_title_artist(info) if mc else (info.get('title', ''), '')

    async def _fetch_lyrics_synced(self, info: dict) -> str | None:
        """[Phase 7E stub] → MusicCog._fetch_lyrics_synced"""
        mc = self.bot.cogs.get('MusicCog')
        return await mc._fetch_lyrics_synced(info) if mc else None

    async def _fetch_lyrics_raw(self, info: dict) -> str | None:
        """[Phase 7E stub] → MusicCog._fetch_lyrics_raw"""
        mc = self.bot.cogs.get('MusicCog')
        return await mc._fetch_lyrics_raw(info) if mc else None

    async def _fetch_comment_raw(self, info: dict) -> str | None:
        """[Phase 7E stub] → MusicCog._fetch_comment_raw"""
        mc = self.bot.cogs.get('MusicCog')
        return await mc._fetch_comment_raw(info) if mc else None

    async def _fetch_dj_interjection_raw(self, info: dict) -> dict | None:
        """[Phase 7E stub] → MusicCog._fetch_dj_interjection_raw"""
        mc = self.bot.cogs.get('MusicCog')
        return await mc._fetch_dj_interjection_raw(info) if mc else None

    async def _meta_with_ack_fallback(self, info: dict, requested_by: str) -> dict:
        """[Phase 7E stub] → MusicCog._meta_with_ack_fallback"""
        mc = self.bot.cogs.get('MusicCog')
        return await mc._meta_with_ack_fallback(info, requested_by) if mc else {}

    async def _fetch_song_meta(self, info: dict) -> dict:
        """[Phase 7E stub] → MusicCog._fetch_song_meta"""
        mc = self.bot.cogs.get('MusicCog')
        return await mc._fetch_song_meta(info) if mc else {}

    async def _maybe_play_dj_interjection(self, dj: dict | None):
        """[Phase 7E stub] → MusicCog._maybe_play_dj_interjection"""
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            await mc._maybe_play_dj_interjection(dj)

    async def _t2_discovery_candidates(self, members: list[str], exclude_titles: list[str]) -> list:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            return await mc._t2_discovery_candidates(members, exclude_titles)
        return []

    def _load_taste_fingerprint(self) -> dict:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            return mc._load_taste_fingerprint()
        return {}

    async def _auto_recommend(self, username: str, *, _tier: int = 1):
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            await mc._auto_recommend(username, _tier=_tier)

    async def _llm_coverify(self, cand, exclude_titles: list[str]) -> str:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            return await mc._llm_coverify(cand, exclude_titles)
        return ""

    async def _handle_find_song(self, mode: str, payload: str, speaker: str):
        """[Phase 7G stub] → MusicCog._handle_find_song"""
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            await mc._handle_find_song(mode, payload, speaker)

    async def _get_audio_duration(self, path: str) -> float:
        """[Phase 7E stub] → MusicCog._get_audio_duration"""
        mc = self.bot.cogs.get('MusicCog')
        return await mc._get_audio_duration(path) if mc else 3.0

    async def play_stream_song(self, url: str, title: str, dj_audio_path: str | None = None):
        """🎵 [Phase 7D stub] → MusicCog.play_stream_song"""
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            await mc.play_stream_song(url, title, dj_audio_path=dj_audio_path)

    async def _measure_norm_gain_bg(self, url: str):
        """[Phase 7E stub] → MusicCog._measure_norm_gain_bg"""
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            await mc._measure_norm_gain_bg(url)

    def _extract_song_metadata(self, file_path: str):
        """[Phase 7E stub] → MusicCog._extract_song_metadata"""
        mc = self.bot.cogs.get('MusicCog')
        return mc._extract_song_metadata(file_path) if mc else {"title": os.path.basename(file_path), "artist": "未知藝術家"}

    def _extract_song_cover(self, file_path: str):
        """[Phase 7E stub] → MusicCog._extract_song_cover"""
        mc = self.bot.cogs.get('MusicCog')
        return mc._extract_song_cover(file_path) if mc else None

    def _extract_dominant_color(self, cover_path: str) -> discord.Color:
        """[Phase 7E stub] → MusicCog._extract_dominant_color"""
        mc = self.bot.cogs.get('MusicCog')
        return mc._extract_dominant_color(cover_path) if mc else discord.Color.dark_grey()

    async def _delayed_cleanup(self, file_path: str, delay: float = 10.0):
        """[Phase 7E stub] → MusicCog._delayed_cleanup"""
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            await mc._delayed_cleanup(file_path, delay)

