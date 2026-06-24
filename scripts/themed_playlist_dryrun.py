"""離線眼驗：讀真實 chat_summary_log + taste_fingerprint → 主題偵測 → LLM 策展 → 印歌單。

用法：venv_simon/bin/python scripts/themed_playlist_dryrun.py
不碰播放、不入隊，只印 LLM 策展出的歌單名+歌+理由，給人眼看品質夠不夠。
"""
import asyncio
import datetime as _dt
import json
import sys

sys.path.insert(0, ".")
from dotenv import load_dotenv
load_dotenv()

from diary_comic.parser import parse_log
from themed_playlist import gather_theme_brief, curate_themed_set, resolve_themed_set
from llm_pool import call_paid_review


async def _standalone_resolve(query: str):
    """鏡像 music_cog._resolve_yt_query 的核心（yt-dlp + pick_best_music_candidate），
    給 dryrun 離線眼驗 resolve 用，不依賴 cog 實例。"""
    import asyncio
    import yt_dlp
    from music_search import pick_best_music_candidate
    opts = {"format": "bestaudio/best", "quiet": True, "no_warnings": True,
            "noplaylist": True, "ignoreerrors": True}

    def _extract():
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch5:{query}", download=False)
            entries = [e for e in (info.get("entries") or []) if e] if info else []
            chosen = pick_best_music_candidate(entries) if entries else None
            if not chosen:
                return None
            return {"title": chosen.get("title", "?"), "uploader": chosen.get("uploader", "?"),
                    "webpage_url": chosen.get("webpage_url") or chosen.get("original_url") or "",
                    "url": chosen.get("url", ""), "duration": chosen.get("duration")}
    return await asyncio.to_thread(_extract)


async def main():
    entries = parse_log(open("records/chat_summary_log.txt", encoding="utf-8").read())
    if not entries:
        print("無 summary entries"); return
    # now = 最後一段 +1 分鐘；窗 6h 抓近一場
    last_ts = _dt.datetime.strptime(entries[-1].ts_str, "%Y-%m-%d %H:%M:%S").timestamp()
    now = last_ts + 60
    members = sorted({sp for e in entries[-12:] for sp in (e.speakers or [])}) or ["狗與露", "showay"]
    taste_fp = json.load(open("records/taste_fingerprint.json", encoding="utf-8"))

    brief = gather_theme_brief(entries, taste_fp, members, now=now, window_hours=6.0)
    if brief is None:
        print("主題偵測：無共識 → fallback 單首 autopilot（近窗核心句太少）"); return
    print("=== Theme Brief（餵 LLM 的料）===")
    print("在場：", "、".join(brief.members))
    print("近窗對話主題核心句：")
    for c in brief.cores:
        print("  -", c)
    print("口味歌手：", "、".join(brief.core_artists), "| 語言：", brief.language_label)

    # 排除：近 7 天播過的歌名（沿用 music_memory）
    try:
        from music_memory import MusicMemory
        mm = MusicMemory()
        exclude = mm.get_recently_played_titles(7 * 24 * 3600)
    except Exception:
        exclude = []
    print(f"\n排除清單 {len(exclude)} 首（近 7 天已播）")

    print("\n=== 呼叫付費 LLM 策展中… ===")
    themed = await curate_themed_set(brief, exclude, call_fn=call_paid_review, set_size=6)
    if themed is None:
        print("LLM 策展失敗/解析失敗 → fallback"); return
    print(f"\n🎵 今夜歌單：《{themed.theme_title}》")
    fresh = 0
    excl_set = set(exclude)
    for i, p in enumerate(themed.picks, 1):
        is_fresh = not any(p.song in t or t in p.song for t in excl_set)
        fresh += is_fresh
        tag = "🆕" if is_fresh else "♻️"
        print(f"  {i}. {tag} {p.artist} - {p.song}")
        print(f"      「{p.reason}」")
    print(f"\n新鮮度：{fresh}/{len(themed.picks)} 首不在近 7 天排除清單（目標 ≥70%）")

    # --resolve：真 yt-dlp 解析，驗 LLM 挑的歌解析得到、版本對（慢、吃網路）
    if "--resolve" in sys.argv:
        from track_quality import is_non_song_video
        from music_memory import extract_video_id, MusicMemory
        excl_vids = MusicMemory().get_recently_played_video_ids(7 * 24 * 3600)
        print("\n=== Step 3a resolve_themed_set（真 yt-dlp）… ===")
        infos = await resolve_themed_set(
            themed, resolve_fn=_standalone_resolve, exclude_vids=excl_vids,
            is_non_song_fn=is_non_song_video, extract_vid_fn=extract_video_id)
        print(f"resolve+品質閘後可入隊：{len(infos)}/{len(themed.picks)} 首")
        for info in infos:
            print(f"  ✅ {info['title'][:50]}")
            print(f"      理由「{info['_pick_reason']}」")
        if len(infos) < 3:
            print("  ⚠️ 不足 3 首 → 實際播放層會用 T1/T2 補位（Step 3b）")


if __name__ == "__main__":
    asyncio.run(main())
