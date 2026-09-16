"""
MusicPersonalShuffleMixin — MusicCog 的個人歌單連續隨機播、絕境回收安全網、
歌曲卡/HUD 橋接發布方法。

從 music_cog.py 抽出（減肥，比照 voice_controller.py 拆解先例），以 mixin 形式
併入 MusicCog：
    class MusicCog(..., MusicPersonalShuffleMixin, ..., commands.Cog): ...
因此 self 仍是 MusicCog 實例，_vc / _stream_loop / _cancel_stream_task /
_resolve_yt_query / _check_song_duplicate / _fetch_lyrics_control_card 等
全部沿用原本的 self 存取，行為零改動。
"""
from __future__ import annotations

import asyncio
import logging
import os

import discord

from music_memory import extract_video_id

logger = logging.getLogger(__name__)


class MusicPersonalShuffleMixin:
    # ── 🎲 個人歌單連續隨機播 ────────────────────────────────────────────────

    async def start_personal_shuffle(self, username: str) -> tuple[bool, str]:
        """連續隨機播放某使用者點過的『全部』歌（不重複、播完為止）。

        一次只墊一首待播（見 _personal_shuffle_topup），不塞爆佇列，別人現場點歌照樣
        進得來。池子＝music_memory 裡 requesters 含該使用者的所有歌，純隨機洗牌。
        """
        mm = getattr(self.bot, 'music_memory', None)
        if mm is None:
            return (False, "音樂記憶尚未就緒。")
        pool = [s for s in mm.all_songs().values()
                if username in (s.get("requesters") or {})]
        if not pool:
            return (False, f"{username} 還沒點過任何歌，沒有歌單可以播。")
        import random
        random.shuffle(pool)
        self._personal_shuffle = {"user": username, "remaining": pool}
        logger.warning(f"🎲 [PersonalShuffle] start user={username} pool={len(pool)} stream_mode={self.stream_mode}")
        await self._personal_shuffle_topup()
        if not self.stream_mode:
            self.stream_mode = True
            self.stream_volume = self._default_stream_volume
            self._stream_user_stopped = False
            if self.stream_task and not self.stream_task.done():
                self._cancel_stream_task("start_personal_shuffle")
            self.stream_task = asyncio.create_task(self._stream_loop())
        msg = f"🎲 開始連續隨機播放 {username} 的歌單（{len(pool)} 首，播完為止、不重複）。"
        vc = self._vc()
        ch = vc.active_text_channel if vc is not None else None
        if ch is not None:
            try:
                await ch.send(msg)
            except Exception:
                pass
        return (True, msg)

    def stop_personal_shuffle(self) -> bool:
        """關掉個人歌單連續播，並清掉佇列裡還沒播的個人墊位 → 下一首立刻回一般推薦／
        主題歌單（補位邏輯看 _personal_shuffle is None 即走 _auto_recommend）。

        回傳先前是否在進行中。當前正在播的那首（已 pop 出佇列）會自然播完。
        """
        was = self._personal_shuffle is not None
        self._personal_shuffle = None
        self.stream_queue[:] = [it for it in self.stream_queue if it.get("_lane") != "personal"]
        self._republish_queue_snapshot()
        return was

    def _personal_shuffle_pending(self) -> bool:
        """佇列裡是否已有一首個人歌單待播歌（保證一次只墊一首）。"""
        return any(it.get("_lane") == "personal" for it in self.stream_queue)

    async def _personal_shuffle_topup(self) -> bool:
        """個人歌單補位：佇列尾墊『一首』他的歌。

        已有待播個人歌 → 不補（回 True）。池空 → 收掉 session、回退一般推薦（回 False）。
        成功墊一首 → 回 True。
        """
        sess = self._personal_shuffle
        if not sess:
            return False
        # 無連線語音（被 dismiss/撤離）→ 結束 session，別讓 stream loop 一直 churn 解析+跳過。
        # 多條離開語音路徑不一定都有清 session，這裡當總關（2026-06-29 死鎖事故相鄰根因）。
        if not any(v.is_connected() for v in self.bot.voice_clients):
            logger.warning(f"🎲 [PersonalShuffle] 無連線語音，結束 {sess['user']} 的個人歌單 session。")
            self._personal_shuffle = None
            return False
        # 單飛守衛：stream loop 的 <2 分支會 fire-and-forget 噴多個 topup task；pending
        # 檢查與 append 之間隔著慢 resolve（log 滿滿 >5s timeout），併發的兩個 topup 會同時
        # 通過檢查各塞一首 → 兩首搶播。inflight 旗標在第一個 await 前同步設好，後到的直接退。
        if self._personal_topup_inflight:
            return True
        if self._personal_shuffle_pending():
            return True
        self._personal_topup_inflight = True
        user = sess["user"]
        try:
            while sess["remaining"]:
                song = sess["remaining"].pop(0)
                query = (song.get("webpage_url") or song.get("url")
                         or f"{song.get('uploader', '')} {song.get('title', '')}".strip())
                if not query:
                    continue
                try:
                    info = await self._resolve_yt_query(query)
                except Exception as e:
                    logger.debug(f"⚠️ [PersonalShuffle] resolve 失敗 '{query}': {e}")
                    continue
                if not info:
                    continue
                if self._check_song_duplicate(url=info.get('url', ''), title=info.get('title', ''),
                                              username=user, webpage_url=info.get('webpage_url', ''), check_history=False):
                    continue
                info['requested_by'] = user
                info['_lane'] = 'personal'
                self.stream_queue.append(info)
                self._republish_queue_snapshot()
                # WARNING 級：music_cog 的 INFO 目前被壓掉，個人歌單要看得到才好診斷搶播
                logger.warning(f"🎲 [PersonalShuffle] 墊一首（{user}）: {info['title']}（剩 {len(sess['remaining'])} 首）")
                return True
            # 池空 → 收尾
            self._personal_shuffle = None
            vc = self._vc()
            ch = vc.active_text_channel if vc is not None else None
            if ch is not None:
                try:
                    await ch.send(f"🎲 {user} 的歌單播完了，回到一般推薦。")
                except Exception:
                    pass
            logger.warning(f"🎲 [PersonalShuffle] {user} 歌單播畢，session 結束。")
            return False
        finally:
            self._personal_topup_inflight = False

    @staticmethod
    def _eligible_replay_pool(history: list, skip_vids: set) -> list:
        """最終安全網選池：從播放歷史挑可重播的舊歌，排除最近 min(5, 歷史-1) 首(防立即重複)+skip 過的。

        歷史 <2 首 → 回 []（真沒得循環，讓串流正常停）。舊門檻是「歷史 <6 首」，但那只是
        「排除最近 5 首」順手訂出來的數字，不是真的沒歌可放——短場次(剛重啟/才聽幾首)一律
        打不開安全網，還會在 `_last_resort_replay` 靜默失敗（2026-08-10 事故）。
        """
        hist = [s for s in history if isinstance(s, dict) and s.get('webpage_url')]
        if len(hist) < 2:
            return []
        recent_n = min(5, len(hist) - 1)
        recent_vids = {extract_video_id(s.get('webpage_url', '')) for s in hist[-recent_n:]}
        out = []
        for s in hist[:-recent_n]:
            v = extract_video_id(s.get('webpage_url', ''))
            if v and v not in recent_vids and v not in skip_vids:
                out.append(s)
        return out

    async def _last_resort_replay(self) -> bool:
        """三層 autopilot 全枯竭（歌庫 24h 內被播光→候選全被『已播過』濾掉）時的最終安全網：
        從本場歷史挑一首舊歌重播，保證只要有足夠歷史就永不靜默停播（無限續歌本意；
        2026-06-24/07-08 停播事故）。force_fresh 重抓避開過期 URL。回 True=補到歌。"""
        mm = getattr(self.bot, 'music_memory', None)
        skip_vids = mm.get_skipped_video_ids() if mm is not None else set()
        pool = self._eligible_replay_pool(list(self.stream_history), skip_vids)
        if not pool:
            logger.warning(f"⚠️ [AutoRecommend] 絕境回收失敗：本場歷史不足可回收（{len(self.stream_history)}首）")
            return False
        import random
        pick = random.choice(pool)
        info = await self._resolve_yt_query(pick['webpage_url'], force_fresh=True)
        if not info or not info.get('url'):
            logger.warning(f"⚠️ [AutoRecommend] 絕境回收失敗：重抓網址失敗「{pick.get('title')}」")
            return False
        info['requested_by'] = 'Marvin推薦（點給大家）'
        self.stream_queue.append(info)
        self._republish_queue_snapshot()
        logger.info(f"🔁 [AutoRecommend] 絕境回收：三層枯竭→重播「{info['title']}」（永不靜默停）")
        return True

    def _resolve_requester_avatar(self, vc, requester: str) -> str | None:
        """點播者頭像 URL：Marvin 推薦→bot 頭像；真人→從語音頻道成員 display_name 找；找不到→bot 兜底。"""
        try:
            bot_av = str(self.bot.user.display_avatar.url) if getattr(self.bot, 'user', None) else None
            if not requester or requester.startswith('Marvin'):
                return bot_av
            # vc 是 VoiceController cog → 語音頻道走 vc.voice_client.channel（非 vc.channel）
            ch = getattr(getattr(vc, 'voice_client', None), 'channel', None)
            if ch is not None:
                for m in ch.members:
                    if not m.bot and m.display_name == requester:
                        return str(m.display_avatar.url)
            return bot_av
        except Exception:
            return None

    async def _post_music_cards(self, active_ch, vc, info: dict) -> None:
        """貼①歌曲卡（封面全幅+點播者頭像圓徽合成圖）②歌詞（刪舊貼新，查無資料就不貼）
        ③控制台（刪舊貼新在底部）。背景執行。"""
        logger.info(f"🎛️ [Card] 貼卡 requester={info.get('requested_by')} cover={bool(info.get('thumbnail'))} ch={getattr(active_ch,'id',None)}")
        from cogs.voice_views import PlayControlView, build_song_embed, build_control_embed, build_lyrics_embed
        # ① 歌曲卡：合成封面+頭像；任一步失敗 → 退純封面（不阻斷）
        image_url = None
        file = None
        try:
            cover_url = info.get('thumbnail')
            avatar_url = self._resolve_requester_avatar(vc, info.get('requested_by', ''))
            if cover_url and avatar_url:
                import io
                import aiohttp
                from music_cover_card import compose_cover_with_avatar
                async with aiohttp.ClientSession() as s:
                    async with s.get(cover_url) as r1:
                        cov = await r1.read()
                    async with s.get(avatar_url) as r2:
                        av = await r2.read()
                pal = info.get('palette') or []
                png = await asyncio.to_thread(
                    compose_cover_with_avatar, cov, av,
                    title=info.get('title', ''),
                    primary=(pal[0] if len(pal) >= 1 else None),
                    secondary=(pal[1] if len(pal) >= 2 else None),
                )
                file = discord.File(io.BytesIO(png), filename="cover.png")
                image_url = "attachment://cover.png"
        except Exception as e:
            logger.warning(f"⚠️ [Card] 封面+頭像合成失敗，退純封面: {e}")
        try:
            _embed = build_song_embed(info, image_url=image_url)
            await active_ch.send(embed=_embed, file=file) if file else await active_ch.send(embed=_embed)
        except Exception as e:
            logger.warning(f"⚠️ [Card] 歌曲卡貼文失敗: {e}")
        # ② 歌詞：獨立訊息，刪掉上一首的、查無資料就不貼新的（不留存舊歌歌詞）
        lyrics = await self._fetch_lyrics_control_card(info)
        _old_lyrics_msg = self._active_lyrics_message
        if _old_lyrics_msg is not None:
            try:
                await _old_lyrics_msg.delete()
            except Exception:
                pass
        self._active_lyrics_message = None
        if lyrics:
            try:
                self._active_lyrics_message = await active_ch.send(embed=build_lyrics_embed(lyrics))
            except Exception as e:
                logger.warning(f"⚠️ [Card] 歌詞貼文失敗: {e}")
        # ③ 控制台：刪掉上一則、貼新的在最下面
        _old = self._active_control_view
        if _old is not None and getattr(_old, 'message', None):
            try:
                await _old.message.delete()
            except Exception:
                pass
        view = PlayControlView(vc)
        self._active_control_view = view
        try:
            view.message = await active_ch.send(embed=build_control_embed(vc), view=view)
        except Exception as e:
            logger.warning(f"⚠️ [Card] 控制台貼文失敗: {e}")

    def _publish_now_playing_state(self, info: dict | None) -> None:
        """把現正播放狀態寫到跨進程橋接檔，讓 main_satellite.py 的 /now（HUD）讀得到。

        main_satellite.py 是不登入 Discord 的獨立進程，自己的 MusicCog 永遠是空的，
        得靠這個檔案橋接真實播放狀態（見 now_playing_state.py）。寫檔失敗（磁碟/序列化
        問題）不該打斷播放，靜默吞掉。

        HUD 只在家用：瀏覽器 satellite（MARVIN_SATELLITE_BROWSER，在外用手機）是「在外」
        場景，這個模式下不寫橋接檔，避免蓋掉家用 HUD 該看的 Discord 真實播放狀態
        （橋接檔只有一份、沒有來源標記，寫了就會蓋掉）。

        車載 puck 有自己專屬的顯示端（100.109.213.74:8766），不再需要跟家用 HUD
        搶這份橋接檔，所以車機在場與否不影響這裡寫入（2026-08-19 起取消，見
        car_presence_state.py 開頭說明——is_car_actively_in_use 仍留給其他用途讀）。
        """
        if os.getenv("MARVIN_SATELLITE_BROWSER", "").strip().lower() in ("1", "true", "yes", "on"):
            return
        try:
            from now_playing_state import save_now_playing_state
            if info:
                queue = [{"title": s.get("title", ""), "by": s.get("requested_by", ""),
                          "thumbnail": s.get("thumbnail", "") or ""}
                         for s in self.stream_queue[:10]]
                save_now_playing_state(
                    playing=True,
                    title=info.get("title", ""),
                    by=info.get("requested_by", ""),
                    cover=info.get("thumbnail", ""),
                    palette=info.get("palette", []),
                    queue=queue,
                    duration=info.get("duration"),
                    song_start_time=self._current_stream_start_time,
                    comment=self._current_stream_comment,
                    explanation=self._current_stream_explanation,
                )
            else:
                save_now_playing_state(playing=False)
        except Exception:
            pass

    def _republish_queue_snapshot(self) -> None:
        """佇列變動（補歌/新點歌/移除個人歌單）後重寫橋接檔，不用等下一首開播 HUD 才刷新。"""
        self._publish_now_playing_state(self._current_stream_info)
