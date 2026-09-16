"""
MusicStoryArcMixin — MusicCog 的「故事弧線節目」（dj_story_arc.py）Prepare/Play
兩階段管線 + slash 指令，以及一般 autopilot 推薦主流程 `_auto_recommend`。

從 music_cog.py 抽出（減肥，比照 voice_controller.py 拆解先例），以 mixin 形式
併入 MusicCog：
    class MusicCog(..., MusicStoryArcMixin, ..., commands.Cog): ...
因此 self 仍是 MusicCog 實例，bot.music_memory / bot.router / bot.tts_engine /
_resolve_yt_query / _try_themed_set / _t2_discovery_candidates /
_t4_fresh_discovery / _current_bpm_filter / _load_taste_fingerprint /
_attribution_with_suki / _recommend_blurb / _llm_coverify 等全部沿用原本的
self 存取，行為零改動。

`_auto_recommend` 對 MusicAutopilotMixin 的方法有多條呼叫邊（_try_themed_set/
_current_bpm_filter/_t2_discovery_candidates/_t4_fresh_discovery/
_load_taste_fingerprint/_attribution_with_suki/_recommend_blurb/_llm_coverify），
是這批拆解裡耦合最重的一個檔案——但跨 mixin 檔的 self 呼叫本就安全（同一個
MusicCog 實例），把它跟其餘 story_arc 內容放在同一份「故事弧線」檔而不硬塞進
autopilot 檔，純粹是保留原始 section 邊界讓 diff 好審，不影響行為。
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time

import discord
from discord import app_commands

import owner_song_voice_samples
from intent_agents.recommendation import Recommendation, append_recommendation, time_of_day_bucket
from music_memory import extract_video_id
from music_recommender import (
    assign_unique_owners,
    build_member_pools,
    demote_low_quality_versions,
    find_recent_same_song,
    is_already_recommended,
    pick_candidates,
    ring_titles_for,
)

logger = logging.getLogger(__name__)


class MusicStoryArcMixin:
    # ── 📖 [StoryArc] 故事弧線節目（dj_story_arc.py）──────────────────────────

    async def _run_story_arc_pipeline(self, members: list, target_minutes: float):
        """離線 Step1-5：找敘事流→共同/個人回憶→大綱+選歌→口白→resolve+片頭。

        跟 scripts/preview_story_arc.py 同一批函式、同一套邏輯，只是資料源改成
        cog 內既有的 self.bot.music_memory / suki / self._resolve_yt_query（不用
        另外拉 yt-dlp standalone resolve）。

        回 (arc, infos, brief, intro) 或 (None, 原因字串) 供指令層告知使用者為何沒開播。
        """
        from dj_story_arc import (build_show_intro, build_story_candidate_pools,
                                  curate_story_interjections, curate_story_outline,
                                  gather_story_brief, resolve_story_arc)
        from llm_pool import call_paid_review
        from track_quality import is_non_song_video, extract_video_id

        entries = self._load_summary_entries()
        if not entries:
            return None, "沒有對話記錄可用"
        now = time.time()

        suki = getattr(getattr(self.bot, 'router', None), 'memory', None)
        liked_items = []
        if suki is not None:
            for m in members:
                try:
                    for item in suki.get_recent_liked_items(m, limit=2):
                        liked_items.append(f"{m}喜歡{item}")
                except Exception:
                    pass
        conv_snippets = [e.core for e in entries[-4:] if getattr(e, "core", None)]

        target_duration_s = target_minutes * 60.0
        brief = gather_story_brief(entries, members, liked_items, conv_snippets,
                                   now=now, target_duration_s=target_duration_s)
        if brief is None:
            return None, "共同回憶素材不足（近7天可用共同核心句 < 2），無法生成故事弧"

        mm = getattr(self.bot, 'music_memory', None)
        if mm is None:
            return None, "音樂記憶尚未就緒"
        exclude_titles = mm.get_recently_played_titles(7 * 24 * 3600)
        exclude_vids = mm.get_recently_played_video_ids(7 * 24 * 3600) | mm.get_skipped_video_ids()
        pools = build_story_candidate_pools(members, mm.all_songs(), exclude_titles, now=now)

        arc = await curate_story_outline(brief, pools, exclude_titles, call_fn=call_paid_review)
        if arc is None or not arc.nodes:
            return None, "LLM 生成故事大綱失敗"

        arc = await curate_story_interjections(arc, brief, call_fn=call_paid_review)

        infos = await resolve_story_arc(
            arc, resolve_fn=self._resolve_yt_query, exclude_vids=exclude_vids,
            is_non_song_fn=is_non_song_video, extract_vid_fn=extract_video_id)
        if not infos:
            return None, "選好的歌都解析失敗，無法播放"

        intro = build_show_intro(arc, brief)
        return (arc, infos, brief, intro), None

    async def _render_tts_with_duration(self, text: str) -> tuple:
        """文字轉 TTS 音檔 + ffprobe 量真實秒數（取代 Phase 1 preview 用的粗估）。
        失敗回 (None, 0.0)——caller 該優雅跳過這段口白，不中斷整場故事弧。"""
        if not text:
            return None, 0.0
        try:
            audio_path = await self.bot.tts_engine.generate_audio(text)
        except Exception:
            logger.warning("⚠️ [StoryArc] TTS 渲染失敗", exc_info=True)
            return None, 0.0
        if not audio_path:
            return None, 0.0
        dur = await self._probe_audio_duration(audio_path)
        return audio_path, dur

    @staticmethod
    async def _probe_audio_duration(path: str) -> float:
        """ffprobe 量音檔實際秒數，失敗回 0.0（caller 該當作「這段沒有時長可等」處理）。"""
        def _probe():
            import subprocess
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", path],
                capture_output=True, text=True, timeout=10)
            return float(out.stdout.strip())
        try:
            return await asyncio.to_thread(_probe)
        except Exception:
            return 0.0

    async def _splice_owner_voice_clip(self, dj_audio: str | None, info: dict) -> str | None:
        """語音點歌時，若能撈到 owner 當時點這首歌的原音片段，接在 DJ 介紹口白前面
        當彩蛋（先放「我想聽周杰倫的歌」原音，再接 DJ 說「幫你點的...」）。

        找不到樣本 / 非語音點歌 / 接檔失敗 → 原樣回傳 dj_audio，不影響既有行為
        （見 owner_song_voice_samples.py：opt-in、只 owner、7 天滾動保留）。
        """
        if not dj_audio or not info.get('voice_request'):
            return dj_audio
        clip_path = owner_song_voice_samples.find_recent_clip(
            f"{info.get('title', '')} {info.get('uploader', '')}"
        )
        if not clip_path:
            return dj_audio
        combined = f"{dj_audio}.with_clip.wav"

        def _concat():
            subprocess.run(
                ["ffmpeg", "-y", "-i", clip_path, "-i", dj_audio,
                 "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1[out]",
                 "-map", "[out]", combined],
                capture_output=True, timeout=15, check=True,
            )
        try:
            await asyncio.to_thread(_concat)
            logger.info(f"🎙️ [SongVoiceSample] 已接原音彩蛋：{os.path.basename(clip_path)} → {info.get('title')}")
            return combined
        except Exception as e:
            logger.debug(f"⚠️ [SongVoiceSample] 接原音失敗，退回純TTS: {e}")
            return dj_audio

    async def _prepare_and_stage_story_arc(self, members: list, target_minutes: float):
        """Prepare 階段：跑生成管線 + 把片頭/每個節點的口白都預渲染成真實 TTS 音檔，
        存成一份「待播節目」（`dj_story_arc.save_staged_show`）。播放當下（Play 階段）
        不再做任何 LLM/TTS 工作，零延遲、可排程在生成完成後任何時間點觸發。

        回 (staged_dict, None) 或 (None, 原因字串)。
        """
        from dj_story_arc import build_staged_show, save_staged_show

        result, err = await self._run_story_arc_pipeline(members, target_minutes)
        if result is None:
            return None, err
        arc, infos, brief, intro = result

        intro_audio_path, intro_audio_dur = await self._render_tts_with_duration(intro.intro_script)

        for info in infos:
            script = (info.get('_story_interjection_script') or '').strip()
            if script:
                audio_path, dur_s = await self._render_tts_with_duration(script)
                info['_story_interjection_audio_path'] = audio_path
                info['_story_interjection_duration_s'] = dur_s

        staged = build_staged_show(
            infos, intro, intro_audio_path=intro_audio_path,
            intro_audio_duration_s=intro_audio_dur, ts=time.time(),
            narrative_day=brief.narrative_day, target_duration_s=brief.target_duration_s)
        save_staged_show(staged)
        return staged, None

    async def _play_story_arc(self, staged: dict) -> None:
        """Play 階段：純播放一份已經 Prepare 好的「待播節目」（見 `dj_story_arc.load_staged_show`）。

        只有片頭（開場一次性 BGM+引導口白）是故事弧自己播；歌曲本身**直接丟進既有
        `stream_queue`**，交給 `_stream_loop`/`_run_tail_dj`/`play_stream_song` 這套
        本來就正確的機制接手播放跟 DJ 尾段口白——2026-08-17 真機測試踩到的三個 bug
        （still_active 誤判/BGM音量蓋過口白/webpage_url不是可播網址）本質上都是自己
        重造這套邏輯繞開既有正確實作造成的：歌曲只是故事裡的一份待播清單，不需要
        另外重寫一套播放器。`_fetch_dj_interjection_raw` 認得 `_lane == 'story_arc'`
        的節點，直接用 Prepare 階段預渲染好的口白，不重新過 LLM/TTS。

        片頭 BGM 音量固定壓到 `_STORY_ARC_BGM_VOLUME`（口白約 10% 感覺時，BGM 抓一半
        5%，別蓋過口白）。片頭口白 `vc._tts_protected = True` 全程開著，不被
        barge-in/靜音閘/game_mode 中途打斷（同 `_maybe_play_dj_interjection` 既有慣例）。
        """
        from dj_story_arc import ShowIntro, record_story_arc

        vc = self._vc()
        if vc is None:
            return
        intro_dict = staged.get('intro') or {}
        bgm_path = intro_dict.get('music_path') or ""

        # 只在片頭這段短暫的一次性播放期間開著——擋掉同時間第二個 /story_arc_play
        # 重複觸發片頭。歌曲交棒給 stream_queue 之後，正常播放狀態就看 stream_mode。
        self._story_arc_active = True
        try:
            # 片頭：一次性播放，跟後面的歌曲佇列無關，播完就結束這段。
            bgm_task = (asyncio.create_task(
                vc.play_local_file(bgm_path, volume=self._STORY_ARC_BGM_VOLUME))
                if bgm_path else None)
            intro_audio = intro_dict.get('audio_path')
            intro_dur = intro_dict.get('audio_duration_s') or 0.0
            if intro_audio and intro_dur > 0:
                with vc._protected_tts_window():
                    await vc.play_dj_on_tts_layer(intro_audio)
                    await asyncio.sleep(intro_dur)
            if bgm_task:
                bgm_task.cancel()

            # 歌曲：原樣丟進既有佇列（info dict 保留 resolve_story_arc 給的 url/webpage_url/
            # duration/highlight_start_s，不重新設計格式），交給 _stream_loop 接手播放。
            infos = sorted(staged.get('infos', []), key=lambda i: i.get('_story_node_position') or 0)
            for info in infos:
                info = dict(info)   # copy，避免共用 staged dict 的可變狀態
                info['requested_by'] = 'Marvin故事弧'
                info['_lane'] = 'story_arc'
                self.stream_queue.append(info)
            if infos:
                self._republish_queue_snapshot()
                self._ensure_stream_loop()
        finally:
            self._story_arc_active = False

        record_story_arc(
            staged.get("arc_title", ""), infos,
            target_duration_s=staged.get("target_duration_s", 0.0), ts=time.time(),
            narrative_day=staged.get("narrative_day", ""),
            intro=ShowIntro(intro_script=intro_dict.get("script", ""), intro_music_path=bgm_path))
        # 播完（其實是「交棒播放」那一刻）刻意不清 staged show——測播放設定不該每次都
        # 重新 Prepare 燒一次 LLM token。同一份內容可以重複 /story_arc_play；要換內容
        # 就重新 /story_arc_prepare，會覆蓋掉舊的（見 save_staged_show 是整檔覆寫）。

    @app_commands.command(name="story_arc_prepare", description="[DJ] 預先生成故事弧節目內容+口白TTS，不播放")
    @app_commands.describe(minutes="目標時長（分鐘，預設20）")
    async def story_arc_prepare(self, interaction: discord.Interaction, minutes: int = 20):
        await interaction.response.defer(ephemeral=False)
        guild_vc = interaction.guild.voice_client
        members = ([m.display_name for m in guild_vc.channel.members if not m.bot]
                  if guild_vc else [])
        if not members and interaction.user.voice:
            members = [m.display_name for m in interaction.user.voice.channel.members if not m.bot]
        if not members:
            await interaction.followup.send(
                "❌ 找不到故事對象——請待在語音頻道裡再試（不需要先 /summon，"
                "Prepare 階段不碰播放）。", ephemeral=True)
            return

        await interaction.followup.send(f"📖 正在為 {'、'.join(members)} 編一段故事，請稍候…")
        staged, err = await self._prepare_and_stage_story_arc(members, float(minutes))
        if staged is None:
            await interaction.followup.send(f"❌ 故事弧沒生成成功：{err}", ephemeral=True)
            return
        n = len(staged.get('nodes', []))
        await interaction.followup.send(
            f"✅ 《{staged.get('arc_title', '')}》準備好了，{n} 首歌 + 口白已預渲染。"
            f"用 `/story_arc_play` 開始播放。")

    @app_commands.command(name="story_arc_play", description="[DJ] 播放已經 /story_arc_prepare 好的故事弧節目")
    async def story_arc_play(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        vc = self._vc()
        if not vc:
            await interaction.followup.send("❌ 語音系統尚未就緒。", ephemeral=True)
            return
        guild_vc = interaction.guild.voice_client
        if not guild_vc:
            await interaction.followup.send("❌ 馬文不在語音頻道中。請先使用 `/summon`。", ephemeral=True)
            return
        if self._story_arc_active:
            await interaction.followup.send("📖 已經有一場故事弧在進行中了。", ephemeral=True)
            return
        if self.stream_mode:
            await interaction.followup.send(
                "❌ 目前有音樂正在自動播放，故事弧要先淨空播放狀態才能開始——"
                "先 `/marvin_radio stop` 或等目前播放結束再試。", ephemeral=True)
            return

        from dj_story_arc import load_staged_show
        staged = load_staged_show()
        if staged is None:
            await interaction.followup.send(
                "❌ 沒有準備好的節目，先跑 `/story_arc_prepare`。", ephemeral=True)
            return

        await interaction.followup.send(
            f"🎬 《{staged.get('arc_title', '')}》，{len(staged.get('nodes', []))} 首歌，開始了。")
        await self._play_story_arc(staged)

    async def _auto_recommend(self, username: str, *, _tier: int = 1):
        """佇列空 → 依在場成員的音樂記憶推薦下一首批。"""
        mm = getattr(self.bot, 'music_memory', None)
        if mm is None:
            return

        vc = self._vc()
        members = (vc.get_online_members() if vc is not None else []) or [username]

        self._recommend_spotlight_idx = (self._recommend_spotlight_idx + 1) % len(members)
        spotlight = members[self._recommend_spotlight_idx]

        recently = [s['title'] for s in list(self.stream_history)[-15:]]
        recommended = mm.get_recent_recommendation_titles()
        skipped = mm.get_skipped_titles(members)
        suki_hist: list[str] = []
        _suki = getattr(self.bot.router, 'memory', None)
        if _suki is not None:
            for m in members:
                suki_hist += (_suki.get_song_history(m) or [])[:10]
        exclude_titles = list(dict.fromkeys(recently + recommended + skipped + suki_hist))

        # 🎚️ [ThemedSet] 新一輪起手先試讀空氣主題歌單（env-gated，閘關/失敗回 0 → 走原 autopilot）
        if _tier == 1:
            _n_themed = await self._try_themed_set(members, exclude_titles, spotlight, mm)
            if _n_themed > 0:
                return

        vibe_filter = None
        vibe_label = None
        if self._mood_sensor is not None:
            try:
                self._mood_sensor.invalidate()
                active_ch = vc.active_text_channel if vc is not None else None
                guild_id = active_ch.guild.id if active_ch else 0
                vibe_label = await self._mood_sensor.current_vibe(guild_id=guild_id)
                vibe_filter = {"mood": vibe_label.mood, "topic": vibe_label.topic, "min_score": 0.0}
                logger.info(f"🎵 [AutoRecommend] vibe={vibe_label.mood} (engagement={vibe_label.engagement:.2f}, source={vibe_label.source})")
            except Exception as e:
                logger.warning(f"⚠️ [AutoRecommend] vibe sensor 失敗，fallback to no vibe filter: {e}")

        bpm_filter = self._current_bpm_filter()

        # per-member 候選 → 跨使用者唯一歸屬：同一首歌只歸一人（round-robin 平手代表），
        # 避免團體歌被分別指定給不同使用者重播。當輪只取 spotlight 自己的去重後候選。
        _member_pools = build_member_pools(
            members=members,
            songs=mm.all_songs(),
            exclude_titles=exclude_titles,
            now=time.time(),
            vibe_filter=vibe_filter,
            bpm_filter=bpm_filter,
        )
        pool = assign_unique_owners(_member_pools, rotation_order=members).get(spotlight, [])

        _skipped_vids = mm.get_skipped_video_ids()
        _taste_fp = self._load_taste_fingerprint()

        # k 多抽 3 倍當緩衝：入隊前把 cover/現場版降到隊尾，好版本先填滿 round（見下方 demote）。
        _k_buf = self._round_size * 3
        if _tier == 1:
            cands = pick_candidates(pool, k=_k_buf, top_n=max(9, _k_buf))
            ring_exclude = exclude_titles
            excluded_vids = _skipped_vids | mm.get_recently_played_video_ids(self._PLAYED_EXCLUDE_TTL_S)
            _played_titles = mm.get_recently_played_titles(self._PLAYED_EXCLUDE_TTL_S)
        elif _tier == 2:
            cands = await self._t2_discovery_candidates(members, exclude_titles)
            ring_exclude = exclude_titles
            excluded_vids = _skipped_vids | mm.get_recently_played_video_ids(self._PLAYED_EXCLUDE_TTL_S)
            _played_titles = mm.get_recently_played_titles(self._PLAYED_EXCLUDE_TTL_S)
        elif _tier == 3:
            # 放寬到 24h 而非砍光：仍回收 1-7 天前舊歌，但擋當天剛播過的，防同場收斂重播。
            # 候選池(歌名)與 enqueue 迴圈(video-id)同步排除 24h 已播，否則池子挑出剛播歌、
            # 迴圈又擋掉 → enqueue=0 → T3 無 fallback → 停播（2026-06-24 回報）。
            _t3_played = mm.get_recently_played_titles(self._T3_PLAYED_EXCLUDE_TTL_S)
            _t3_exclude = list(dict.fromkeys(skipped + _t3_played))
            _relaxed_pools = build_member_pools(
                members=members, songs=mm.all_songs(),
                exclude_titles=_t3_exclude,
                now=time.time(), vibe_filter=vibe_filter, bpm_filter=bpm_filter,
            )
            relaxed_pool = assign_unique_owners(_relaxed_pools, rotation_order=members).get(spotlight, [])
            cands = pick_candidates(relaxed_pool, k=_k_buf, top_n=max(9, _k_buf))
            ring_exclude = _t3_exclude
            excluded_vids = _skipped_vids | mm.get_recently_played_video_ids(self._T3_PLAYED_EXCLUDE_TTL_S)
            _played_titles = _t3_played
        else:
            # T4 冒險發現：T1-T3(個人史/radio 收斂)全枯竭→用核心藝人 catalog 搜「未播新歌」。
            # 罕見觸發＝值得冒險注入全新歌，不只回收舊歌（2026-07-08：個人史子集耗盡、radio 種子
            # 收斂到同批已播熱門歌；使用者訂「觸發難就冒險」）。新歌不在 24h 已播內、排除照舊。
            cands = await self._t4_fresh_discovery(members, spotlight, exclude_titles)
            ring_exclude = exclude_titles
            excluded_vids = _skipped_vids | mm.get_recently_played_video_ids(self._PLAYED_EXCLUDE_TTL_S)
            _played_titles = mm.get_recently_played_titles(self._PLAYED_EXCLUDE_TTL_S)

        # 🎚️ [Quality] cover/現場版降到隊尾——自動推薦 cover 11% vs 真人 3%，humans 避開。
        # 好版本先填滿 round；沒更好的時 cover/live 仍會播（不丟棄→不枯竭）。
        cands = demote_low_quality_versions(cands)
        if not cands:
            if _tier < 4:
                return await self._auto_recommend(username, _tier=_tier + 1)
            logger.debug("🎵 [AutoRecommend] 四層皆無候選，跳過（退最終回收保險）")
            return

        self._round_track_count = 0

        if self._cover_blacklist is None:
            try:
                from track_quality import CoverBlacklist
                self._cover_blacklist = CoverBlacklist.shared()
            except Exception:
                logger.exception("[AutoRecommend] CoverBlacklist init 失敗")

        enqueued = 0
        _prev_round_title = self.stream_queue[-1].get('title') if self.stream_queue else None
        for cand in cands:
            if enqueued >= self._round_size:
                break
            if cand.direct_url:
                query = cand.direct_url
            elif cand.mode == "cover":
                query = await self._llm_coverify(cand, exclude_titles)
            else:
                query = f"{cand.anchor_artist} {cand.anchor_title}".strip() or cand.anchor_title
            if not query:
                continue

            try:
                info = await self._resolve_yt_query(query)
            except Exception as e:
                logger.debug(f"⚠️ [AutoRecommend] _resolve_yt_query fail '{query}': {e}")
                continue
            if not info:
                continue
            if self._check_song_duplicate(url=info['url'], title=info['title'], username=username, webpage_url=info.get('webpage_url', '')):
                logger.info(f"🎵 [AutoRecommend] {info['title']} 本場已播過，略過")
                continue
            if is_already_recommended(info['title'], ring_exclude):
                logger.info(f"🎵 [AutoRecommend] {info['title']} 已在 recent ring，略過")
                continue
            _cand_vid = extract_video_id(info.get('webpage_url') or info.get('url') or '')
            if _cand_vid and _cand_vid in excluded_vids:
                logger.info(f"🎵 [AutoRecommend] {info['title']} video-id 已播過/已skip，略過")
                continue
            _same = find_recent_same_song(info['title'], _played_titles)
            if _same:
                logger.info(f"🎵 [AutoRecommend] {info['title']} 與最近播過『{_same[:30]}』同歌不同上傳，略過")
                continue
            from track_quality import is_non_song_video
            _ns, _ns_reason = is_non_song_video(info.get('title', ''), info.get('duration'))
            if _ns:
                logger.info(f"🚫 [AutoRecommend] 非單曲略過 '{info['title']}': {_ns_reason}")
                continue
            if _tier == 2:
                from taste_fingerprint import explore_matches_floor
                if not explore_matches_floor(info.get('title', ''), _taste_fp):
                    logger.info(f"🎵 [AutoRecommend] explore 不合口味地板(語言)略過: {info['title']}")
                    continue

            if self._cover_blacklist is not None:
                try:
                    from track_quality import assess_track_quality
                    passes, reason = await assess_track_quality(
                        info['url'], info['title'],
                        blacklist=self._cover_blacklist,
                    )
                    if not passes:
                        logger.info(f"🚫 [AutoRecommend] Quality block '{info['title']}': {reason}")
                        continue
                except Exception:
                    logger.exception("[AutoRecommend] quality filter raised — fail-open")

            # 掛名（2026-07-02+07-09）：X 點過→為X；否則歌手強匹配某在場者 suki 愛歌手→為X；再否則點給大家
            info['requested_by'] = self._attribution_with_suki(mm, info, spotlight)
            info['_round_first'] = (enqueued == 0)
            info['_spotlight'] = spotlight
            info['_lane'] = cand.lane
            info['_anchor_title'] = cand.anchor_title
            # 🎯 推薦解釋：必須在這裡（record_play() 之前）算，不能等到真正播放時才算
            # ——mm.all_songs() 到那時已經把「現在正要播的這次」記進 plays[]，會把「你
            # 上次聽是 0 週前」這種自我指涉的假解釋算進去。這裡拿到的還是播放前的乾淨
            # 歷史（見 explanation_slotfill.py 開頭動機說明）。
            info['_explanation'] = self._compute_recommend_explanation(mm, cand)
            info['_round_position'] = enqueued
            # round 內同批 enqueue 時 stream_history 還沒更新到本輪前面幾首歌（要等真正播放
            # 才 append），DJ 反查 prev_title 會抓到上一輪的舊歷史。round 內歌曲會依序播放，
            # 用同一輪前一個位置的標題當作可靠的「上一首」提示（見 _fetch_dj_interjection_raw）。
            if _prev_round_title:
                info['_prev_title_hint'] = _prev_round_title
            _prev_round_title = info['title']

            self.stream_queue.append(info)
            for _ring_title in ring_titles_for(info['title'], cand.mode, cand.anchor_title):
                mm.add_recent_recommendation(_ring_title)
            logger.info(f"🎵 [AutoRecommend] lane={cand.lane} round-#{enqueued+1}: {info['title']}")
            blurb = ""
            if enqueued == 0:
                vibe_tag = f" [vibe: {vibe_label.mood}]" if vibe_label else ""
                # 文案與掛名同規則：blurb 指名的人（target_member 優先）也要真的點過這首
                _blurb_who = cand.target_member or spotlight
                _personal = bool(_blurb_who) and mm.is_requester(info, _blurb_who)
                blurb = self._recommend_blurb(cand, info['title'], spotlight=spotlight,
                                              personal=_personal) + vibe_tag
                # 2026-07-08 使用者：這段推薦文字不貼頻道了——推薦會播出來(DJ 語音)+有歌曲卡，文字多餘。
                # blurb 仍計算，供日記/推薦紀錄 append_recommendation 用。

            _recent_titles = [
                s.get("title", "") for s in self.stream_history[-3:] if isinstance(s, dict)
            ]
            append_recommendation(self._build_autopilot_rec(
                spotlight=spotlight, title=info['title'], lane=cand.lane, mode=cand.mode,
                anchor_title=cand.anchor_title, blurb=blurb, now=time.time(),
                channel_state_extras={
                    "vibe_mood": vibe_label.mood if vibe_label else None,
                    "vibe_engagement": round(vibe_label.engagement, 2) if vibe_label else None,
                    "queue_position": enqueued,
                    "round_first": info['_round_first'],
                    "queue_depth": len(self.stream_queue),
                    "recent_history_titles": _recent_titles,
                    "spotlight_member": spotlight,
                },
            ))

            next_url = info.get('url', '')
            if next_url and next_url not in self._prefetch_cache and vc is not None:
                self._prefetch_cache[next_url] = asyncio.create_task(self._fetch_song_meta(info))

            enqueued += 1

        logger.info(f"🎵 [AutoRecommend] T{_tier} round 完成: enqueued={enqueued}/{self._round_size}")
        if enqueued:
            self._republish_queue_snapshot()
        if enqueued == 0 and _tier < 4:
            await self._auto_recommend(username, _tier=_tier + 1)

    @staticmethod
    def _build_autopilot_rec(*, spotlight, title, lane, mode, anchor_title, blurb, now,
                              channel_state_extras=None) -> "Recommendation":
        """把 autopilot 推薦包成 Recommendation（offline feedback 用）。"""
        channel_state = dict(channel_state_extras or {})
        channel_state["lane"] = lane
        channel_state["mode"] = mode
        channel_state["time_of_day"] = time_of_day_bucket(now)
        return Recommendation(
            ts=now, agent="music", speaker=spotlight,
            trigger="queue_empty", selected=title,
            reason_internal=f"queue_empty:{lane}:{mode}:{anchor_title}",
            explanation_uttered=blurb, feedback_window_s=300,
            channel_state=channel_state,
        )
