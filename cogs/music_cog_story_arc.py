"""
MusicStoryArcMixin — MusicCog 的一般 autopilot 推薦主流程 `_auto_recommend` 及其輔助（檔名沿用舊稱）。

從 music_cog.py 抽出（減肥，比照 voice_controller.py 拆解先例），以 mixin 形式
併入 MusicCog：
    class MusicCog(..., MusicStoryArcMixin, ..., commands.Cog): ...
因此 self 仍是 MusicCog 實例，bot.music_memory / bot.router / bot.tts_engine /
_resolve_yt_query / _t2_discovery_candidates /
_t4_fresh_discovery / _current_bpm_filter / _load_taste_fingerprint /
_attribution_with_suki / _recommend_blurb / _llm_coverify 等全部沿用原本的
self 存取，行為零改動。

`_auto_recommend` 對 MusicAutopilotMixin 的方法有多條呼叫邊（
_current_bpm_filter/_t2_discovery_candidates/_t4_fresh_discovery/
_load_taste_fingerprint/_attribution_with_suki/_recommend_blurb/_llm_coverify），
是這批拆解裡耦合最重的一個檔案——但跨 mixin 檔的 self 呼叫本就安全（同一個
MusicCog 實例）。
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time

import owner_song_voice_samples
from intent_agents.recommendation import Recommendation, append_recommendation, time_of_day_bucket
from music_memory import extract_video_id
from music_recommender import (
    Candidate,
    arc_nostalgia_candidates,
    assemble_arc,
    assign_unique_owners,
    build_member_pools,
    demote_low_quality_versions,
    find_recent_same_song,
    is_already_recommended,
    filter_unfamiliar,
    pick_candidates,
    ring_titles_for,
    server_play_count,
    song_video_id_for_title,
)

logger = logging.getLogger(__name__)


class MusicStoryArcMixin:

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

    async def _maybe_state_pick(self, spotlight: str, members: list, cands: list) -> list:
        """💬 [StatePick] spotlight 近期有狀態（如「感冒喉嚨痛」）→ LLM 從他自己的候選池挑一首
        排第一、附關心理由（DJ 口白 memory_match 用）。任何不符/失敗回原 cands，不中斷選歌。
        隱私：當事人在場才用；taboos/annoyed 在 collect_fresh_states 排除。見 state_song_pick.py。"""
        try:
            if os.getenv("MARVIN_STATE_PICK") != "1":
                return cands
            import dataclasses
            from state_song_pick import (MAX_TITLES, PERSON_COOLDOWN_S, build_state_pick_prompt,
                                         collect_fresh_states, parse_state_pick)
            from suki_memory import is_pseudo_player
            if not spotlight or is_pseudo_player(spotlight) or spotlight not in members:
                return cands
            suki = getattr(getattr(self.bot, 'router', None), 'memory', None)
            if suki is None or not suki.has_player(spotlight):
                return cands
            store = self._dj_topic_store()
            person_key = f"state_pick:{spotlight}"
            if not store.is_cool("", meme_id=person_key, cooldown_s=PERSON_COOLDOWN_S):
                return cands
            states = [s for s in collect_fresh_states(suki.get_player_memory(spotlight), now=time.time())
                      if store.is_cool(s)]
            if not states:
                return cands
            titles = [c.anchor_title for c in cands[:MAX_TITLES]]
            if len(titles) < 2:
                return cands
            # 不管 LLM 成敗都先冷卻這個人，避免每輪重打 LLM
            store.mark_used("", meme_id=person_key, cooldown_s=PERSON_COOLDOWN_S)
            sys_p, user_p = build_state_pick_prompt(spotlight, states, titles)
            # 不傳 speaker=：會被算進該人的互動次數
            raw = await asyncio.wait_for(
                self.bot.router._call_llm(sys_p, user_p, is_json=True, tier="simple",
                                          purpose="state_song_pick"),
                timeout=10)
            parsed = parse_state_pick(raw, n_titles=len(titles), n_states=len(states))
            if parsed is None:
                logger.info(f"💬 [StatePick] {spotlight} 有狀態但 LLM 沒挑（無合適/不合格）")
                return cands
            idx, s_idx, reason = parsed
            store.mark_used(states[s_idx])
            chosen = dataclasses.replace(cands[idx], state_reason=reason)
            logger.info(f"💬 [StatePick] {spotlight} 狀態『{states[s_idx][:20]}』→《{chosen.anchor_title}》")
            return [chosen] + [c for i, c in enumerate(cands) if i != idx]
        except Exception:
            logger.debug("[StatePick] 失敗，沿用原候選", exc_info=True)
            return cands

    async def _assemble_arc_candidates(self, spotlight, members, pool, fallback, exclude_titles, mm,
                                       excluded_vids=frozenset()) -> list:
        """T1 一組三首：懷舊（spotlight 點過、30 天沒播）→ 過門（以第 1 首當 radio 種子）→
        新歌（T4 冒險發現、伺服器沒播過）。哪個位置湊不到就由 fallback（原 T1 候選）補。"""
        songs = mm.all_songs()
        nost = arc_nostalgia_candidates(pool, songs, now=time.time(), excluded_vids=excluded_vids)
        nost = pick_candidates(nost, k=self._round_size, top_n=9)
        lead = fallback[0] if fallback[0].state_reason else (nost[0] if nost else None)
        seed_vid = song_video_id_for_title(songs, lead.anchor_title) if lead else ""

        async def _no_radio():
            return []

        radio, fresh = await asyncio.gather(
            self._t2_radio_for_seed(seed_vid, exclude_titles) if seed_vid else _no_radio(),
            self._t4_fresh_discovery(members, spotlight, exclude_titles),
            return_exceptions=True,
        )
        if isinstance(radio, BaseException):
            logger.warning(f"⚠️ [AutoRecommend] arc 過門 radio 失敗，略過: {radio}")
            radio = []
        if isinstance(fresh, BaseException):
            logger.warning(f"⚠️ [AutoRecommend] arc 新歌 T4 失敗，略過: {fresh}")
            fresh = []
        bridge = [
            Candidate(anchor_title=c["title"], anchor_artist=c["artist"], lane="discovery",
                      mode="direct", target_member=None, score=0.0, direct_url=c["url"],
                      discovery_seed_title=lead.anchor_title)
            for c in radio
        ]
        discovery = filter_unfamiliar(fresh, songs)
        logger.info(f"🎼 [AutoRecommend] arc: spotlight={spotlight} nostalgia={len(nost)} bridge={len(bridge)} "
                    f"discovery={len(discovery)}/{len(fresh)} fallback={len(fallback)}")
        return assemble_arc(nost, bridge, discovery, fallback)

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

        if _tier == 1:
            # 🎵 [AssociativeCuration] 嘗試對話關聯與歌詞金句選曲（env-gated + 冷卻，失敗回 0 → 走原 autopilot）
            if hasattr(self, '_try_associative_pick'):
                _n_assoc = await self._try_associative_pick(members, exclude_titles, spotlight, mm)
                if _n_assoc > 0:
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
            cands = await self._maybe_state_pick(spotlight, members, cands)
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

        # 🎼 T1 一組三首（懷舊→過門→新歌）；T1 沒候選就照舊往 T2 遞迴。
        _arc = _tier == 1 and bool(cands)
        if _arc:
            cands = await self._assemble_arc_candidates(spotlight, members, pool, cands, exclude_titles, mm,
                                                        excluded_vids)

        # 🎚️ [Quality] cover/現場版降到隊尾——自動推薦 cover 11% vs 真人 3%，humans 避開。
        # 好版本先填滿 round；沒更好的時 cover/live 仍會播（不丟棄→不枯竭）。arc 已分段 demote。
        if not _arc:
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
        _filled_roles: set[str] = set()
        _prev_round_title = self.stream_queue[-1].get('title') if self.stream_queue else None
        for cand in cands:
            if enqueued >= self._round_size:
                break
            if cand.arc_role and cand.arc_role in _filled_roles:
                continue
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
            if _tier == 2 or cand.arc_role == "bridge":
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
            info['_arc_role'] = cand.arc_role
            info['_anchor_title'] = cand.anchor_title
            # 🎯 推薦解釋：必須在這裡（record_play() 之前）算，不能等到真正播放時才算
            # ——mm.all_songs() 到那時已經把「現在正要播的這次」記進 plays[]，會把「你
            # 上次聽是 0 週前」這種自我指涉的假解釋算進去。這裡拿到的還是播放前的乾淨
            # 歷史（見 explanation_slotfill.py 開頭動機說明）。
            info['_explanation'] = self._compute_recommend_explanation(mm, cand)
            # 全伺服器播放次數（DJ 口白熟悉度分流用）：同上，要在 record_play 之前算
            info['_server_plays'] = server_play_count(mm.all_songs(), info['title'], _cand_vid or "")
            if getattr(cand, 'state_reason', ''):
                info['_state_reason'] = cand.state_reason
            info['_round_position'] = enqueued
            # round 內同批 enqueue 時 stream_history 還沒更新到本輪前面幾首歌（要等真正播放
            # 才 append），DJ 反查 prev_title 會抓到上一輪的舊歷史。round 內歌曲會依序播放，
            # 用同一輪前一個位置的標題當作可靠的「上一首」提示（見 _fetch_dj_interjection_raw）。
            if _prev_round_title:
                info['_prev_title_hint'] = _prev_round_title
            _prev_round_title = info['title']

            self.stream_queue.append(info)
            if cand.arc_role:
                _filled_roles.add(cand.arc_role)
            for _ring_title in ring_titles_for(info['title'], cand.mode, cand.anchor_title):
                mm.add_recent_recommendation(_ring_title)
            logger.info(f"🎵 [AutoRecommend] lane={cand.lane} role={cand.arc_role or '-'} round-#{enqueued+1}: {info['title']}")
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
                    "arc_role": cand.arc_role,
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
