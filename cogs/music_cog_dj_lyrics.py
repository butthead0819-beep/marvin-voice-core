"""
MusicDJLyricsMixin — MusicCog 的歌詞/評論抓取 + DJ 播報內容生成（LLM文字+TTS預渲染）。

從 music_cog.py 抽出（減肥，比照 voice_controller.py 拆解先例），以 mixin 形式
併入 MusicCog：
    class MusicCog(..., MusicDJLyricsMixin, ..., commands.Cog): ...
因此 self 仍是 MusicCog 實例，bot.router / bot.tts_engine / bot.music_memory /
_vc / _life_cores_async / _present_interests / _dj_topic_store /
_recent_emotional_highlight / _autopilot_pick_reason 等
全部沿用原本的 self 存取，行為零改動。

_DJ_TEMPLATES 及其衍生常數（_DJ_EMPATHY_HOOK_TEMPLATES / _AUTOPILOT_DJ_PHRASES_* /
_QUICK_SEGUE_TEMPLATES*）跟著搬進這裡：這些是 class-body 層級（非 self/cls）的
衍生常數宣告，靠 Python 循序 class-body namespace 解析、不是 MRO——留在主檔會讓
這裡的 _QUICK_SEGUE_TEMPLATES* 宣告在 import 時找不到 _DJ_TEMPLATES 炸
NameError。所有消費者（_autopilot_dj_phrase 的 cls.X / _quick_segue_text 的
cls.X / _fetch_dj_interjection_raw 的 self.X）都在這個檔案裡，搬過來後行為
零改動；_autopilot_dj_phrase 已在前一個 commit 改成 classmethod + cls.，避免
搬檔後硬寫 MusicCog 類名造成循環 import。
"""
from __future__ import annotations

import asyncio
import logging
import random
import time

from persona_loader import load_dj_templates

logger = logging.getLogger(__name__)

# 🎤 DJ 口白等歌詞槽的上限（實測歌詞抓取約 3.3s、命中率約一半，6s 讓大多數
# 命中案例趕得上，逾時就放棄這輪歌詞槽，不拖慢整段口白預抓）。
_DJ_LYRICS_WAIT_S = 6.0


