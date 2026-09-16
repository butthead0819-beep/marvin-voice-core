"""
MusicTailDJMixin — MusicCog 的歌曲 metadata 統籌預取 + PuckMixer 硬體橋接原語 +
DJ 尾段滑動窗串場排程（_run_tail_dj 是整條 DJ Tail 機制的核心）。

從 music_cog.py 抽出（減肥，比照 voice_controller.py 拆解先例），以 mixin 形式
併入 MusicCog：
    class MusicCog(..., MusicTailDJMixin, commands.Cog): ...
因此 self 仍是 MusicCog 實例，bot.tts_engine / bot.music_memory /
_fetch_lyrics_raw / _fetch_comment_raw / _fetch_dj_interjection_raw /
_fetch_lyrics_synced / _dj_requester_suffix / _dj_clean_name /
_preload_music_cache / _prefetch_cache / stream_queue 等全部沿用原本的
self 存取，行為零改動。

_get_puck_client() 是 music_cog.py 模組層級純函式，這裡在三處呼叫點各自
method-內 local import 取用，避免跟主檔互相 import 造成循環（同招
music_cog_subsystem.py 的 stop_stream 已用過）。

_DJ_TAIL_SFX_DIR / _DJ_TAIL_SFX_NAMES / _DJ_TAIL_LEAD_S /
_DJ_TAIL_SFX_PRELOAD_WAIT_S / _PUCK_STATUS_POLL_INTERVAL_S 這五個常數的
消費者全部都在這個檔案裡（搬離後主檔已無引用），跟著搬過來、主檔對應定義
一併移除。_NORM_GAIN_MEASURE_DELAY_S / _TASTE_PROFILE_CACHE 主檔（stream
loop/autopilot）也用得到，各自定義一份不搬移。
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import time

import discord

logger = logging.getLogger(__name__)

_TASTE_PROFILE_CACHE = "records/taste_profiles.json"
_NORM_GAIN_MEASURE_DELAY_S = 6.0
_DJ_TAIL_SFX_DIR = "assets/dj_sfx"
# 暫時關閉其他特效（riser, shoutout, dj_airhorn），100% 專注於 scratch 刷碟轉盤效果
_DJ_TAIL_SFX_NAMES = ("scratch",)
# 5s→8s：留更多餘裕給 _play_dj_tail_sfx 等下一首 preload 解碼完（見該處
# asyncio.wait_for），避免逼近歌1實際結束點才設 _dj_played_in_tail、跟主
# stream loop 換歌撞在一起（見 _run_tail_dj docstring）。
_DJ_TAIL_LEAD_S = 8.0
_DJ_TAIL_SFX_PRELOAD_WAIT_S = 2.0
# 輪詢 /puck/status 的間隔（_fire_puck_crossfade 用，兩種硬體共用）——resolve
# 現在多半是 cache 命中幾乎瞬間完成，1s 夠即時又不會洗爆 Pi 的 HTTP handler。
_PUCK_STATUS_POLL_INTERVAL_S = 1.0


class MusicTailDJMixin:
    @staticmethod
    def _ready_meta(prefetch_task) -> dict | None:
        """Play-First：只回傳『已就緒』的 prefetch meta；未就緒/失敗/非 dict → None。

        None 時 caller 先出聲、放棄本首 DJ、meta 背景補——不讓 LLM meta 生成阻塞出聲
        （Plan12 即時混音，DJ 本就疊在 ducked 音樂上，等它生成完才出聲沒意義）。
        """
        if prefetch_task is not None and prefetch_task.done():
            # prefetch task 被 cancel（LLM 全家掛掉、語音重連、skip 連打）時
            # .result() 會拋 CancelledError——它是 BaseException 不是 Exception，
            # 舊的 `except Exception` 接不住，會一路竄出 _stream_loop 被那邊的
            # `except asyncio.CancelledError` 誤判成「迴圈被要求停」→ stream_mode
            # 歸位 False、整條佇列收攤（2026-09-02 實測：一首歌就停）。子 task 的
            # 取消不是我們的取消，先擋掉再取值（對齊 _apply_bg_meta 的 .cancelled() 寫法）。
            if prefetch_task.cancelled():
                return None
            try:
                m = prefetch_task.result()
            except Exception:
                return None
            return m if isinstance(m, dict) else None
        return None

    def _compute_recommend_explanation(self, mm, cand) -> str | None:
        """算這次 autopilot 推薦要附的解釋（見 explanation_slotfill.py）。

        呼叫時機：**必須在 record_play() 之前**（見呼叫點 `_auto_recommend`）——
        record_play 一執行，這次播放就會被記進 plays[]，晚一步算 evidence 會把
        「現在正要播的這次」誤當成「你上次聽過」的證據，變成自我指涉的假解釋
        （2026-08-20 實測發現）。

        用 anchor_title 正規化比對找歷史紀錄，**不用 `mm._key(info)` 直接查**：
        同一首歌重新 yt-dlp 搜尋常常命中不同 webpage_url（同名不同上傳，
        music_memory.json 實測有 7 組重複標題、不同 key），照 key 查會找到一個
        全新的空白 entry，漏掉真正的收聽歷史。改用 normalize_title 比對，命中
        多筆同名 entry 時取 total_plays 最多的那個代表「這首歌」的歷史。
        """
        if not cand.lane or not hasattr(self.bot, 'music_memory'):
            return None
        try:
            from explanation_slotfill import TemplateRotationStore, generate_explanation
            from music_recommender import extract_evidence, extract_radio_related_evidence, normalize_title
            import taste_profile

            anchor_norm = normalize_title(cand.anchor_title)
            song = None
            if anchor_norm:
                matches = [
                    s for s in mm.all_songs().values()
                    if isinstance(s, dict) and normalize_title(s.get('title', '')) == anchor_norm
                ]
                if matches:
                    song = max(matches, key=lambda s: s.get('total_plays', 0))

            evidence = None
            if isinstance(song, dict):
                evidence = extract_evidence(song, cand)
                if evidence is not None and evidence.play_count == 0 and evidence.timestamp is None:
                    evidence = None  # 沒有真的聽過紀錄，別硬湊一句「你聽過」

            if evidence is None and cand.target_member:
                adjacent = taste_profile.fresh_adjacent_artists(_TASTE_PROFILE_CACHE, [cand.target_member], 8 * 86400)
                evidence = taste_profile.extract_discover_new_evidence(cand.anchor_artist, adjacent)

            if evidence is None and cand.lane == "discovery":
                evidence = extract_radio_related_evidence(cand)

            if evidence is None:
                return None
            return generate_explanation(evidence, store=TemplateRotationStore())
        except Exception as e:
            logger.debug(f"⚠️ [Explanation] 計算失敗，跳過本次解釋: {e}")
            return None

    async def _fetch_song_meta(self, info: dict) -> dict:
        """並行 fetch 歌詞、馬文評語、DJ 播報（含 TTS 預渲染）+ 長前奏跳過起點。

        推薦解釋不在這裡算——`_auto_recommend` 已在 record_play() 之前同步算好、
        存進 `info['_explanation']`（見 `_compute_recommend_explanation` docstring
        說明時機為何不能延後），這裡不用等、也不用重算。
        """
        lyrics, comment, dj, lyrics_synced = await asyncio.gather(
            self._fetch_lyrics_raw(info),
            self._fetch_comment_raw(info),
            self._fetch_dj_interjection_raw(info),
            self._fetch_lyrics_synced(info),
            return_exceptions=True,
        )
        # 🎬 [IntroSkip] YouTube 熱力圖已挑過精華起點就不覆蓋；沒有才用 LRC 補長前奏跳過
        # （見 music_intro_skipper.pick_intro_skip_start）。原地改 info——跟 stream_queue
        # 裡 popleft 出去要播的是同一個 dict 物件，播放端 play_stream_song/_start_music_preload
        # 已原生吃 highlight_start_s，這裡填了就自動生效，不用額外改播放路徑。
        if not info.get('highlight_start_s') and not info.get('voice_request') and isinstance(lyrics_synced, str):
            from music_intro_skipper import pick_intro_skip_start
            intro_start = pick_intro_skip_start(lyrics_synced, info.get('duration'))
            if intro_start is not None:
                info['highlight_start_s'] = intro_start
                logger.info(f"🎬 [IntroSkip] {info.get('title', '?')} 前奏跳過起播點 {intro_start:.1f}s")
        return {
            'lyrics': lyrics if isinstance(lyrics, str) else None,
            'comment': comment if isinstance(comment, str) else None,
            'dj': dj if isinstance(dj, dict) else None,
        }

    async def _meta_with_ack_fallback(self, info: dict, requested_by: str) -> dict:
        """冷啟動 meta fetch + 5s timeout fallback。"""
        try:
            return await asyncio.wait_for(
                self._fetch_song_meta(info),
                timeout=self._COLD_META_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            title = info.get('title', '未知曲目')
            logger.warning(
                f"⚠️ [Stream] _fetch_song_meta >{self._COLD_META_TIMEOUT_S}s timeout, "
                f"用 hardcoded fallback (song={title}, by={requested_by})"
            )
            suffix = self._dj_requester_suffix(requested_by)
            return {
                "lyrics": None,
                "comment": None,
                "dj": {
                    "text": f"下一首是《{title}》，{suffix}。",
                    "audio_path": None,
                },
            }

    async def _speak_song_ack(self, vc, title: str) -> None:
        """語音點歌第三個Ack：合成後直推 TTS 層（同 _play_ack 路徑），不走 play_tts 的
        Silence Gate/Interrupt Guard，才不會被聊天室裡持續講話的其他人擋掉。"""
        try:
            audio_path = await self.bot.tts_engine.generate_audio(f"幫你點了《{title}》")
        except Exception as e:
            logger.warning(f"⚠️ [第三個Ack] TTS 生成失敗，跳過報歌名: {e}")
            return
        if not audio_path:
            return
        try:
            await vc.play_dj_on_tts_layer(audio_path)
        except Exception as e:
            logger.warning(f"⚠️ [第三個Ack] 推播失敗: {e}")

    async def _fire_puck_play(self, puck_client, url: str, title: str = None,
                               highlight_start_s: float = None, duration: float = None) -> None:
        """[PuckMixer] esp32_edge_mix 專用硬 play：沒有 standby deck 可 crossfade 接手時
        （開場第一首/skip/上一首無尾段task）用這個讓 ESP32 從乾淨狀態開播（見
        car_puck.ino dispatchNewCommands 的 play 分支：兩個 deck 都停、deck0 接新
        URL）。fire-and-forget，失敗只記警告，不影響本地 Discord/家用播放路徑。

        title/highlight_start_s/duration：ESP32 的 PuckCommandQueueClient 目前忽略
        這幾個欄位，接受它們只是跟其他 client 維持同款介面（見
        marvin_voice_core/puck_command_queue.py::PuckCommandQueueClient.play）。"""
        ok = await puck_client.play(url, title=title, seek=highlight_start_s, duration=duration)
        if not ok:
            logger.warning(f"[PuckMixer] play 失敗: {url}")

    async def _fire_puck_stop(self, puck_client) -> None:
        """[PuckMixer] esp32_edge_mix 專用。2026-08-17 實機踩到：stop_stream() 原本
        從沒通知裝置端，Mac 說「停止播放」後 stream_mode 歸位，但裝置端狀態沒同步，
        下次送新歌時容易殘留舊狀態。裝置端通知是盡力而為，失敗不擋 Mac 端本身的
        停播流程。"""
        try:
            ok = await puck_client.stop()
            if not ok:
                logger.warning("[PuckMixer] stop 失敗")
        except Exception as e:
            logger.warning(f"[PuckMixer] stop 呼叫例外: {e}")

    async def _fire_puck_speak(self, puck_client, audio_path: str) -> None:
        """[PuckMixer Phase3] DJ 口白：ESP32 端會 duck 音樂再疊播（見
        car_puck.ino::mixOutputTask 的 VOICE_DUCK_GAIN）。esp32_edge_mix 專用
        （送 Mac 本機預渲染音檔路徑，ESP32 pull 播放）。"""
        ok = await puck_client.speak(audio_path)
        if not ok:
            logger.warning(f"[PuckMixer] speak 失敗: {audio_path}")

    async def _fire_puck_sfx(self, puck_client, audio_path: str) -> None:
        """[PuckMixer Phase3] 轉場音效：不 duck，直接疊播。"""
        ok = await puck_client.sfx(audio_path)
        if not ok:
            logger.warning(f"[PuckMixer] sfx 失敗: {audio_path}")

    async def _fire_puck_crossfade(self, puck_client, next_url: str,
                                    buffer_s: float = 4.0, crossfade_s: float = 4.0,
                                    title: str = None) -> bool:
        """[PuckMixer] 純音樂 crossfade：queue_next 後留 buffer_s 給裝置端背景
        ffmpeg 起手緩衝，再送 crossfade。跟本地 Discord mixer/DJ 口白邏輯
        完全獨立（見呼叫點 _run_tail_dj），queue_next 失敗就放棄、不重試（下一輪
        tail-fire 或下一首開頭會再給機會）。回傳裝置端是否真的接手了下一首
        （queue_next 或 crossfade 任一步失敗都是 False）。esp32_edge_mix 專用——

        2026-08-20：pi_bt（Pi Zero 2W 車 puck）換歌決策/DJ口白改回跟家用喇叭共用
        同一顆 mixer（見 main_satellite.py::setup_satellite 的 TeeSpeakerOutput +
        /audio_stream「收音機」模式說明），不再需要 Mac 送 play/queue_next/crossfade
        指令，這支函式跟 pi_bt 完全脫鉤，只剩 esp32_edge_mix 會呼叫。

        buffer_s 2.0→4.0（2026-08-11）：esp32_edge_mix 實機驗證，2.0s 對 ESP32 的
        /puck_deck 鏈路（Mac resolve+ffmpeg轉碼+MP3編碼+網路傳輸)不夠，crossfade
        觸發時 standby deck 常常還沒緩衝夠，混音瞬間出現真的靜音空白。

        ⚠️ 2026-08-17：這裡收到的 buffer_s 上限由呼叫端決定的觸發窗口決定——
        esp32_edge_mix 走 _run_tail_dj 內建的 _DJ_TAIL_LEAD_S(=8.0)s 窗口，不能
        逼近甚至超過它。

        ⚠️ 2026-08-18：pi_bt 接上 YouTube cookies 後，resolve 常要吃到 ~24s CPU
        time（deno 解 JS challenge，見 puck_mixer.py::resolve_stream_url()
        docstring），遠超原本假設的 ~7s。固定 sleep(buffer_s) 賭一個時長不管用——
        猜太短會在 deck_b 還沒 ready 時打 /puck/crossfade，Pi 端 raise
        RuntimeError（deck_b is None）被吞掉、这次转场直接放弃、当前曲播完只剩靜音；
        猜太長又浪費窗口。改成輪詢 /puck/status 的 next_queued 是否已等於
        next_url，ready 就提早出手，buffer_s 退化成「polling 的上限」，esp32_edge_mix
        的 client 沒有 status() 保留舊的固定 sleep 行為不變（hasattr 分辨，同
        speak/speak_text 的既有 pattern）。"""
        ok = await puck_client.queue_next(next_url, title=title)
        if not ok:
            logger.warning(f"[PuckMixer] queue_next 失敗，放棄本次 crossfade: {next_url}")
            return False
        if hasattr(puck_client, "status"):
            # 2026-08-19：狀態驅動到底——輪詢逾時代表裝置端還沒真的 ready，
            # 直接放棄這次 crossfade，不要賭一把硬打（那個賭注就是「花田錯
            # 提早結束 20s 空白」的根因：逼近真正歌曲結尾時 Pi 端 deck_b 常常
            # 還沒好，crossfade() 丟 RuntimeError 失敗，反而比乾脆不打還慢）。
            # 放棄後回傳 False，呼叫端（_run_tail_dj）不會標記 _dj_played_in_tail，
            # 下一首開頭走 _fire_puck_play 的既有硬 play 回退路徑（見該函式）。
            deadline = time.time() + buffer_s
            ready = False
            while time.time() < deadline:
                await asyncio.sleep(_PUCK_STATUS_POLL_INTERVAL_S)
                st = await puck_client.status()
                if st is not None and st.get("next_queued") == next_url:
                    ready = True
                    break
            if not ready:
                logger.warning(f"[PuckMixer] queue_next 逾時仍未就緒，放棄本次 crossfade（交給下一首開頭補 play）: {next_url}")
                return False
        else:
            await asyncio.sleep(buffer_s)
        crossfaded = await puck_client.crossfade(crossfade_s)
        if not crossfaded:
            logger.warning(f"[PuckMixer] crossfade 失敗（deck_b 可能還沒 ready）: {next_url}")
        return bool(crossfaded)

    async def _run_tail_dj(self, cur_info: dict, song_start_time):
        """[DJ Tail] 滑動窗串場：當前歌結束前 _DJ_TAIL_LEAD_S 秒點火，DJ 疊當前歌尾巴 + 溢進下一首開頭。

        關鍵：點火時刻只依「當前歌 duration」算（開播即可知），**下一首在點火當下
        才從 stream_queue[0] 抓**——因為 autopilot 常在播放中才把下一首排入 queue，
        開播時綁定會抓到空的。DJ 掛 mixer TTS 層（與 _music 獨立），set_music_source
        換歌不中斷 DJ、音樂持續 duck → DJ 自然橫跨切歌點。

        任何無法安全派發的情境（duration 未知/歌太短/已過窗、點火時沒有下一首、
        下一首無預渲染 audio、被 skip、私語模式）一律 return，讓下一首走舊路
        （混進開頭 or _maybe_play_dj_interjection）。

        song_start_time：float（已知起播時間戳，測試/相容用）或 asyncio.Future（真正
        出聲那刻才 set_result，見 play_stream_song/_mixer_play_music 的 started_future）。
        傳 Future 才準——call 這個函式時歌其實還沒出聲（highlight_start_s 的網路 seek
        +整首解碼都要花時間），拿「排 task 那刻」的時間戳當基準會讓 elapsed 系統性偏大、
        尾段提早點火（見 project_dj_tail_seek_latency）。Future 若中途被取消（歌提早結束/
        skip）視同「等不到」，退回舊行為。
        """
        from dj_tail_schedule import tail_dj_fire_delay

        title_cur = cur_info.get('title', '?')

        duration = cur_info.get('duration')
        if not duration:
            logger.info(f"[DJ Tail] {title_cur} duration 未知，退回舊行為")
            return
        # 精華起播（highlight_start_s）讓實際播放時間軸位移了一截——elapsed 是從
        # 「起播那秒」算起，尾段點火要抓的是「離實際結束還有多久」，duration 要跟著扣掉
        # 位移，否則會算成離結尾還很久（其實早就快撥完了），點火時間表全錯。
        if cur_info.get('highlight_start_s'):
            duration = max(0.0, duration - cur_info['highlight_start_s'])

        if isinstance(song_start_time, asyncio.Future):
            try:
                real_start = await song_start_time
            except asyncio.CancelledError:
                logger.info(f"[DJ Tail] {title_cur} 等真正出聲前被取消，退回舊行為")
                return
        else:
            real_start = song_start_time

        elapsed = time.time() - real_start
        # 滑動窗：當前歌結束前 _DJ_TAIL_LEAD_S 秒點火，DJ（~15s）疊尾巴 + 溢進下一首開頭。
        delay = tail_dj_fire_delay(duration, elapsed, lead_s=_DJ_TAIL_LEAD_S)
        if delay is None:
            logger.info(f"[DJ Tail] {title_cur} 過窗或歌太短，退回舊行為")
            return

        logger.info(f"[DJ Tail] {title_cur} 尾段點火倒數 {delay:.1f}s")
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            logger.info(f"[DJ Tail] {title_cur} 尾段 task 被取消（skip/stop）")
            return

        # re-check：歌仍在播、沒被 skip、仍是同一首
        if not self.stream_mode:
            logger.info(f"[DJ Tail] {title_cur} stream 已停，不派發")
            return
        if getattr(self, '_current_song_skipped', False):
            logger.info(f"[DJ Tail] {title_cur} 已被 skip，不派發")
            return
        if self._current_stream_info is not cur_info:
            logger.info(f"[DJ Tail] {title_cur} 歌已切換，不派發")
            return

        # 點火當下才抓下一首（此時 autopilot 幾乎必定已排入 queue）
        next_info = self.stream_queue[0] if self.stream_queue else None
        if next_info is None:
            logger.info(f"[DJ Tail] {title_cur} 點火時 queue 仍空、無下一首，退回舊行為")
            return
        title_next = next_info.get('title', '?')

        # [PuckMixer] esp32_edge_mix 專用：額外送純音樂 crossfade 訊號給裝置端，跟下面
        # 本地 Discord mixer 的 DJ 口白邏輯完全獨立、不共用旗標、不影響其他硬體行為
        # （DJ 口白走另一條未實作的 TTS 串流管線，這裡只管換歌）。fire-and-forget
        # 背景 task，不阻塞/不改變既有 flow 的時序。pi_bt（車 puck Pi Zero 2W）
        # 2026-08-20 起不再呼叫這裡——換歌決策/DJ口白改回跟家用喇叭共用同一顆 mixer
        # （見 main_satellite.py::setup_satellite 的 TeeSpeakerOutput + /audio_stream
        # 說明），_get_puck_client() 對 pi_bt 回 None，下面這段自然被跳過。
        #
        # ⚠️ 2026-08-11 實機踩到：這裡一定要用 webpage_url（可重新 yt-dlp resolve 的
        # youtube 頁面網址），不能用 'url'——後者是 _resolve_yt_query() 當下呼叫 yt-dlp
        # 解出來、已經是 googlevideo CDN 的最終直連網址（見該函式 return dict）。
        # esp32_edge_mix 收到 webpage_url 後靠 /puck_deck 端點在 Mac 端重新
        # resolve（main_satellite.py::handle_puck_deck），餵一個已經是 CDN 網址
        # 的字串進去再 resolve 一次 100% 失敗（實機驗證：ESP32 /puck_deck 穩定
        # 回 502）。
        from cogs.music_cog import _get_puck_client
        puck_client = _get_puck_client()
        next_url = next_info.get('webpage_url', '')
        if puck_client is not None and next_url:
            asyncio.create_task(
                self._fire_puck_crossfade(puck_client, next_url, title=next_info.get('title'))
            )

        # 2026-08-14：preload 只跟「下一首歌本身」有關，不該綁在 DJ 口白是否成功
        # 預渲染上——DJ meta 拿不到時（生成失敗/逾時/quick 模式不講話）以前會直接
        # return 導致這裡從沒被呼叫，切歌當下退回同步整首解碼，造成聽得到的等待。
        # 提前到 dj_meta 判斷之前，確保退回舊行為時下一首依然有機會提前解碼好。
        self._start_music_preload(next_info)

        dj_meta = await self._resolve_tail_dj_meta(next_info, cur_info=cur_info)
        if dj_meta is None:
            logger.info(f"[DJ Tail] {title_next} 無可用預渲染 DJ，退回舊行為")
            return

        logger.info(f"[DJ Tail] 點火！疊播 {title_next} 的 DJ 在 {title_cur} 尾段")
        # 2026-07-25：跟 DJ 開場白同時，背景先把下一首整首解碼好（preload_f32_source
        # 消除 mixer 中段爆音的代價是換源前要等整首解碼完；不先做，這段延遲就會落在
        # 「DJ 開場白講完」跟「下一首出聲」中間，變成聽得到的中斷）。DJ 開場白＋尾段疊播
        # 還有 ~_DJ_TAIL_LEAD_S 秒窗口，剛好夠蓋掉解碼時間。
        await self._maybe_play_dj_interjection(dj_meta)
        await self._play_dj_tail_sfx(next_info)
        next_info['_dj_played_in_tail'] = True
        logger.info(f"[DJ Tail] {title_next} 已標記 _dj_played_in_tail=True")


    def _start_music_preload(self, info: dict) -> None:
        """[DJ Tail] 背景預解碼下一首整首音樂進記憶體，供 play_stream_song 換源時直接用
        （見 _resolve_music_source）。idempotent：同一 url 不重複起 task。

        cache 最多留 2 個未被領取的 task——一首完整解碼是幾十 MB，正常路徑幾秒內就會被
        play_stream_song 領走清掉，這裡只是防呆（例如點火後又被 skip，task 沒人領走）。
        """
        url = info.get('url', '')
        if not url or url in self._preload_music_cache:
            return
        while len(self._preload_music_cache) >= 2:
            _stale_url, stale_task = self._preload_music_cache.popitem()
            stale_task.cancel()

        highlight = info.get('highlight_start_s')

        async def _do():
            from local_mixing_source import preload_f32_source
            p12_opts = {
                'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -probesize 32M',
                'options': '-vn -bufsize 512k',
            }
            if highlight:
                # -ss 放在 -i 前（input seeking），ffmpeg 用容器索引快跳，不必解碼到那秒。
                p12_opts['before_options'] = f'-ss {highlight:.2f} ' + p12_opts['before_options']
            s16 = discord.FFmpegPCMAudio(url, **p12_opts)
            return await asyncio.to_thread(preload_f32_source, s16)

        self._preload_music_cache[url] = asyncio.create_task(_do())
        norm_gains = getattr(self, "_stream_norm_gain", None)
        measure_fn = getattr(self, "_measure_norm_gain_bg", None)
        if norm_gains is not None and url not in norm_gains and measure_fn is not None:
            asyncio.create_task(measure_fn(
                url,
                duration=float(info.get('duration') or 0),
                highlight_start_s=highlight,
                info=info,
                delay_s=_NORM_GAIN_MEASURE_DELAY_S,
            ))

    async def _resolve_music_source(self, url: str, ffmpeg_factory):
        """回傳 (preloaded, fresh_s16_source)——恰好一個非 None。

        cache 有這個 url 的預解碼 task（完成或進行中皆可，await 等它）就用，領走後從 cache
        清掉；沒有、或預解碼失敗（例如網路斷）→ 退回現場用 ffmpeg_factory() 建全新 s16 音源
        （跟修這個之前的行為一致，不會因為預解碼失敗就播不出來）。
        """
        preload_task = self._preload_music_cache.pop(url, None)
        if preload_task is not None:
            try:
                source = await preload_task
                logger.info("[DJ Tail] 換源命中預解碼，零等待")
                return source, None
            except Exception as e:
                logger.info(f"[DJ Tail] 預解碼失敗，退回現場解碼: {e}")
        return None, ffmpeg_factory()

    async def _resolve_tail_dj_meta(self, next_info: dict, cur_info: dict = None) -> dict | None:
        """取下一首已預渲染的 DJ meta（有 audio 檔才回）；不可用回 None（退回舊路）。

        下一首若還沒 prefetch（autopilot 較晚排入 queue）→ 現場補建一個並存回 cache，
        供後續 loop 複用（不重複 fetch）。這樣點火時一定拿得到 DJ、不會白白退回開頭。
        """
        url = next_info.get('url', '')
        prefetch_task = self._prefetch_cache.get(url)
        if prefetch_task is None:
            prefetch_task = asyncio.create_task(self._fetch_song_meta(next_info))
            if url:
                self._prefetch_cache[url] = prefetch_task
        try:
            meta = await prefetch_task
        except asyncio.CancelledError:
            return None
        except Exception as e:
            logger.info(f"[DJ Tail] prefetch 失敗: {e}")
            return None
        if not isinstance(meta, dict):
            return None
        dj_meta = meta.get('dj')
        if not isinstance(dj_meta, dict):
            return None

        # 🛡️ [Consistency Guard] 檢查 DJ 口白所用的 prev_title 與當前實際結束的歌名是否吻合
        if cur_info is not None:
            prev_used = dj_meta.get('prev_title_used')
            actual_prev = cur_info.get('title', '')
            if prev_used and actual_prev:
                from song_name_clean import clean_title_regex
                norm_used = clean_title_regex(prev_used).strip().lower()
                norm_actual = clean_title_regex(actual_prev).strip().lower()
                if norm_used and norm_actual and norm_used != norm_actual:
                    logger.warning(
                        f"🛡️ [DJ Tail Consistency Guard] 預期上一首《{prev_used}》與實際《{actual_prev}》不符"
                        f"（佇列可能被插播/skip），放棄過期音檔以防報錯歌名"
                    )
                    clean_title, clean_artist = self._dj_clean_name(next_info)
                    req_suffix = self._dj_requester_suffix(next_info.get('requested_by', ''))
                    if clean_artist:
                        safe_text = f"DJ Marvin為你帶來{clean_artist}演唱的{clean_title}，{req_suffix}"
                    else:
                        safe_text = f"DJ Marvin為你帶來《{clean_title}》，{req_suffix}"
                    safe_audio = None
                    try:
                        safe_audio = await self.bot.tts_engine.generate_audio(safe_text, emotion="normal")
                    except Exception as e:
                        logger.debug(f"[DJ Tail] safe fallback TTS 失敗: {e}")
                    if safe_audio and os.path.exists(safe_audio):
                        return {
                            'text': safe_text,
                            'audio_path': safe_audio,
                            'prev_title_used': None,
                        }
                    return None

        audio_path = dj_meta.get('audio_path')
        if not audio_path or not os.path.exists(audio_path):
            return None
        return dj_meta

    async def _play_tail_dj_after_skip(self, next_info: dict) -> None:
        """手動 skip 後背景解析/播放 DJ 串場，逾時或出錯都不影響已經生效的 skip。"""
        try:
            timeout_s = self._SEAMLESS_SKIP_TIMEOUT_S
            try:
                dj_meta = await asyncio.wait_for(
                    self._resolve_tail_dj_meta(next_info, cur_info=self._current_stream_info),
                    timeout=timeout_s,
                )
            except asyncio.TimeoutError:
                logger.warning(f"⚠️ [Seamless Skip] DJ meta 背景解析逾時 >{timeout_s}s，放棄串場")
                return

            if dj_meta is not None:
                next_info['_dj_played_in_tail'] = True
                await self._maybe_play_dj_interjection(dj_meta)
        except Exception as e:
            logger.warning(f"⚠️ [Seamless Skip] 背景 DJ 串場出錯: {e}")

    async def _maybe_play_dj_interjection(self, dj: dict | None):
        """播放預先生成的 DJ 播報。有預渲染音訊則直接播檔案，否則即時串流。"""
        if not dj:
            return
        text = dj.get('text', '')
        audio_path = dj.get('audio_path')
        if not text:
            return

        vc = self._vc()
        if vc is None:
            logger.info("[DJ Tail] 口白：找不到 VoiceController cog（_vc()→None），這輪不放")
            return
        # 私語模式：聽>>講，不主動唸 DJ 播報（autopilot 與今夜歌單共用此路）
        if getattr(vc, '_intimate_mode', False):
            logger.info("[DJ Tail] 口白：_intimate_mode=True，這輪不放")
            return
        vc._tts_protected = True
        try:
            if audio_path and os.path.exists(audio_path):
                # 尾段 DJ：走 TTS 層（duck 音樂、非阻塞、撐過歌1→歌2 換源）。
                # 不可用 play_local_file——那條把檔案設成音樂層來源會替換掉正在播的歌，
                # DJ 只播到切歌點就被下一首蓋掉（使用者實測「只聽到狗與露就停」）。
                await vc.play_dj_on_tts_layer(audio_path)
                # [PuckMixer] vc.play_dj_on_tts_layer 疊的 DJ 口白出現在這個進程自己的
                # mixer 輸出——pi_bt（車 puck Pi Zero 2W）2026-08-20 起也接進同一顆
                # mixer（見 main_satellite.py::setup_satellite 的 TeeSpeakerOutput 說明），
                # DJ 口白自然隨 /audio_stream 一起播到車上，不用另外傳。esp32_edge_mix
                # 仍是獨立通道（送 Mac 本機預渲染音檔路徑，ESP32 pull 播放，見
                # car_puck.ino 的 speak 分支）——只有 audio_path 有預渲染檔的情況才送，
                # 即時 TTS（else 分支）沒有檔案/固定文字可傳，這裡先不接。
                from cogs.music_cog import _get_puck_client
                puck_client = _get_puck_client()
                if puck_client is not None and hasattr(puck_client, "speak"):
                    asyncio.create_task(self._fire_puck_speak(puck_client, audio_path))
            else:
                await vc.play_tts(text, already_in_channel=True)
        finally:
            vc._tts_protected = False

    async def _synthesize_dynamic_scratch(self, next_info: dict) -> str | None:
        """抓下一首已預解碼的 PCM、即時合成專屬該曲的黑膠刷碟聲。抓不到/沒 ready/合成
        失敗一律回 None——刻意不留靜態備用檔，交給呼叫端直接放棄這輪 SFX（見
        _play_dj_tail_sfx：沒有 fallback 音效，播不出來就是這輪真的沒抓到 PCM，訊號
        要乾淨，別用預錄音檔混過去）。

        preload 是背景整首解碼，點火當下十之八九還沒好——與其一次性 done() 檢查
        （幾乎必定 miss），改用 wait_for 主動等一小段（在 _DJ_TAIL_LEAD_S 的窗口內
        仍有餘裕），拉高真正用上真實 PCM 的機率。asyncio.shield：等待逾時只放棄
        「這次用它」，不能連 preload task 本身也砍掉——它還要留給 _resolve_music_source
        換源時用。
        """
        url = next_info.get('url', '')
        preload_task = self._preload_music_cache.get(url)
        if preload_task is None or preload_task.cancelled():
            return None

        try:
            preloaded = await asyncio.wait_for(
                asyncio.shield(preload_task), timeout=_DJ_TAIL_SFX_PRELOAD_WAIT_S,
            )
        except (asyncio.TimeoutError, asyncio.CancelledError):
            return None
        except Exception as e:
            logger.debug(f"[DJ Tail] preload 讀取失敗: {e}")
            return None

        try:
            frames = getattr(preloaded, '_frames', None)
            if not frames or len(frames) < 50:
                return None
            import hashlib
            import numpy as np
            from bpm_estimate import estimate_bpm_from_pcm
            from scripts.gen_dj_sfx import gen_scratch_from_pcm, _write_wav
            raw_bytes = b"".join(frames[:100])
            raw_f32 = np.frombuffer(raw_bytes, dtype=np.float32).reshape(-1, 2)
            # 用同一段預解碼 PCM 順手估下一首 BPM，餵給刷碟合成拆成半分/三連/四分
            # 節奏的多段手勢——沒抓到 BPM（太安靜/太短）就退回舊的單段隨機手法。
            next_bpm = estimate_bpm_from_pcm(raw_f32.mean(axis=-1).astype(np.float32), 48000)
            dynamic_samples = gen_scratch_from_pcm(raw_f32, rate=48000, bpm=next_bpm)
            # 固定檔名在多首歌同時點火時會互踩（寫入中被下一次點火覆蓋/搶讀半寫檔），
            # 用 url hash 隔開。
            url_hash = hashlib.md5(url.encode()).hexdigest()[:10]
            dynamic_path = f"/tmp/scratch_dynamic_{url_hash}.wav"
            _write_wav(dynamic_path, dynamic_samples)
            return dynamic_path
        except Exception as e:
            logger.debug(f"[DJ Tail] 動態 scratch 合成失敗: {e}")
            return None

    async def _play_dj_tail_sfx(self, next_info: dict | None = None):
        """[DJ Tail] DJ 口白播完後，隨機疊一支轉場音效（riser 合成自
        scripts/gen_dj_sfx.py；dj_airhorn 方波太刺耳已從輪替池移除，函式仍留著給
        gen_dj_sfx.py 素材產出用；shoutout 是 edge-tts 用 Marvin 現役聲線 zh-TW-YunJheNeural
        rate=-20% pitch=-15Hz 錄的「Yo，DJ Maaaarvinnnnn！」拉長音報名 stamp；scratch 是即時抓下一首
        PCM 合成的動態黑膠刷碟聲）進 TTS 層——跟口白同一條佇列接續播出，落在尾段疊播
        溢進下一首開頭的窗口內。

        scratch 沒有靜態備用檔：抓不到下一首 PCM 就這輪不放，不用預錄音檔頂替——
        「這次沒聽到刷碟聲」本身就是訊號，別讓 fallback 把失敗蓋掉。找不到 vc 就靜靜
        放棄，不影響主流程。

        2026-08-25 暫時停用：SFX 疊播（scratch 動態合成 + ffmpeg fork）跟換歌本身的
        preload/口白 fork 疊在同一個窗口，是 Discord 語音斷續的可疑根因之一（見
        feedback_diagnose_timing_vs_cpu_dropout 同類診斷）。要復原刪掉這個 return 即可。
        """
        return
        vc = self._vc()
        if vc is None:
            logger.info("[DJ Tail] SFX：找不到 VoiceController cog（_vc()→None），這輪不放")
            return
        name = random.choice(_DJ_TAIL_SFX_NAMES)

        if name == "scratch":
            path = await self._synthesize_dynamic_scratch(next_info) if next_info else None
            if path is None:
                logger.info("[DJ Tail] SFX：scratch 抽中但沒抓到下一首 PCM，這輪不放")
                return
            logger.info("[DJ Tail] SFX：scratch（動態合成）")
        else:
            path = os.path.join(_DJ_TAIL_SFX_DIR, f"{name}.wav")
            if not os.path.exists(path):
                return
            logger.info(f"[DJ Tail] SFX：{name}")

        try:
            # 轉場音效不是講話，音量比照音樂 10% 感受，別用口白的滿幅正規化（太搶戲）。
            await vc.play_dj_on_tts_layer(path, peak=0.1)
        except Exception as e:
            logger.debug(f"⚠️ [DJ Tail] SFX 疊播失敗（不影響主流程）: {e}")

        # [PuckMixer Phase3] 比照 _maybe_play_dj_interjection：Discord/家用混音走 vc
        # 自己的輸出，esp32_edge_mix 要另外送一份給裝置端（不 duck，見 car_puck.ino
        # 的 sfx 分支）。
        from cogs.music_cog import _get_puck_client
        puck_client = _get_puck_client()
        if puck_client is not None and hasattr(puck_client, "sfx"):
            asyncio.create_task(self._fire_puck_sfx(puck_client, path))


