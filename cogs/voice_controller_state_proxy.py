"""
StateProxyMixin — VoiceController 的狀態存取 property 代理。

從 voice_controller.py 抽出（減肥）。這些 @property 把 stream_mode / radio_mode /
stream_* / radio_* / is_playing_audio / tts_queue_duration / voice_client /
_last_search 等代理到 MusicCog / _mixer / bot.voice_clients（不在則用 _X_local
fallback，於 __init__ 設定）。全是 self 存取的 descriptor，以 mixin 併入後經 MRO
正常解析，行為零改動。

這是把委派 shim「搬出 voice_controller」（保留 facade，呼叫端不必知道 MusicCog
載入順序），不是刪 facade 改呼叫端。
"""
from __future__ import annotations


class StateProxyMixin:
    # 🎛️ [Plan 12 / T4] flag=on 時 is_playing_audio / tts_queue_duration 由 mixer 維護，
    # ~20 個既有 reader（Echo Guard / wake-suppress / storm / ack / dual / :853）零改動自然正確。
    # flag=off 時走 backing field（舊 writer 照常設）。
    # 🩹 [Pre-existing fix] 9254f841 的 _play_ack 讀 self.voice_client 但該屬性從未定義 →
    # AttributeError 崩潰（incident 191408 / stream-wake ack 沒出）。改成 property 即時查
    # 連線中的 vc；setter 供測試覆寫（測試本來就 cog.voice_client = mock）。
    @property
    def voice_client(self):
        if self._voice_client_override is not None:
            return self._voice_client_override
        return next((vc for vc in self.bot.voice_clients if vc.is_connected()), None)

    @voice_client.setter
    def voice_client(self, value):
        self._voice_client_override = value

    @property
    def is_playing_audio(self) -> bool:
        if getattr(self, "_plan12", False) and getattr(self, "_mixer", None) is not None:
            return self._mixer.is_playing_audio
        return getattr(self, "_is_playing_audio", False)

    @is_playing_audio.setter
    def is_playing_audio(self, value: bool) -> None:
        self._is_playing_audio = value

    @property
    def tts_queue_duration(self) -> float:
        if getattr(self, "_plan12", False) and getattr(self, "_mixer", None) is not None:
            return self._mixer.tts_load_seconds()
        return getattr(self, "_tts_queue_duration", 0.0)

    @tts_queue_duration.setter
    def tts_queue_duration(self, value: float) -> None:
        self._tts_queue_duration = value

    @property
    def stream_mode(self) -> bool:
        mc = self.bot.cogs.get('MusicCog')
        return mc.stream_mode if mc is not None else self._stream_mode_local

    @stream_mode.setter
    def stream_mode(self, value: bool) -> None:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            mc.stream_mode = value
        else:
            self._stream_mode_local = value

    @property
    def radio_mode(self) -> bool:
        mc = self.bot.cogs.get('MusicCog')
        return mc.radio_mode if mc is not None else self._radio_mode_local

    @radio_mode.setter
    def radio_mode(self, value: bool) -> None:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            mc.radio_mode = value
        else:
            self._radio_mode_local = value

    # ── Phase 2: stream subsystem proxy properties ────────────────────────────

    @property
    def stream_volume(self) -> float:
        mc = self.bot.cogs.get('MusicCog')
        return mc.stream_volume if mc is not None else self._stream_volume_local

    @stream_volume.setter
    def stream_volume(self, value: float) -> None:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            mc.stream_volume = value
        else:
            self._stream_volume_local = value
        # 即時套進 mixer：_mixer_play_music 的 100ms sync loop 可能提早結束（has_music
        # race）→ 按鈕/語音改音量只在下一首生效。這裡直接推 mixer，當前播放即時生效。
        # 只在 stream 活躍時套，避免誤蓋 radio_volume。
        mixer = getattr(self, '_mixer', None)
        if mixer is not None and mc is not None and getattr(mc, 'stream_mode', False):
            try:
                ng = mc._stream_norm_gain.get(getattr(mc, '_current_stream_url', None), 1.0)
                mixer.set_volume(value * ng)
            except Exception:
                pass

    @property
    def _stream_play_gen(self) -> int:
        mc = self.bot.cogs.get('MusicCog')
        return mc._stream_play_gen if mc is not None else self._stream_play_gen_local

    @_stream_play_gen.setter
    def _stream_play_gen(self, value: int) -> None:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            mc._stream_play_gen = value
        else:
            self._stream_play_gen_local = value

    @property
    def _current_stream_url(self):
        mc = self.bot.cogs.get('MusicCog')
        return mc._current_stream_url if mc is not None else self._current_stream_url_local

    @_current_stream_url.setter
    def _current_stream_url(self, value) -> None:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            mc._current_stream_url = value
        else:
            self._current_stream_url_local = value

    @property
    def _stream_norm_gain(self) -> dict:
        mc = self.bot.cogs.get('MusicCog')
        return mc._stream_norm_gain if mc is not None else self._stream_norm_gain_local

    @_stream_norm_gain.setter
    def _stream_norm_gain(self, value: dict) -> None:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            mc._stream_norm_gain = value
        else:
            self._stream_norm_gain_local = value

    @property
    def _last_user_song_seed(self):
        mc = self.bot.cogs.get('MusicCog')
        return mc._last_user_song_seed if mc is not None else self._last_user_song_seed_local

    @_last_user_song_seed.setter
    def _last_user_song_seed(self, value) -> None:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            mc._last_user_song_seed = value
        else:
            self._last_user_song_seed_local = value

    @property
    def stream_queue(self) -> list:
        mc = self.bot.cogs.get('MusicCog')
        return mc.stream_queue if mc is not None else self._stream_queue_local

    @stream_queue.setter
    def stream_queue(self, value: list) -> None:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            mc.stream_queue = value
        else:
            self._stream_queue_local = value

    @property
    def stream_task(self):
        mc = self.bot.cogs.get('MusicCog')
        return mc.stream_task if mc is not None else self._stream_task_local

    @stream_task.setter
    def stream_task(self, value) -> None:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            mc.stream_task = value
        else:
            self._stream_task_local = value

    @property
    def _current_stream_info(self):
        mc = self.bot.cogs.get('MusicCog')
        return mc._current_stream_info if mc is not None else self._current_stream_info_local

    @_current_stream_info.setter
    def _current_stream_info(self, value) -> None:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            mc._current_stream_info = value
        else:
            self._current_stream_info_local = value

    @property
    def stream_history(self) -> list:
        mc = self.bot.cogs.get('MusicCog')
        return mc.stream_history if mc is not None else self._stream_history_local

    @stream_history.setter
    def stream_history(self, value: list) -> None:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            mc.stream_history = value
        else:
            self._stream_history_local = value

    @property
    def stream_paused(self) -> bool:
        mc = self.bot.cogs.get('MusicCog')
        return mc.stream_paused if mc is not None else self._stream_paused_local

    @stream_paused.setter
    def stream_paused(self, value: bool) -> None:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            mc.stream_paused = value
        else:
            self._stream_paused_local = value

    @property
    def _current_lyrics(self):
        mc = self.bot.cogs.get('MusicCog')
        return mc._current_lyrics if mc is not None else self._current_lyrics_local

    @_current_lyrics.setter
    def _current_lyrics(self, value) -> None:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            mc._current_lyrics = value
        else:
            self._current_lyrics_local = value

    @property
    def _current_stream_comment(self):
        mc = self.bot.cogs.get('MusicCog')
        return mc._current_stream_comment if mc is not None else self._current_stream_comment_local

    @_current_stream_comment.setter
    def _current_stream_comment(self, value) -> None:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            mc._current_stream_comment = value
        else:
            self._current_stream_comment_local = value

    @property
    def _active_control_view(self):
        mc = self.bot.cogs.get('MusicCog')
        return mc._active_control_view if mc is not None else self._active_control_view_local

    @_active_control_view.setter
    def _active_control_view(self, value) -> None:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            mc._active_control_view = value
        else:
            self._active_control_view_local = value

    # ── Phase 3: radio subsystem proxy properties ─────────────────────────────

    @property
    def radio_task(self):
        mc = self.bot.cogs.get('MusicCog')
        return mc.radio_task if mc is not None else self._radio_task_local

    @radio_task.setter
    def radio_task(self, value) -> None:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            mc.radio_task = value
        else:
            self._radio_task_local = value

    @property
    def radio_volume(self) -> float:
        mc = self.bot.cogs.get('MusicCog')
        return mc.radio_volume if mc is not None else self._radio_volume_local

    @radio_volume.setter
    def radio_volume(self, value: float) -> None:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            mc.radio_volume = value
        else:
            self._radio_volume_local = value

    @property
    def _radio_song_list(self) -> list:
        mc = self.bot.cogs.get('MusicCog')
        return mc._radio_song_list if mc is not None else self._radio_song_list_local

    @_radio_song_list.setter
    def _radio_song_list(self, value: list) -> None:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            mc._radio_song_list = value
        else:
            self._radio_song_list_local = value

    @property
    def _radio_source(self):
        mc = self.bot.cogs.get('MusicCog')
        return mc._radio_source if mc is not None else self._radio_source_local

    @_radio_source.setter
    def _radio_source(self, value) -> None:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            mc._radio_source = value
        else:
            self._radio_source_local = value

    @property
    def _radio_fade_task(self):
        mc = self.bot.cogs.get('MusicCog')
        return mc._radio_fade_task if mc is not None else self._radio_fade_task_local

    @_radio_fade_task.setter
    def _radio_fade_task(self, value) -> None:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            mc._radio_fade_task = value
        else:
            self._radio_fade_task_local = value

    @property
    def radio_paused(self) -> bool:
        mc = self.bot.cogs.get('MusicCog')
        return mc.radio_paused if mc is not None else self._radio_paused_local

    @radio_paused.setter
    def radio_paused(self, value: bool) -> None:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            mc.radio_paused = value
        else:
            self._radio_paused_local = value

    # ── Phase 4: autoplay/recommendation proxy properties ────────────────────

    @property
    def _recommend_spotlight_idx(self) -> int:
        mc = self.bot.cogs.get('MusicCog')
        return mc._recommend_spotlight_idx if mc is not None else self._recommend_spotlight_idx_local

    @_recommend_spotlight_idx.setter
    def _recommend_spotlight_idx(self, value: int) -> None:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            mc._recommend_spotlight_idx = value
        else:
            self._recommend_spotlight_idx_local = value

    @property
    def _mood_sensor(self):
        mc = self.bot.cogs.get('MusicCog')
        return mc._mood_sensor if mc is not None else self._mood_sensor_local

    @_mood_sensor.setter
    def _mood_sensor(self, value) -> None:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            mc._mood_sensor = value
        else:
            self._mood_sensor_local = value

    @property
    def _cover_blacklist(self):
        mc = self.bot.cogs.get('MusicCog')
        return mc._cover_blacklist if mc is not None else self._cover_blacklist_local

    @_cover_blacklist.setter
    def _cover_blacklist(self, value) -> None:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            mc._cover_blacklist = value
        else:
            self._cover_blacklist_local = value

    @property
    def _round_track_count(self) -> int:
        mc = self.bot.cogs.get('MusicCog')
        return mc._round_track_count if mc is not None else self._round_track_count_local

    @_round_track_count.setter
    def _round_track_count(self, value: int) -> None:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            mc._round_track_count = value
        else:
            self._round_track_count_local = value

    @property
    def _round_size(self) -> int:
        mc = self.bot.cogs.get('MusicCog')
        return mc._round_size if mc is not None else self._round_size_local

    @_round_size.setter
    def _round_size(self, value: int) -> None:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            mc._round_size = value
        else:
            self._round_size_local = value

    @property
    def _prefetch_cache(self) -> dict:
        mc = self.bot.cogs.get('MusicCog')
        return mc._prefetch_cache if mc is not None else self._prefetch_cache_local

    @_prefetch_cache.setter
    def _prefetch_cache(self, value: dict) -> None:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            mc._prefetch_cache = value
        else:
            self._prefetch_cache_local = value

    @property
    def _last_search(self) -> dict:
        mc = self.bot.cogs.get('MusicCog')
        return mc._last_search if mc is not None else self._last_search_local

    @_last_search.setter
    def _last_search(self, value: dict) -> None:
        mc = self.bot.cogs.get('MusicCog')
        if mc is not None:
            mc._last_search = value
        else:
            self._last_search_local = value