class MusicDJLyricsMixin:
    # DJ 播報模板池資料源見 personas/dj_templates.yaml；選池邏輯/random.choice() 呼叫點不動
    _DJ_TEMPLATES = load_dj_templates()
    _DJ_EMPATHY_HOOK_TEMPLATES = tuple(_DJ_TEMPLATES["empathy_hooks"])

    _AUTOPILOT_DJ_PHRASES_PERSONAL = _DJ_TEMPLATES["autopilot_phrases"]["personal"]
    _AUTOPILOT_DJ_PHRASES_PERSONAL_NO_ARTIST = _DJ_TEMPLATES["autopilot_phrases"]["personal_no_artist"]
    _AUTOPILOT_DJ_PHRASES_GROUP = _DJ_TEMPLATES["autopilot_phrases"]["group"]
    _AUTOPILOT_DJ_PHRASES_GROUP_NO_ARTIST = _DJ_TEMPLATES["autopilot_phrases"]["group_no_artist"]
    _AUTOPILOT_DJ_PHRASES_LONG_TAIL = _DJ_TEMPLATES["autopilot_phrases"]["long_tail"]
    _AUTOPILOT_DJ_PHRASES_DISCOVERY = _DJ_TEMPLATES["autopilot_phrases"]["discovery"]
    _AUTOPILOT_DJ_PHRASES_SPOTLIGHT_ANCHOR = _DJ_TEMPLATES["autopilot_phrases"]["spotlight_anchor"]

    # ── 🎵 Song metadata / fetch helpers ────────────────────────────────────────

    def _parse_song_title_artist(self, info: dict) -> tuple[str, str]:
        """從 info 解析出乾淨的 title 和 artist，處理 'Artist - Title' 格式。"""
        raw_title = info.get('title', '')
        artist = info.get('artist') or info.get('uploader', '')
        if ' - ' in raw_title and not info.get('track'):
            parts = raw_title.split(' - ', 1)
            return parts[1].strip(), parts[0].strip()
        return info.get('track') or raw_title, artist

    def _dj_clean_name(self, info: dict) -> tuple[str, str]:
        """DJ 播報專用乾淨歌名（track→catalog videoId→regex 剝雜訊）。與歌詞路徑的
        _parse_song_title_artist 分開：catalog 的「藝人 歌名」合併格式不適合 lrclib 查詞。"""
        # 曲庫正規化過（info['_canon']，canon::<videoId>）就用正規化曲名/歌手（2026-09-29 使用者定）
        canon = info.get('_canon') or {}
        if canon.get('title') and canon.get('artist'):
            return canon['title'], canon['artist']
        from song_name_clean import dj_display_name
        from music_memory import extract_video_id
        return dj_display_name(info, extract_vid=extract_video_id)

    async def _fetch_lyrics_synced(self, info: dict) -> str | None:
        """像 _fetch_lyrics_raw 但保留 [mm:ss.xx] timestamp（給 lyrics_seek 用）。"""
        import aiohttp
        title, artist = self._parse_song_title_artist(info)
        try:
            import syncedlyrics
            lrc = await asyncio.to_thread(
                syncedlyrics.search,
                f"{title} {artist}".strip(),
                providers=["NetEase", "Lrclib", "Musixmatch", "Genius"],
            )
            if lrc and "[" in lrc:
                return lrc
        except Exception as e:
            logger.debug(f"⚠️ [LyricsSynced/syncedlyrics] {e}")
        try:
            async with aiohttp.ClientSession() as session:
                params = {'track_name': title, 'artist_name': artist}
                async with session.get('https://lrclib.net/api/get', params=params,
                                       timeout=aiohttp.ClientTimeout(total=8)) as r:
                    if r.status == 200:
                        data = await r.json()
                        synced = data.get('syncedLyrics')
                        if synced:
                            return synced
        except Exception as e:
            logger.debug(f"⚠️ [LyricsSynced/lrclib] {e}")
        return None

    async def _fetch_lyrics_raw(self, info: dict) -> str | None:
        """Pure lyrics fetch：syncedlyrics (NetEase 優先) → lrclib.net fallback。"""
        import re, aiohttp
        title, artist = self._parse_song_title_artist(info)
        duration = int(info.get('duration') or 0)

        def _strip_lrc(lrc: str) -> str:
            return re.sub(r'\[\d+:\d+\.\d+\]\s?', '', lrc).strip()

        try:
            import syncedlyrics
            lrc = await asyncio.to_thread(
                syncedlyrics.search,
                f"{title} {artist}".strip(),
                providers=["NetEase", "Lrclib", "Musixmatch", "Genius"],
            )
            if lrc:
                return _strip_lrc(lrc)
        except Exception as e:
            logger.debug(f"⚠️ [Lyrics/syncedlyrics] {e}")

        try:
            async with aiohttp.ClientSession() as session:
                params = {'track_name': title, 'artist_name': artist, 'duration': duration}
                async with session.get('https://lrclib.net/api/get', params=params,
                                       timeout=aiohttp.ClientTimeout(total=8)) as r:
                    if r.status == 200:
                        data = await r.json()
                        plain = data.get('plainLyrics') or ''
                        if plain:
                            return plain
        except Exception as e:
            logger.debug(f"⚠️ [Lyrics/lrclib] {e}")
        return None

    async def _fetch_lyrics_control_card(self, info: dict) -> str | None:
        """控制台卡片歌詞：純 lrclib.net plainLyrics，不逐行同步、不快取（每首開播現查一次）。
        跟 _fetch_lyrics_raw（DJ 評論用，syncedlyrics 多來源）分開——那條有既有用途不動它。"""
        import aiohttp
        title, artist = self._parse_song_title_artist(info)
        duration = int(info.get('duration') or 0)
        try:
            async with aiohttp.ClientSession() as session:
                params = {'track_name': title, 'artist_name': artist, 'duration': duration}
                async with session.get('https://lrclib.net/api/get', params=params,
                                       timeout=aiohttp.ClientTimeout(total=8)) as r:
                    if r.status == 200:
                        data = await r.json()
                        return data.get('plainLyrics') or None
        except Exception as e:
            logger.debug(f"⚠️ [Lyrics/ControlCard/lrclib] {e}")
        return None

    async def _fetch_comment_raw(self, info: dict) -> str | None:
        """Pure Marvin commentary fetch via LLM，注入使用者音樂記憶。"""
        parts = [f"歌名：{info['title']}，頻道：{info.get('uploader', '')}"]
        requested_by = info.get('requested_by', '')
        if requested_by and not requested_by.startswith('Marvin'):
            parts.append(f"點播者：{requested_by}")
            if hasattr(self.bot, 'music_memory'):
                music_ctx = self.bot.music_memory.get_user_music_context(requested_by)
                if music_ctx:
                    parts.append(music_ctx)
        try:
            return await self.bot.router.generate_dynamic_system_msg(
                "stream_now_playing", context="\n".join(parts)
            )
        except Exception:
            return None

    @staticmethod
    def _dj_requester_suffix(requester: str) -> str:
        """把 requested_by 轉成播報結尾——只有『真人點播』才能講「XX 點的」；
        autopilot 掛名（`requested_by` 以 "Marvin" 開頭，如 `Marvin推薦（為X）`）
        不是使用者真的點播，講成「XX 點的」等於考驗使用者記憶（萬一沒點過呢），
        改用「希望大家喜歡」這種機器人自己推薦的說法。"""
        requester = (requester or '').strip()
        if not requester or requester.startswith('Marvin'):
            return "希望大家喜歡"
        return f"{requester} 點的"

    @classmethod
    def _autopilot_dj_phrase(cls, spotlight: str, clean_title: str, clean_artist: str,
                              lane: str = "", anchor: str = "") -> str:
        """為 autopilot 推薦歌曲生成 DJ 台詞，理由依 lane 而定（DJ 編個理由）。"""
        import random
        who = spotlight or "你"
        if lane == "group_resonance":
            pool = (cls._AUTOPILOT_DJ_PHRASES_GROUP if clean_artist
                    else cls._AUTOPILOT_DJ_PHRASES_GROUP_NO_ARTIST)
        elif lane == "long_tail":
            pool = cls._AUTOPILOT_DJ_PHRASES_LONG_TAIL
        elif lane == "discovery":
            pool = cls._AUTOPILOT_DJ_PHRASES_DISCOVERY
        elif anchor and anchor != clean_title:
            pool = cls._AUTOPILOT_DJ_PHRASES_SPOTLIGHT_ANCHOR
        else:
            pool = (cls._AUTOPILOT_DJ_PHRASES_PERSONAL if clean_artist
                    else cls._AUTOPILOT_DJ_PHRASES_PERSONAL_NO_ARTIST)
        tmpl = random.choice(pool)
        return tmpl.format(who=who, title=clean_title, artist=clean_artist, anchor=anchor)

    @staticmethod
    def _autopilot_pick_reason(info: dict) -> str:
        """autopilot 選這首的理由（給 DJ LLM 當素材，語意同 _autopilot_dj_phrase 的 lane 分流）。

        優先用 `info['_explanation']`（`_compute_recommend_explanation` 算好的 grounded
        解釋，見 explanation_slotfill.py）——比下面 lane 分流的固定樣版更具體、更可查證
        （例如 T2 discovery 會有「YouTube Music 常把這首和你們聽過的《XX》放在一起」，
        而非「照口味挖出來的新歌」這種空泛說法）。沒有 explanation（例如沒 evidence
        可用）才退回原本 lane 分流的固定樣版。
        """
        explanation = info.get('_explanation')
        if explanation:
            return explanation
        who = info.get('_spotlight', '') or '大家'
        lane = info.get('_lane', '')
        if lane == 'group_resonance':
            return "這首是大家都有共鳴的歌"
        if lane == 'long_tail':
            return f"{who} 很久沒點到這首了"
        if lane == 'discovery':
            return f"照 {who} 的口味挖出來的新歌"
        anchor = info.get('_anchor_title', '')
        if anchor:
            return f"因為 {who} 點過《{anchor}》才接這首"
        return f"這首是 {who} 平常會聽的歌"

    @staticmethod
    def _current_season() -> str:
        """由當前月份推台北季節（北半球）。給 DJ 串場的環境沉浸用。"""
        mon = time.localtime().tm_mon
        if mon in (3, 4, 5):
            return "春天"
        if mon in (6, 7, 8):
            return "夏天"
        if mon in (9, 10, 11):
            return "秋天"
        return "冬天"

    @staticmethod
    def _city_label() -> str:
        """車載 ESP32 puck 的 GPS 訊號 → DJ 環境行用的城市/區名。

        沒有新鮮訊號（不在車上）時退回「台中」（家裡預設）。讀檔/座標推算失敗
        不該讓 DJ 串場掛掉，走跟 _life_cores_async 一樣的降級哲學。
        """
        try:
            from gps_context import city_label
            from location_state import load_location_state
            return city_label(load_location_state(), now=time.time())
        except Exception:
            return "台中"

    _QUICK_SEGUE_TEMPLATES = tuple(_DJ_TEMPLATES["quick_segue"]["default"])
    _QUICK_SEGUE_TEMPLATES_INTIMATE = tuple(_DJ_TEMPLATES["quick_segue"]["intimate"])
    _QUICK_SEGUE_TEMPLATES_ENERGETIC = tuple(_DJ_TEMPLATES["quick_segue"]["energetic"])

    @classmethod
    def _quick_segue_text(cls, n_online: int = 0) -> str:
        """沒有任何話題/素材可用時的本地過場模板——純接歌，跳過 LLM。

        n_online 決定語氣（跟原本 group-size ctx 提示同一套門檻），quick 模式
        沒有 LLM 可以照 ctx 調語氣，改本地挑模板池達到同樣效果。
        """
        import random
        if n_online == 1:
            pool = cls._QUICK_SEGUE_TEMPLATES_INTIMATE
        elif n_online >= 4:
            pool = cls._QUICK_SEGUE_TEMPLATES_ENERGETIC
        else:
            pool = cls._QUICK_SEGUE_TEMPLATES
        return random.choice(pool)

    def _life_cores(self, entries, now: float,
                    present_speakers: set[str] | None = None) -> list:
        """日記 entries → DJ 雞湯用的近日生活素材（純函式包裝，測試用此點注入）。

        回傳 LifeCore 列表（含事件主角），供 dj_topic_selector.select_mode 判斷
        主角現在在不在場。present_speakers: 在場人集合，傳給
        recent_life_cores_with_speakers 做 privacy filter。None = 不過濾
        （fail-open，vc 不可用時的預設）。
        """
        from dj_life_context import recent_life_cores_with_speakers
        return recent_life_cores_with_speakers(entries, now=now, present_speakers=present_speakers)

    async def _life_cores_async(self) -> list[str]:
        """讀日記檔取生活素材。606K 檔的 read+parse 走 to_thread，不阻塞 event loop。
        任何失敗回 []（DJ 少一味料，不該讓整條串場掛掉）。

        在場人（vc.get_online_members）傳給 privacy filter，
        讓敏感 entry 在參與者不全在場時自動過濾。
        vc 不可用 → present_speakers=None（不過濾，fail-open）。
        """
        present_speakers: set[str] | None = None
        try:
            _vc = self._vc()
            if _vc is not None:
                present_speakers = set(_vc.get_online_members())
        except Exception as e:
            logger.debug(f"[DJ Life] 讀在場人失敗，privacy filter 跳過: {e}")
        try:
            entries = await asyncio.to_thread(self._load_summary_entries)
            return self._life_cores(entries, time.time(),
                                    present_speakers=present_speakers)
        except Exception as e:
            logger.debug(f"⚠️ [DJ Life] 生活素材抽取失敗，DJ 改走無生活素材: {e}")
            return []

    async def _fetch_news_items_async(self, interests: list[str]) -> list[str]:
        """從 Google News 獲取適合電台播報的乾淨新聞（已過濾受傷/死亡/政治/八卦）。"""
        if not getattr(self, "_enable_dj_news_fetch", True):
            return []
        try:
            from news_fetch import fetch_news_headline
            _kw = interests[0] if interests else None
            _news_res = await fetch_news_headline(_kw)
            if _news_res and _news_res.get('title'):
                return [_news_res['title']]
        except Exception as e:
            logger.debug(f"[DJ News] 新聞抓取失敗: {e}")
        return []

    def _dj_heat_bank(self):
        """DJ 話題庫的 lazy 單例（熱聊時存快照，降溫時取出接回話題）。"""
        bank = getattr(self, '_dj_topic_bank', None)
        if bank is None:
            from dj_heat import TopicBank
            bank = TopicBank()
            self._dj_topic_bank = bank
        return bank

    def _dj_topic_store(self):
        """DJ 話題冷卻表的 lazy 單例（跨呼叫共用同一份記憶體狀態＋disk-backed）。"""
        store = getattr(self, "_dj_topic_cooldown_store", None)
        if store is None:
            from dj_topic_selector import TopicCooldownStore
            store = TopicCooldownStore()
            self._dj_topic_cooldown_store = store
        return store

    def _attach_cached_canon(self, info: dict) -> None:
        """info 還沒掛 _canon 就從曲庫快取（canon::<videoId>）補上——只讀本地快取、零網路，
        給歌曲卡用正規化曲名/歌手。任何失敗靜默略過（卡片退回原標題）。"""
        if info.get('_canon'):
            return
        try:
            from music_memory import extract_video_id
            vid = extract_video_id(info.get('webpage_url') or info.get('url') or '')
            if not vid:
                return
            store, _guard, _router = self._audiophile_deps()
            canon = store.get(f"canon::{vid}")
            if canon:
                info['_canon'] = canon
        except Exception as e:
            logger.debug(f"[Canon] 讀快取失敗，歌曲卡用原標題: {e}")

    def _shazam_breaker_lazy(self):
        b = getattr(self, '_shazam_breaker', None)
        if b is None:
            from shazam_identify import ShazamBreaker
            b = ShazamBreaker()
            self._shazam_breaker = b
        return b

    async def _dj_song_material(self, info: dict, clean_title: str, clean_artist: str) -> tuple[dict | None, str | None]:
        """DJ 串場的歌曲素材：(canon, guide)。canon 是 iTunes 正規化後的歌手/歌名/專輯/
        年份（resolve_canon，零幻覺），guide 是長版導聆稿（song_guide_for_dj）。

        canon 決定導聆稿要用哪把 label（有正規化名字優先用，避免 YouTube 髒標題各自
        查一次）；human（真人點歌）才可以燒付費額度，autopilot 只打免費層且受
        AutoGuideBudget 節流（budget/inflight 是這個 cog 生命週期內的 lazy 單例）。
        store 跟 /guide_song、/tour 共用同一實例（見 _audiophile_deps，多實例整份
        寫檔會互蓋）。"""
        from music_memory import extract_video_id
        from audiophile_fetcher import _song_label, resolve_canon, song_guide_for_dj

        store, guard, router = self._audiophile_deps()
        video_id = extract_video_id(info.get('webpage_url') or info.get('url') or '')

        canon = None
        try:
            canon = await resolve_canon(
                store, video_id, clean_title, clean_artist,
                artist_hay=f"{info.get('title') or ''} {info.get('uploader') or ''}",
                stream_url=info.get('url') or '',
                duration=info.get('duration'),
                breaker=self._shazam_breaker_lazy(),
            )
        except Exception as e:
            logger.debug(f"[Canon] 正規化失敗，跳過: {e}")
            canon = None

        if canon:
            info['_canon'] = canon   # 這首開播貼歌曲卡時用正規化曲名/歌手
        label = _song_label(canon['title'], canon['artist']) if canon else _song_label(clean_title, clean_artist)

        requester = info.get('requested_by') or ''
        # 個人歌單自動墊歌掛的是真人名字，但不是真人當下點的，跟 autopilot 一樣只走免費
        human = not requester.startswith('Marvin') and info.get('_lane') != 'personal'

        free_client = getattr(router, 'google_client', None)
        paid_client = getattr(router, 'google_paid_client', None) if human else None
        if free_client is None and paid_client is None:
            return canon, None

        budget = getattr(self, '_auto_guide_budget', None)
        if budget is None:
            from audiophile_fetcher import AutoGuideBudget
            budget = AutoGuideBudget()
            self._auto_guide_budget = budget

        inflight = getattr(self, '_guide_inflight', None)
        if inflight is None:
            inflight = {}
            self._guide_inflight = inflight

        guide = await song_guide_for_dj(
            label, human=human, free_client=free_client, paid_client=paid_client,
            guard=guard, store=store, budget=budget, inflight=inflight,
        )
        if store and guide:
            from audiophile_fetcher import _KEY_PREFIX
            cached = store.get(_KEY_PREFIX + label)
            if cached and cached.get("audiophile_guide"):
                info['_song_card'] = {
                    "audiophile_guide": cached.get("audiophile_guide"),
                    "lyric_hook": cached.get("lyric_hook"),
                }
        return canon, guide

    def _present_interests(self) -> list[str]:
        """在場成員在 suki_memory 的興趣，供話題選擇器沒有『最近生活』可用時當引子。
        任何失敗回 []（DJ 少一味料，不該讓整條串場掛掉）。"""
        try:
            suki = getattr(getattr(self.bot, 'router', None), 'memory', None)
            vc = self._vc()
            if suki is None or vc is None:
                return []
            out = []
            for m in vc.get_online_members():
                # 按最近才被強化排序，別老是講分數最高的舊愛好（跳針）。
                for like in suki.get_recent_liked_items(m, limit=2):
                    like = str(like).strip()
                    if like:
                        out.append(f"{m}喜歡{like}")
            return out
        except Exception:
            return []

    _EMOTIONAL_HIGHLIGHT_MAX_AGE_S = 8 * 86400  # 跟 taste_profile 其他 freshness window 一致

    def _recent_emotional_highlight(self, requester: str) -> str:
        """requester 最近一則「讓 Marvin 情緒波動的瞬間」（見 gemini_router_content.py
        extract_emotional_moments / suki_memory.add_emotional_highlight），供 DJ 話題選擇器
        當第三優先話題。只取 warm/surprised/moved——annoyed 不當 DJ 素材（串場裡講『你讓我
        不爽』很怪，跟這個場合的語氣不合）。8 天內才算新鮮。任何失敗回 ""（DJ 少一味料，
        不該讓整條串場掛掉，同 _present_interests 的降級哲學）。
        """
        try:
            suki = getattr(getattr(self.bot, 'router', None), 'memory', None)
            if suki is None or not requester:
                return ""
            highlights = suki.get_player_memory(requester).get('emotional_highlights', [])
            if not isinstance(highlights, list):
                return ""
            now = time.time()
            for h in reversed(highlights):
                if not isinstance(h, dict):
                    continue
                if h.get('valence') == 'annoyed':
                    continue
                ts = h.get('timestamp')
                if not isinstance(ts, (int, float)) or now - ts > self._EMOTIONAL_HIGHLIGHT_MAX_AGE_S:
                    continue
                moment = str(h.get('moment', '')).strip()
                if moment:
                    return moment
            return ""
        except Exception:
            return ""

    async def _fetch_dj_interjection_raw(self, info: dict, lyrics_task=None) -> dict | None:
        """預先生成 DJ 播報：LLM 文字 + TTS 預渲染音訊。回傳 {'text', 'audio_path'} 或 None。"""
        requester = info.get('requested_by', '')
        if not requester:
            return None

        if requester.startswith('Marvin'):
            _pos = info.get('_round_position', 0)
            if _pos > 0:
                await asyncio.sleep(_pos * 3.0)

        mm = getattr(self.bot, 'music_memory', None)
        play_count, feelings, lyric_match = 0, [], ''
        if mm:
            key = mm._key(info)
            song_data = mm._data.get('songs', {}).get(key, {})
            play_count = song_data.get('requesters', {}).get(requester, 0)
            r = song_data.get('reactions', {}).get(requester, {})
            feelings = r.get('feelings', [])
            lyric_match = r.get('lyric_match', '')

        conv_lines = []
        conv_buf = getattr(getattr(self.bot, 'engine', None), 'conv_buffer', None)
        if conv_buf:
            for entry in conv_buf.get_last_n_utterances(4):
                if entry.get('speaker') != 'Marvin':
                    conv_lines.append(f"{entry['speaker']}：「{entry['text'][:25]}」")

        slot = mm.time_slot(time.time()) if mm else ''
        title = info.get('title', '')
        # 餵 LLM 用乾淨歌名（別給完整 YouTube 標題，否則 DJ 會照唸一長串）。
        # 之前正規化過的歌先從曲庫快取補上 _canon，_dj_clean_name 就會回正規化名字。
        self._attach_cached_canon(info)
        _clean_t, _clean_a = self._dj_clean_name(info)
        _song_label = f"{_clean_a} - {_clean_t}" if _clean_a else _clean_t
        ctx = [f"歌曲：{_song_label or title}", f"點播者：{requester}"]
        song_candidates: list[str] = []
        lyric_candidates: list[str] = []
        # 串場不提上一首（9/30 使用者定：專注寫下一首）——預抓時的「上一首」常因插播/
        # 換順序過期，寫進 prompt 再被 Consistency Guard 丟掉改唸報幕，得不償失。
        if play_count >= 2:
            song_candidates.append(f"喜好線索：這首是 {requester} 常聽的愛歌")
        if feelings:
            song_candidates.append(f"情感記錄：{' / '.join(feelings[:2])}")
        if lyric_match:
            lyric_candidates.append(f"歌詞呼應：{lyric_match[:60]}")

        _vc_ref = None
        present_members: set[str] | None = None
        try:
            _vc_ref = self._vc()
            if _vc_ref is not None:
                present_members = set(_vc_ref.get_online_members())
        except Exception:
            pass  # fail-open：vc 不可用時不過濾在場人

        # 🔥 [DJ Heat] 預抓時：熱聊中就存話題快照；話題庫有新鮮存貨就這首改走 revival 接回話題
        # （2026-09-30 使用者定：熱聊素材最多但沒人在聽 DJ，該熱聊時少講，降溫時延續話題）。
        revival_lines: list[str] = []
        try:
            from dj_heat import is_hot
            _entries = conv_buf.get_history() if conv_buf else []
            _now = time.time()
            if is_hot(_entries, len(present_members or ()), _now):
                self._dj_heat_bank().snapshot(_entries, _now)
            revival_lines = self._dj_heat_bank().take(_now)
        except Exception:
            revival_lines = []  # fail-open

        from dj_social_affinity import (
            find_song_social_affinity,
            find_spoken_taste_match,
            format_temporal_atmosphere,
        )

        affinity = find_song_social_affinity(mm, info, requester, present_members)
        if affinity:
            song_candidates.append(f"喜好線索：{affinity}")

        spoken_match = None
        try:
            _suki_mem = getattr(getattr(self.bot, 'router', None), 'memory', None)
            _spotlight = info.get('_spotlight', '') or ''
            _people = ([requester] if not requester.startswith('Marvin') else ([_spotlight] if _spotlight else []))
            _people += sorted(present_members or [])
            spoken_match = find_spoken_taste_match(_suki_mem, _clean_t, _clean_a, _people)
        except Exception:
            spoken_match = None  # fail-open：記憶讀取失敗不影響 DJ
        memory_evidence = (info.get('_state_reason') or '') or spoken_match or affinity or ""

        # 🎵 歌曲素材：iTunes 正規化（歌手/歌名/專輯/年份，零幻覺）+ 長版導聆稿（查證過的真實資料）
        canon, guide = None, None
        try:
            canon, guide = await self._dj_song_material(info, _clean_t, _clean_a)
            if canon and canon.get('title') and canon.get('artist'):
                # 這輪才正規化完：開頭「歌曲：」那行與後面笑話比對/長度閘門改用正規化名字
                _clean_t, _clean_a = canon['title'], canon['artist']
                _song_label = f"{_clean_a} - {_clean_t}"
                ctx[0] = f"歌曲：{_song_label}"
            if canon and canon.get('album'):
                song_candidates.append(
                    f"歌曲資料：{canon['artist']}《{canon['album']}》"
                    + (f"（{canon['year']}）" if canon.get('year') else "")
                )
        except Exception:
            pass  # fail-open：歌曲素材查證異常不影響 DJ 生成

        # 環境沉浸：城市/區（GPS 訊號，沒有則退回台北）+ 季節（日期推）+ 星期/時段。
        # 不再無條件塞進 ctx——只有 mode == "atmosphere" 被選中時才當開場素材用，
        # 其餘時候別讓它變成 LLM 隨手可用的預設開場（治「每次都靠環境/天氣開場」）。
        season = self._current_season()
        city = self._city_label()
        env = format_temporal_atmosphere(city, season, slot)
        # 本地決定這次串場怎麼寫，LLM 不必自己判斷「有沒有話題、要不要硬掰、
        # 這件事是不是點播者本人的」——樣版/素材/在場判斷全部本地做完，LLM 只負責
        # 把選定的素材寫成自然的過場文字。
        # 順序：近期生活（主角要在場，否則換下一個候選）→ 在場興趣 → 都沒有時在
        # 對話銜接/氣氛/純接歌 之間本地輪替（治「每次都靠環境/天氣開場」）。
        from dj_narration_orchestrator import select_narration_mode
        life = await self._life_cores_async()
        interests = self._present_interests()
        _emo_highlight = self._recent_emotional_highlight(requester)
        emotional_highlights = [_emo_highlight] if _emo_highlight else []

        # 獲取安全生活/科技新聞素材（已在 news_fetch 過濾政治/受傷/死亡）
        news_items = await self._fetch_news_items_async(interests)

        # autopilot 策展理由算在 mode 選擇之前：有理由可講時別讓它被 fallback 輪替
        # 排進 quick（quick 沒素材時直接跳過 LLM，會把這個好料浪費掉）。
        _autopilot_reason = ''
        if requester.startswith('Marvin'):
            _autopilot_reason = self._autopilot_pick_reason(info) or ''

        # 🔥 [DJ Heat] 話題庫有東西可接回 → 直接走 revival，不讓扭蛋池蓋過去
        # （降溫時的第一要務是接回剛剛聊的話題，不是照常規話題優先序抽獎）。
        if revival_lines:
            topic, mode = None, "revival"
            logger.info(f"🔥 [DJ Heat] 話題庫接回 {len(revival_lines)} 句 → revival")
        else:
            # select_mode 挑話題來源 + autopilot 理由覆蓋 quick/atmosphere 這兩步，
            # 原封不動交給 orchestrator（見 dj_narration_orchestrator.select_narration_mode
            # 的 characterization test）。
            topic, mode = select_narration_mode(
                life=life,
                interests=interests,
                topic_store=self._dj_topic_store(),
                present_members=present_members,
                has_conversation=bool(conv_lines),
                emotional_highlights=emotional_highlights,
                news_items=news_items,
                autopilot_reason=_autopilot_reason,
                memory_evidence=memory_evidence,
                has_guide=bool(guide),
            )

        # 開場鉤子提示依「歌會中的心理機制」分兩類套用：
        #   代入感（life/interest）——這是聽眾自己的事，別只是轉述，要讓人覺得被說中。
        #   氣氛精準（atmosphere）——緊扣這個時間/地點，像特別為這一刻準備的。
        # conversation 本身就是銜接類，維持原本的過場方向指示即可。
        if mode == "memory_match":
            ctx.append(f"【你熟悉他的生活】記憶證據（這首為什麼現在放）：\n・{topic}")
            ctx.append("開場鉤子：開場直接點名講出這條記憶證據，讓對方聽得出你記得他說過/做過的事；只能講證據裡寫的事實，不准自己補細節或編故事。")
        elif mode == "life":
            ctx.append(f"【你熟悉他的生活】最近生活：\n・{topic}")
            ctx.append(random.choice(self._DJ_EMPATHY_HOOK_TEMPLATES))
        elif mode == "interest":
            ctx.append(f"【你熟悉他的生活】在場興趣：\n・{topic}")
            ctx.append(random.choice(self._DJ_EMPATHY_HOOK_TEMPLATES))
        elif mode == "emotional_highlight":
            # 這是 Marvin 自己（機器人）的記憶與反應，不是聽眾的事——跟 life/interest
            # 的「代入感」方向相反，robot_pov_rule 對「第一人稱」的限制在這裡要放行。
            ctx.append(f"你（機器人自己）記得的一個瞬間：\n・{topic}")
            ctx.append("開場鉤子：這是你自己的記憶與反應，可以用第一人稱提起這個瞬間，不是在講聽眾的事。")
        elif mode == "news":
            ctx.append(f"最新時事消息：\n・{topic}")
            ctx.append("開場鉤子：簡潔提及這則時事消息，像電台順帶關心生活一樣，自然引導大家聽下一首歌，不說教、不嚴肅。")
        elif mode == "conversation":
            if conv_lines:
                ctx.append("【你熟悉他的生活】頻道近期對話：\n" + '\n'.join(conv_lines))
            ctx.append("串場方向：用剛才頻道對話的氣氛自然接過去就好，不用硬掰新話題。")
        elif mode == "revival":
            ctx.append("【你熟悉他的生活】剛剛大家聊過（原句）：\n" + "\n".join(revival_lines))
            ctx.append("串場方向：現在大家聊天告一段落，接回剛剛的話題延續一下，或丟個輕鬆的問題製造話題感，再帶進這首歌；只能用上面原句裡的內容，不准編造誰說了什麼、不准替人下結論。")
        elif mode == "atmosphere":
            ctx.append(env)
            ctx.append("開場鉤子：緊扣現在的時間/地點氛圍切入，像是特別為這一刻準備的，不用硬掰別的話題。")
        elif mode == "guide":
            ctx.append(f"導聆素材（查證過的真實資料，只能用這裡寫的事實）：\n{guide}")
            from dj_gacha_narrator import pick_gacha_motivation
            card = info.get('_song_card')
            if not card and isinstance(guide, str):
                card = {"audiophile_guide": guide}
            if isinstance(card, dict):
                if card.get("lyric_hook") and isinstance(card["lyric_hook"], dict):
                    lh = card["lyric_hook"]
                    ctx.append(f"歌詞靈魂刺點：『{lh.get('quote')}』（{lh.get('subtext')}）")
            gacha = pick_gacha_motivation(card, topic=topic)
            if gacha:
                ctx.append(gacha.instruction)
                ctx.append("只能講上面素材裡寫的事實，不准自己補細節或編故事。")
            else:
                ctx.append("串場方向：把導聆素材濃縮成一兩句，點出這首歌耳朵該聽的地方；只能講素材裡寫的事實，不准自己補細節或編故事。")
        _song_pick = None
        if _autopilot_reason:
            _reason_line = f"選這首的理由：{_autopilot_reason}"
            if mode == "reason":
                ctx.append(_reason_line)   # reason 就是這輪的主素材
            else:
                song_candidates.append(_reason_line)

        # 🎤 歌詞槽（9/30 使用者定：老朋友想分享這首的原因）——等真實歌詞抓完（上限 6s）挑重複最多的一句
        # 放在生活/新聞素材都查完之後：歌詞抓取跟那些 await 同時在跑，這裡多半已經好了
        _lyric_line = None
        if lyrics_task is not None:
            try:
                _lyrics = await asyncio.wait_for(asyncio.shield(lyrics_task), timeout=_DJ_LYRICS_WAIT_S)
                from dj_lyric_pick import pick_chorus_line
                _lyric_line = pick_chorus_line(_lyrics if isinstance(_lyrics, str) else None)
            except Exception:
                _lyric_line = None  # 逾時/失敗：這輪沒有歌詞槽
        if _lyric_line:
            lyric_candidates.append(f"歌詞：『{_lyric_line}』")

        # 歌曲類/歌詞類素材各抽 1 個（9/30 使用者定：老朋友三槽——生活/品味/歌詞，
        # 每槽最多 1 個，不再全部疊上去造成混線）。
        # guide 本身就是歌曲素材；quick 不走 LLM——這兩個 mode 不抽。
        _lyric_pick = None
        if mode not in ("guide", "quick"):
            from dj_narration_orchestrator import pick_song_material
            _song_pick = pick_song_material(
                song_candidates, exclude_text=(topic or "") if mode == "memory_match" else "")
            if _song_pick:
                ctx.append(f"【你懂他的音樂品味】{_song_pick}")
            _lyric_pick = pick_song_material(lyric_candidates)
            if _lyric_pick:
                ctx.append(f"【你想跟他分享這首的原因】{_lyric_pick}")
        logger.info(f"🎰 [DJ Material] 主={mode} 品味={(_song_pick or '')[:24]!r} 歌詞={(_lyric_pick or '')[:24]!r}（候選 品味{len(song_candidates)}/歌詞{len(lyric_candidates)}）")

        # Group size & Chat Heat → 語氣：綜合在線人數與 AtmosphereTracker 對話活躍度。
        # vc() 不可用時靜默略過。quick 模式沒有 LLM 可以照 ctx 調語氣，改本地選模板池。
        _n_online = 0
        _heat_mode = "default"
        try:
            if _vc_ref is not None:
                _n_online = len(_vc_ref.get_online_members())
                _tracker = getattr(getattr(self.bot, 'router', None), 'atmosphere_tracker', None)
                from dj_social_affinity import assess_channel_heat
                _heat_mode, _heat_instr = assess_channel_heat(_tracker, conv_buf, _n_online)
                if _heat_instr:
                    ctx.append(_heat_instr)
        except Exception:
            pass  # fail-open：語氣注入失敗不影響 DJ 生成

        text = ''
        _source = ''
        _llm_raw = None
        _llm_cleaned = None
        _disqualify = None
        if not text and info.get('_lane') == 'associative':
            text = (info.get('_dj_line') or '').strip()  # 關聯選曲：直接使用 45-55 字金句串場詞，不重複燒 LLM
            if text:
                _source = "associative"

        # 🎭 [DJ Joke Interlude] 頻道安靜（非熱烈聊天）且距上次超過冷卻時間 → 這輪
        # crossfade 換成馬文式厭世冷笑話。改用「策展笑話庫 + 歌名拼音比對」（見
        # joke_bank.py / personas/joke_bank.yaml）：下一首歌名字音撞到哪則笑話的 hook
        # 就播那則，沒撞到就不講（fallback 回正常串場）。LLM 現編諧音梗實測品質不穩，
        # 已改成純本地查表（零 LLM、零花費、品質有下限）。
        _last_joke_ts = getattr(self, '_last_dj_joke_ts', None)
        if not text and _heat_mode != "active_chat" \
                and _last_joke_ts is not None \
                and (time.time() - _last_joke_ts) >= getattr(self, '_DJ_JOKE_COOLDOWN_S', 1800):
            try:
                from joke_bank import get_joke_bank
                from music_memory import extract_video_id
                _recent = getattr(self, '_recent_dj_jokes', ())
                _vid = extract_video_id(info.get('webpage_url') or info.get('url') or info.get('id') or '')
                joke_text = get_joke_bank().match(
                    _song_label or title, video_id=_vid, exclude=set(_recent)) or ""
            except Exception as e:
                logger.warning(f"⚠️ [DJ Joke] 笑話庫比對失敗: {e}")
                joke_text = ""
            if 10 <= len(joke_text) <= 120:
                text = joke_text
                _source = "joke"
                self._last_dj_joke_ts = time.time()
                self._recent_dj_jokes = (list(getattr(self, '_recent_dj_jokes', ()))[-4:] + [joke_text])
        if not text and mode == "quick":
            # 沒有任何素材可用 → 本地固定模板直接接歌，跳過 LLM（零出錯風險、零延遲、零花費）。
            text = self._quick_segue_text(_n_online)
            _source = "quick"
        if not text:
            # autopilot 與真人點歌共用這條 LLM 雞湯（走 tier=simple 免費層）。
            try:
                text = await self.bot.router.generate_dynamic_system_msg(
                    'dj_interjection', context='\n'.join(ctx)
                )
            except Exception as e:
                logger.warning(f"⚠️ [DJ Prefetch] LLM 失敗: {e}")
                text = ""
            text = (text or '').strip()
            _llm_raw = text
            from dj_script_clean import clean_dj_script
            _raw_len = len(text)
            text = clean_dj_script(text)
            _llm_cleaned = text
            _source = "llm"
            if len(text) != _raw_len:
                logger.info(f"🧹 [DJ Prefetch] 口白清雜訊 {_raw_len}→{len(text)} 字")

            from dj_prompt_builder import FORBIDDEN_DJ_PHRASES

            # 9/30 使用者定：清雜訊後多長都完整播出，不截斷、不因超長退墊底（聽一段時間再調）。
            def _is_qualified_dj_script(s: str) -> bool:
                if not s or len(s) < 10:
                    return False
                for fb in FORBIDDEN_DJ_PHRASES:
                    if fb in s:
                        return False
                return True

            if not text or not _is_qualified_dj_script(text):
                # 落空原因要留 log：退模板這條原本無聲，近兩天 22% 串場走這裡卻查不出為什麼
                _why = ("空字串" if not text else f"長度{len(text)}" if len(text) < 10 else "禁詞")
                _disqualify = _why
                logger.info(f"🎙️ [DJ Prefetch] LLM 串場不合格({_why}, mode={mode}): {text[:40]!r}")
                # 1. 優先嘗試 autopilot 模板（若為 Marvin 自己選歌）
                if requester.startswith('Marvin'):
                    from song_name_clean import clean_title_regex
                    clean_title, clean_artist = self._dj_clean_name(info)
                    text = self._autopilot_dj_phrase(
                        info.get('_spotlight', ''), clean_title, clean_artist,
                        lane=info.get('_lane', ''),
                        anchor=clean_title_regex(info.get('_anchor_title', '')),
                    )
                    if text:
                        _source = "autopilot_template"

                # 若仍無有效台詞或品質不符，採用 DJ Marvin 經典人設報幕
                if not text or not _is_qualified_dj_script(text):
                    clean_title, clean_artist = self._dj_clean_name(info)
                    suffix = self._dj_requester_suffix(requester)
                    if clean_artist:
                        text = f"DJ Marvin為你帶來{clean_artist}演唱的{clean_title}，{suffix}"
                    else:
                        text = f"DJ Marvin為你帶來《{clean_title}》，{suffix}"
                    _source = "fixed_announcement"
                    logger.info("🎙️ [DJ Prefetch] 採用 fallback template")

        # 9/30 使用者定：DJ 串場不截斷，改由尾段窗口依口白長度在兩首之間留空白（見 music_cog_tail_dj._wait_dj_tail_window）

        audio_path = None
        try:
            _emotion = getattr(self, '_DJ_MODE_TO_TTS_EMOTION', {}).get(mode, "normal")
            audio_path = await self.bot.tts_engine.generate_audio(text, emotion=_emotion)
        except Exception as e:
            logger.warning(f"⚠️ [DJ Prefetch] TTS 預渲染失敗，改用即時串流: {e}")

        # 📒 一週觀察用：每段串場的主題/素材/LLM 產出/最終口白（9/30 使用者定）
        try:
            from dj_narration_log import log_dj_narration, probe_audio_seconds
            log_dj_narration({
                "song": _song_label or title,
                "requester": requester,
                "mode": mode,
                "topic": topic,
                "song_material": _song_pick,
                "lyric_material": _lyric_pick,
                "ctx": "\n".join(ctx),
                "source": _source,
                "llm_raw": _llm_raw,
                "llm_cleaned": _llm_cleaned,
                "disqualify": _disqualify,
                "text": text,
                "chars": len(text),
                "audio_s": await probe_audio_seconds(audio_path),
                "n_online": _n_online,
                "heat_mode": _heat_mode,
            })
        except Exception as e:
            logger.debug(f"[DJ Narration Log] 寫紀錄失敗: {e}")

        logger.info(f"🎙️ [DJ Prefetch] 完成: {text[:30]}… (audio={'✓' if audio_path else '✗'})")

        # 🔥 [DJ Heat] 熱聊時的短版備案：只報歌名，播出前一刻若判定熱聊就改唸這句
        # （見 cogs/music_cog_tail_dj.py::_maybe_play_dj_interjection）。
        _st, _sa = self._dj_clean_name(info)
        short_text = f"下一首，{_sa}的{_st}" if _sa else f"下一首，{_st}"
        if requester and not requester.startswith('Marvin'):
            short_text += f"，{requester} 點的"

        # 串場不提上一首 → 沒有可過期的上一首，Consistency Guard 不必比對
        return {'text': text, 'audio_path': audio_path, 'prev_title_used': None, 'short_text': short_text}

