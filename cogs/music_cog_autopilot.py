"""
MusicAutopilotMixin — MusicCog 的 autopilot 自動推薦引擎（T2 discovery / T4 冒險
發現 / cover 推薦 / 主題歌單策展 / 掛名歸因等）。

從 music_cog.py 抽出（減肥，比照 voice_controller.py 拆解先例），以 mixin 形式
併入 MusicCog：
    class MusicCog(..., MusicAutopilotMixin, ..., commands.Cog): ...
因此 self 仍是 MusicCog 實例，bot.router / bot.music_memory /
_current_stream_info / _resolve_yt_query / _check_song_duplicate /
_republish_queue_snapshot / _round_size / _recommend_spotlight_idx 等全部
沿用原本的 self 存取，行為零改動。

_TASTE_PROFILE_CACHE / _TASTE_FINGERPRINT_CACHE / _SONG_BPM_STORE 是純字面
常數，主檔也用得到，各自定義一份不搬移。
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import os
import random
import time

from music_memory import extract_video_id
from music_recommender import is_already_recommended, normalize_title, ring_titles_for

logger = logging.getLogger(__name__)

_TASTE_PROFILE_CACHE = "records/taste_profiles.json"
_TASTE_FINGERPRINT_CACHE = "records/taste_fingerprint.json"
_SONG_BPM_STORE = "records/song_bpm.json"


class MusicAutopilotMixin:
    # ── 🎵 Autopilot recommendation engine ───────────────────────────────────

    @staticmethod
    def _autorecommend_seed(requested_by: str | None, online_members: list[str]) -> str | None:
        """佇列空時決定要不要續推自動推薦、用誰當 seed user。回 None = 不續推。"""
        if not requested_by or requested_by == '未知':
            return None
        if requested_by.startswith('Marvin'):
            return online_members[0] if online_members else None
        return requested_by

    # 健康播放的最低秒數；低於此且非使用者 skip → 疑似 403/網路失敗（yt-dlp 串流網址
    # 過期，ffmpeg 開檔即 403 → play_stream_song 秒返）。
    _MIN_HEALTHY_PLAY_S = 3.0

    @classmethod
    def _should_retry_failed_song(
        cls, played_s: float, *, stream_active: bool, skipped: bool,
        requested_by: str | None, already_retried: bool,
    ) -> bool:
        """播的歌太短（疑 403/失敗）→ 該重抓網址重試一次，別靜默跳下一首。

        守門（全過才重試）：沒重試過 / 仍在串流(非 stop) / 非使用者 skip / 播放 < 健康門檻。
        **跟「誰點的」無關**——2026-07-07 bug：原本排除 Marvin 自動推薦，導致自動歌 403
        短播時靜默跳過（連 log 都沒有）。單次 force_fresh 重試對誰都安全（already_retried
        上鎖、每首只救一次、不會無限；使用者 skip 由 skipped 擋住不會誤重播）。
        """
        _ = requested_by  # 保留參數簽章相容；短播救援不再看點播者
        if already_retried or not stream_active or skipped:
            return False
        return played_s < cls._MIN_HEALTHY_PLAY_S

    @classmethod
    def _premature_cut(cls, played_s: float, duration) -> bool:
        """歌是否『中途被切』：播超過健康門檻(非開頭403)、卻遠短於真實總長(<80%)。

        用來讓「播到一半串流 URL 中途失效→跳下一首」變可見（ffmpeg stderr 進 DEVNULL＝
        log 隱形，且開頭 403 由 _should_retry_failed_song 處理，這裡只抓中途切）。
        """
        if not duration or duration <= 0:
            return False
        if played_s < cls._MIN_HEALTHY_PLAY_S:
            return False   # 開頭就掛→走 403 重試路徑，不算中途切
        return played_s < duration * 0.8

    def _load_taste_fingerprint(self) -> dict:
        """讀 records/taste_fingerprint.json（5 分鐘快取；缺檔/壞檔 → {} fail-open）。"""
        now = time.time()
        if hasattr(self, "_taste_fp_cache") and now - getattr(self, "_taste_fp_loaded_at", 0) < 300:
            return self._taste_fp_cache
        try:
            import json as _json
            with open(_TASTE_FINGERPRINT_CACHE, "r", encoding="utf-8") as f:
                self._taste_fp_cache = _json.load(f)
        except Exception:
            self._taste_fp_cache = {}
        self._taste_fp_loaded_at = now
        return self._taste_fp_cache

    def _current_bpm_filter(self) -> dict | None:
        """目前播放歌的 BPM（見 bpm_estimate.py 取樣落地）→ build_member_pools 的
        bpm_filter，讓下一輪候選偏好節奏接近的歌。目前歌沒 BPM 記錄（新歌/還沒取樣過）
        → None（不影響原排序，fail-open）。"""
        from bpm_estimate import read_bpm_store
        info = self._current_stream_info or {}
        vid = extract_video_id(info.get("webpage_url") or info.get("url") or "")
        if not vid:
            return None
        store = read_bpm_store(_SONG_BPM_STORE)
        entry = store.get(vid)
        if not isinstance(entry, dict) or entry.get("bpm") is None:
            return None
        return {"current_bpm": entry["bpm"], "store": store}

    async def _t2_radio_for_seed(self, seed_video_id: str, exclude_titles: list[str]) -> list[dict]:
        """單一 seed 的 radio 候選，帶 TTL 快取：一次 API 呼叫已回全量(~50首)，
        同 seed 在 TTL 內重複被選中（seed_rotation 常見連續多輪同一顆）就直接重用，
        只在本地重套當下的 exclude_titles（已播/skip 每輪都在變，不能連同結果一起快取）。

        過濾後隨機抽樣（非固定取前 N 首）：實測同一 seed 連續打 API 兩次，YouTube radio
        本身順序/集合就有小幅漂移（2026-08-10 驗證），可見「取前 N」的變化度一直是這種
        意外漂移撐出來的、不是設計；快取住同一批後這個意外漂移沒了，改用隨機抽樣顯式補回
        變化度，順便比原本的「永遠前 N 首」更不容易讓同一批候選反覆撞臉。
        """
        from ytmusic_radio import ytmusic_radio
        now = time.time()
        cached = self._t2_seed_cache.get(seed_video_id)
        if cached and now - cached[0] < self._T2_SEED_CACHE_TTL_S:
            raw = cached[1]
            logger.debug(f"[T2 SeedCache] seed={seed_video_id} 命中（省一次 API 呼叫）")
        else:
            raw = await asyncio.to_thread(ytmusic_radio, seed_video_id, exclude_titles=(), limit=50)
            self._t2_seed_cache[seed_video_id] = (now, raw)
        if not raw:
            return []
        excl = {normalize_title(t) for t in exclude_titles}
        filtered = [c for c in raw if normalize_title(c["title"]) not in excl]
        k = self._round_size * 2
        if len(filtered) <= k:
            return filtered
        return random.sample(filtered, k)

    async def _t2_discovery_candidates(self, members: list[str], exclude_titles: list[str]) -> list:
        """T2 discovery：多 seed → ytmusic radio 混合取相關新歌 → Candidate(direct_url)。"""
        mm = getattr(self.bot, 'music_memory', None)
        if mm is None:
            return []
        avoid_artists: list[str] = []
        if os.getenv("LLM_TASTE_T2", "off") == "on":
            try:
                import taste_profile
                _MAX_AGE = 8 * 86400
                avoid_artists = taste_profile.fresh_avoid_artists(_TASTE_PROFILE_CACHE, members, _MAX_AGE)
            except Exception as e:
                logger.warning(f"⚠️ [AutoRecommend] T2 LLM 品味快取讀取失敗，略過: {e}")
        try:
            _core = {a for a, _ in self._load_taste_fingerprint().get("core_artists", [])}
            for _a in mm.get_explore_avoid_artists():
                if _a not in _core and _a not in avoid_artists:
                    avoid_artists.append(_a)
        except Exception:
            logger.debug("[AutoRecommend] explore retreat avoid 合併失敗", exc_info=True)
        _N_SEEDS = 3
        # 多人種子輪替：每 N 首換主種子者(round-robin 在場者)、最後手動歌當 fresh lead
        # （N 首後淡出）、永遠混入其他在場者 → 不被單一人霸佔（見 seed_rotation.py）。
        import seed_rotation
        self._seed_epoch = getattr(self, '_seed_epoch', -1) + 1
        _since = getattr(self, '_auto_since_manual', _N_SEEDS)
        self._auto_since_manual = _since + 1
        # 各在場者的種子池＝他真人點過的歌（per-member，已排除 Marvin 自薦）；
        # LLM_TASTE_T2 on 時前置該人的 LLM 鄰近種子（curated taste）。
        _llm_on = os.getenv("LLM_TASTE_T2", "off") == "on"
        seeds_by_member = {}
        for _m in members:
            _pool = mm.get_played_seed_ids([_m], limit=50)
            # 單人模式保護：若該成員個人種子過少（<6 顆），混入伺服器全體真人點過的種子擴充電台廣度
            if len(members) == 1 and len(_pool) < 6:
                all_played = mm.get_played_seed_ids([], limit=30)
                for _v in all_played:
                    if _v not in _pool:
                        _pool.append(_v)
            if _llm_on:
                try:
                    import taste_profile
                    _pool = taste_profile.fresh_seed_ids(_TASTE_PROFILE_CACHE, [_m], 8 * 86400) + _pool
                except Exception:
                    pass
            seeds_by_member[_m] = _pool
        seeds = seed_rotation.order_rotating_seeds(
            members, seeds_by_member,
            epoch=self._seed_epoch, since_manual=_since,
            last_seed=getattr(self, '_last_user_song_seed', None),
            swap_every=_N_SEEDS, n=_N_SEEDS,
        )
        # rotating 不足 N 顆時用團體 liked 墊底
        if len(seeds) < _N_SEEDS:
            for vid in mm.get_liked_video_ids(members):
                if vid not in seeds:
                    seeds.append(vid)
                    if len(seeds) >= _N_SEEDS:
                        break
        logger.info(f"🎲 [AutoRecommend] 種子輪替 epoch={self._seed_epoch} "
                    f"主={seed_rotation.primary_member(members, self._seed_epoch, _N_SEEDS)} "
                    f"since_manual={_since} seeds={len(seeds)}")
        if not seeds:
            return []
        from ytmusic_radio import blend_radio_results
        seed_titles = self._seed_title_lookup(mm, seeds)
        results = []
        for sd in seeds:
            try:
                r = await self._t2_radio_for_seed(sd, exclude_titles)
            except Exception as e:
                logger.warning(f"⚠️ [AutoRecommend] T2 radio seed={sd} 失敗，跳過: {e}")
                continue
            if r:
                seed_title = seed_titles.get(sd, "")
                if seed_title:
                    for c in r:
                        c["_seed_title"] = seed_title
                results.append(r)
        if not results:
            logger.warning("⚠️ [AutoRecommend] T2 全 seed radio 空/失敗，退 T3")
            return []
        radio = blend_radio_results(results, exclude_titles=exclude_titles, limit=self._round_size * 3)
        if avoid_artists:
            import taste_profile
            _before = len(radio)
            radio = taste_profile.filter_avoided(radio, avoid_artists)
            if len(radio) < _before:
                logger.info(f"🚫 [AutoRecommend] T2 avoid 排除 {_before - len(radio)} 首（{avoid_artists}）")
        if not radio:
            return []
        logger.info(f"🎵 [AutoRecommend] T2 discovery: {len(seeds)} seeds 混合 → {len(radio)} 首相關新歌候選")
        from music_recommender import Candidate
        return [
            Candidate(anchor_title=c["title"], anchor_artist=c["artist"],
                      lane="discovery", mode="direct", target_member=None,
                      score=0.0, direct_url=c["url"],
                      discovery_seed_title=c.get("_seed_title", ""))
            for c in radio
        ]

    @staticmethod
    def _seed_title_lookup(mm, seed_video_ids: list[str]) -> dict[str, str]:
        """seed video_id → 曲名，供 T2 解釋層標註「從哪首種子曲找到的」。

        fail-open：mm 缺 all_songs() 或任何比對失敗 → 回空 dict，不擋 T2 主流程
        （解釋是錦上添花，不能因為查不到種子曲名就讓整條 discovery 失敗）。
        """
        needed = set(seed_video_ids)
        lookup: dict[str, str] = {}
        if not needed:
            return lookup
        try:
            for s in mm.all_songs().values():
                if not needed:
                    break
                if not isinstance(s, dict):
                    continue
                vid = extract_video_id(s.get('webpage_url') or s.get('url') or '')
                if vid in needed:
                    lookup[vid] = s.get('title', '')
                    needed.discard(vid)
        except Exception:
            logger.debug("[AutoRecommend] T2 種子曲名查詢失敗，跳過解釋標註", exc_info=True)
            return {}
        return lookup

    # T4 排行榜輪替查詢（華語）——get_charts('TW') 回全球 playlists/藝人不乾淨，改華語搜尋 proxy。
    _T4_CHART_QUERIES = ("華語抒情精選", "華語流行 熱門", "華語 情歌 精選")

    @staticmethod
    def _extract_top_artists(songs: list, n: int = 4) -> list[str]:
        """從歌曲 list（get_top_songs_for_user 回的）抽 top 藝人（artist_of，去重保序、取前 n）。"""
        from taste_fingerprint import artist_of
        out: list[str] = []
        for s in songs:
            a = artist_of(s.get('title', '') if isinstance(s, dict) else '')
            if a and a not in out:
                out.append(a)
        return out[:n]

    async def _t4_fresh_discovery(self, members: list[str], spotlight: str, exclude_titles: list[str]) -> list:  # noqa: ARG002
        """T4 冒險發現：輪到的人(spotlight)的 top 藝人 + 排行榜 → search「還沒播過」的新歌。

        只在 T1/T2/T3 全枯竭才觸發（罕見）→ 值得冒險注入全新歌（使用者訂「觸發難就冒險」）。
        來源＝①spotlight 個人常聽藝人（在場者隨 spotlight 輪替→輪到每個人的歌手）②排行榜輪替。
        排除 avoid_artists（skip≥2 的藝人）。全失敗回 [] → 退最終回收保險。
        """
        mm = getattr(self.bot, 'music_memory', None)
        artists = self._extract_top_artists(
            mm.get_top_songs_for_user(spotlight, limit=20), n=4) if mm is not None else []
        if not artists:  # 該人無史 → 退全域口味指紋核心藝人
            artists = [a for a, _ in self._load_taste_fingerprint().get("core_artists", []) if a][:4]
        # 避開歌手：deterministic skip-avoid + LLM avoid_artists（同 T2 gate/快取）
        avoid_artists = list(mm.get_explore_avoid_artists()) if mm is not None else []
        if os.getenv("LLM_TASTE_T2", "off") == "on":
            try:
                import taste_profile
                _MAX_AGE = 8 * 86400
                # LLM 相近歌手（破回音室、挖他沒聽但會愛的）併入 search 來源
                for _a in taste_profile.fresh_adjacent_artists(_TASTE_PROFILE_CACHE, [spotlight], _MAX_AGE):
                    if _a not in artists:
                        artists.append(_a)
                for _a in taste_profile.fresh_avoid_artists(_TASTE_PROFILE_CACHE, members, _MAX_AGE):
                    if _a not in avoid_artists:
                        avoid_artists.append(_a)
            except Exception as e:
                logger.warning(f"⚠️ [AutoRecommend] T4 LLM taste 讀取失敗，略過: {e}")
        # 排行榜：隨 spotlight 輪替換一條華語 chart 查詢（輪到不同人配不同榜）
        chart_q = self._T4_CHART_QUERIES[self._recommend_spotlight_idx % len(self._T4_CHART_QUERIES)]
        _avoid_set = set(avoid_artists)
        queries = [q for q in (artists + [chart_q]) if q and q not in _avoid_set]
        if not queries:
            return []
        from ytmusic_radio import ytmusic_search_songs, blend_radio_results
        results = []
        for _q in queries:
            try:
                r = await asyncio.to_thread(
                    ytmusic_search_songs, _q,
                    exclude_titles=exclude_titles, limit=self._round_size * 2,
                )
            except Exception as e:
                logger.warning(f"⚠️ [AutoRecommend] T4 search '{_q}' 失敗，跳過: {e}")
                continue
            if r:
                results.append(r)
        if not results:
            logger.warning("⚠️ [AutoRecommend] T4 全 search 空/失敗，退最終回收")
            return []
        fresh = blend_radio_results(results, exclude_titles=exclude_titles, limit=self._round_size * 3)
        # LLM/skip avoid_artists 也套在結果上（chart 查詢可能回避開歌手的歌）
        if avoid_artists:
            import taste_profile
            _before = len(fresh)
            fresh = taste_profile.filter_avoided(fresh, avoid_artists)
            if len(fresh) < _before:
                logger.info(f"🚫 [AutoRecommend] T4 avoid 排除 {_before - len(fresh)} 首（{avoid_artists[:5]}）")
        if not fresh:
            return []
        logger.info(f"🎵 [AutoRecommend] T4 冒險發現: spotlight={spotlight} 藝人+LLM相近={artists} +排行榜『{chart_q}』 avoid={len(avoid_artists)} → {len(fresh)} 首未播新歌候選")
        from music_recommender import Candidate
        return [
            Candidate(anchor_title=c["title"], anchor_artist=c["artist"],
                      lane="discovery", mode="direct", target_member=None,
                      score=0.0, direct_url=c["url"])
            for c in fresh
        ]

    async def _llm_coverify(self, cand, exclude_titles: list[str]) -> str:
        """spotlight lane：請 LLM 推薦選定錨點歌的 cover 版本。回 "" 表示無推薦。"""
        slot = self.bot.music_memory.time_slot(time.time())
        prompt = (
            f"請推薦《{cand.anchor_title}》的【翻唱／cover 版本】（由其他藝人演繹）。\n"
            f"當前時段：{slot}\n"
            f"禁止推薦這些版本：{', '.join(exclude_titles[:20]) or '無'}\n"
            "規則：\n"
            "1. 優先推薦該歌的知名 cover（指定翻唱者更佳）。\n"
            "2. 若無合適 cover，推薦相同曲風／相關藝人的歌。\n"
            "回答格式（一行）：「翻唱藝人 - 歌名 (cover)」或「藝人 - 歌名」。不需要解釋。\n"
            "若真的沒有合適選擇請回答「無推薦」。"
        )
        rec = await self.bot.router._call_llm(
            system_prompt=f"你是 cover/翻唱推薦助手，聚焦在《{cand.anchor_title}》。",
            user_prompt=prompt,
            tier="simple",
        )
        rec = (rec or "").strip()
        return "" if (not rec or "無推薦" in rec) else rec

    def _recommend_blurb(self, cand, title: str, spotlight: str = "",
                         personal: bool = True) -> str:
        """依 lane 產生推薦時的自我說明文案。

        personal=False（歌不在掛名對象的點播歷史）→ 不指名、點給大家
        （2026-07-02 使用者訂：掛名「為X」必須是 X 點過的歌）。
        """
        if cand.lane == "group_resonance":
            return f"🎵 **【馬文精選】** 你們都有共鳴的《{title}》，再聽一次吧。"
        if not personal:
            if cand.lane == "discovery":
                return f"🎵 **【馬文精選】** 挖到新歌《{title}》，點給大家聽聽看。"
            return f"🎵 **【馬文精選】** 翻出《{title}》，點給大家。"
        who = cand.target_member or spotlight or "你"
        if cand.lane == "long_tail":
            return f"🎵 **【馬文精選】** 為 `{who}` 從塵封歌單挖出《{title}》。"
        if cand.lane == "discovery":
            return f"🎵 **【馬文精選】** 為 `{who}` 挖到新歌《{title}》，聽聽看。"
        return f"🎵 **【馬文精選】** 為 `{who}` 翻出的《{title}》。"

    def _themed_gate_open(self, now: float) -> bool:
        """🎚️ 主題歌單觸發閘：env on + 過冷卻 + 未超每晚上限（跨日自動重置）。"""
        if os.getenv("MARVIN_THEMED_PLAYLIST") != "1":
            return False
        today = datetime.date.fromtimestamp(now)
        if today != self._themed_set_date:
            self._themed_set_date = today
            self._themed_sets_tonight = 0
        if now - self._last_themed_set_ts < self._THEMED_SET_COOLDOWN_S:
            return False
        if self._themed_sets_tonight >= self._THEMED_SET_NIGHTLY_CAP:
            return False
        return True

    def _load_summary_entries(self):
        """讀 chat_summary_log → 日記 DiaryEntry（有 ts_str/core/speakers）。失敗回 []。"""
        try:
            from pathlib import Path
            from chat_summary_parser import parse_log
            return parse_log(Path("records/chat_summary_log.txt").read_text(encoding="utf-8"))
        except Exception:
            return []

    @staticmethod
    def _taste_match_owner(title: str, member_likes: dict, order: list) -> str | None:
        """歌名 vs 各成員 suki likes 的歌手強匹配。歌手名(≥2 字)== 抽出歌手 或 出現在歌名 →
        回該成員（依 order 優先）；否則 None。混雜的非音樂興趣(露營/股票)幾乎不會出現在歌名。"""
        from taste_fingerprint import artist_of
        artist = artist_of(title or "")
        for m in order:
            for like in (member_likes.get(m) or []):
                like = str(like).strip()
                if len(like) < 2:
                    continue
                if like == artist or like in (title or ""):
                    return m
        return None

    def _attribution_with_suki(self, mm, info: dict, spotlight: str) -> str:
        """autopilot 掛名：①真的點過→為X（既有）②否則歌手強匹配某在場者 suki 愛歌手→為X
        （記憶影響「為誰點」）③再否則點給大家。強匹配才掛（掛錯名比不掛名傷）。"""
        from music_memory import recommend_attribution, GROUP_ATTRIBUTION
        base = recommend_attribution(mm, info, spotlight)
        if base != GROUP_ATTRIBUTION:
            return base                      # 真的點過 → 保留既有掛名
        suki = getattr(getattr(self.bot, 'router', None), 'memory', None)
        if suki is None:
            return base
        vc = self._vc()
        members = (vc.get_online_members() if vc is not None else []) or ([spotlight] if spotlight else [])
        order = ([spotlight] if spotlight else []) + [m for m in members if m != spotlight]
        likes_map = {}
        for m in order:
            try:
                likes_map[m] = (suki.get_player_memory(m) or {}).get('likes', []) or []
            except Exception:
                likes_map[m] = []
        owner = self._taste_match_owner(info.get('title', ''), likes_map, order)
        return f"Marvin推薦（為{owner}）" if owner else base

    def _enqueue_themed_infos(self, infos: list, theme_title: str, spotlight: str,
                              exclude_titles: list, mm) -> list:
        """成塊入隊：套需 cog 狀態的閘（佇列/正在播去重、ring）+ 標 set 欄位。

        回『實際入隊』的 info 清單（caller 取 len() 當首數、並落日記 record）。
        """
        enqueued: list = []
        for info in infos:
            if self._check_song_duplicate(url=info.get('url', ''), title=info.get('title', ''),
                                          username=spotlight, webpage_url=info.get('webpage_url', '')):
                continue
            if is_already_recommended(info.get('title', ''), exclude_titles):
                continue
            # 掛名規則：themed 選歌通常非 spotlight 點過 → recommend_attribution 走點給大家，
            # 但 _attribution_with_suki 會再用 suki 愛歌手強匹配補「為X」
            info['requested_by'] = self._attribution_with_suki(mm, info, spotlight)
            info['_lane'] = 'themed'
            info['_spotlight'] = spotlight
            info['_set_id'] = theme_title
            info['_round_first'] = (len(enqueued) == 0)
            self.stream_queue.append(info)
            for _rt in ring_titles_for(info.get('title', ''), 'direct', info.get('title', '')):
                mm.add_recent_recommendation(_rt)
            enqueued.append(info)
        if enqueued:
            self._republish_queue_snapshot()
        return enqueued

    @staticmethod
    def _build_themed_announcement(theme_title: str, infos: list) -> str:
        """今夜歌單文字貼文：主題 + 每首歌名與策展理由（_pick_reason）。截到 Discord 2000 上限內。"""
        n = len(infos)
        lines = [f"🎚️ **【今夜歌單】** 我聽你們聊了一晚，為你們策展《{theme_title}》共 {n} 首："]
        for i, info in enumerate(infos, 1):
            title = (info.get('title') or '?').strip()[:60]
            reason = (info.get('_pick_reason') or '').strip()
            lines.append(f"`{i}.` **{title}**" + (f"\n> {reason}" if reason else ""))
        text = "\n".join(lines)
        return (text[:1900] + "…") if len(text) > 1900 else text

    async def _announce_themed_set(self, theme_title: str, enqueued_infos: list) -> None:
        vc = self._vc()
        # 同卡片 fallback：active_text_channel 未設(語音召喚)時退語音頻道內建文字區
        ch = None
        if vc is not None:
            ch = vc.active_text_channel or getattr(getattr(vc, 'voice_client', None), 'channel', None)
        if ch:
            try:
                await ch.send(self._build_themed_announcement(theme_title, enqueued_infos))
            except Exception:
                logger.debug("[ThemedSet] 宣告貼文失敗（忽略）", exc_info=True)

    async def _try_themed_set(self, members: list, exclude_titles: list,
                              spotlight: str, mm) -> int:
        """🎚️ 嘗試策展一張主題歌單入隊。回入隊首數（0 = 沒做 → caller 走一般 autopilot）。

        全程優雅降級：閘關 / 無主題 / LLM 失敗 / resolve 不足 / 任何例外 → 回 0，不中斷音樂。
        """
        if not self._themed_gate_open(time.time()):
            return 0
        try:
            from themed_playlist import (curate_themed_set, gather_theme_brief,
                                         record_themed_set, resolve_themed_set)
            from track_quality import is_non_song_video
            from music_memory import extract_video_id
            from llm_pool import call_paid_review

            brief = gather_theme_brief(self._load_summary_entries(),
                                       self._load_taste_fingerprint(), members, now=time.time())
            if brief is None:
                return 0
            themed = await curate_themed_set(brief, exclude_titles,
                                             call_fn=call_paid_review, set_size=self._round_size * 2)
            if themed is None or not themed.picks:
                return 0
            exclude_vids = mm.get_skipped_video_ids() | mm.get_recently_played_video_ids(
                self._PLAYED_EXCLUDE_TTL_S)
            infos = await resolve_themed_set(
                themed, resolve_fn=self._resolve_yt_query, exclude_vids=exclude_vids,
                is_non_song_fn=is_non_song_video, extract_vid_fn=extract_video_id)
            enqueued_infos = self._enqueue_themed_infos(infos, themed.theme_title, spotlight,
                                                        exclude_titles, mm)
            n = len(enqueued_infos)
            if n == 0:
                logger.info("🎚️ [ThemedSet] resolve+閘後 0 首可入隊 → fallback 一般 autopilot")
                return 0
            record_themed_set(themed.theme_title, enqueued_infos, ts=time.time())  # 落日記「今夜歌單」
            self._themed_sets_tonight += 1
            self._last_themed_set_ts = time.time()
            logger.info(f"🎚️ [ThemedSet]《{themed.theme_title}》入隊 {n} 首"
                        f"（今晚第 {self._themed_sets_tonight} 張）")
            await self._announce_themed_set(themed.theme_title, enqueued_infos)
            return n
        except Exception:
            logger.exception("[ThemedSet] 失敗，fallback 一般 autopilot")
            return 0

    # ── 🎵 Associative curation (對話關聯與歌詞金句選曲) ──────────────────────
    _ASSOCIATIVE_COOLDOWN_S = 900.0  # 15 分鐘冷卻，防聽覺與選曲疲勞
    _last_associative_pick_ts = 0.0

    def _associative_gate_open(self, now: float) -> bool:
        """🎵 關聯性選曲觸發閘：env 開啟（預設 on）+ 過冷卻。"""
        if os.getenv("ASSOCIATIVE_CURATION", "on").strip().lower() in ("off", "0", "false", "no"):
            return False
        if now - getattr(self, "_last_associative_pick_ts", 0.0) < getattr(self, "_ASSOCIATIVE_COOLDOWN_S", 900.0):
            return False
        return True

    async def _try_associative_pick(self, members: list, exclude_titles: list,
                                   spotlight: str, mm) -> int:
        """嘗試從近期對話中挑選 1 首關聯神曲入隊。回入隊首數（0 = 沒做 → caller 走一般流程）。"""
        now = time.time()
        if not self._associative_gate_open(now):
            return 0

        try:
            from associative_curation import curate_associative_song
            from music_recommender import is_already_recommended, ring_titles_for
            from track_quality import is_non_song_video
            from llm_pool import call_paid_review

            # 1. 抓取近期對話
            utts = []
            conv_buf = getattr(getattr(self.bot, 'engine', None), 'conv_buffer', None)
            if conv_buf:
                utts = conv_buf.get_last_n_utterances(15) or []
            if not utts:
                atm = getattr(getattr(self.bot, 'router', None), 'atmosphere_tracker', None)
                if atm and hasattr(atm, '_window'):
                    utts = [{'speaker': e.speaker, 'text': e.text} for e in atm._window]

            utts = [u for u in utts if u.get('speaker') != 'Marvin' and u.get('text', '').strip()]
            if len(utts) < 2:
                return 0

            # 2. 準備口味指紋與在場者
            taste_fp = self._load_taste_fingerprint() if hasattr(self, '_load_taste_fingerprint') else {}
            core_artists = [a for a, _ in (taste_fp.get("core_artists") or [])][:8]

            # 3. LLM 關聯選曲
            pick = await curate_associative_song(
                utts,
                core_artists=core_artists,
                exclude_titles=exclude_titles,
                members=members,
                call_fn=call_paid_review,
            )
            if not pick:
                return 0

            # 4. 解析 YouTube 影片與品質把關
            query = f"{pick.artist} {pick.song}"
            info = await self._resolve_yt_query(query)
            if not info or not info.get('url'):
                logger.info(f"🎵 [AssociativePick] YouTube 搜尋解析失敗: {query}")
                return 0

            if is_non_song_video(info):
                logger.info(f"🛡️ [AssociativePick] 品質閘拒絕非歌曲影片: {info.get('title')}")
                return 0

            if self._check_song_duplicate(url=info.get('url', ''), title=info.get('title', ''),
                                          username=spotlight, webpage_url=info.get('webpage_url', '')):
                return 0

            if is_already_recommended(info.get('title', ''), exclude_titles):
                return 0

            # 5. 標註欄位並入隊
            info['requested_by'] = "Marvin推薦（對話靈感）"
            info['_lane'] = 'associative'
            info['_spotlight'] = spotlight
            info['_dj_line'] = pick.dj_line
            info['_target_lyric'] = pick.target_lyric
            info['_explanation'] = pick.reason
            info['_round_first'] = True

            self.stream_queue.append(info)
            for _rt in ring_titles_for(info.get('title', ''), 'direct', info.get('title', '')):
                if mm:
                    mm.add_recent_recommendation(_rt)
            self._republish_queue_snapshot()
            self._last_associative_pick_ts = now
            logger.info(f"🎵 [AssociativePick] 成功入隊《{info.get('title')}》（話題: {pick.observed_topic}）")
            return 1
        except Exception:
            logger.exception("[AssociativePick] 失敗，fallback 一般 autopilot")
            return 0

