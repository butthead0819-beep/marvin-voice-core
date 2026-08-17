"""離線眼驗：讀真實 chat_summary_log + liked_items + 候選歌池 → 5步驟故事弧管線
（找敘事流 → 共同/個人回憶 → 大綱+選歌(真候選池) → 口白(定案歌單才寫) → resolve）
→ 印出故事線/預計總時間/歌單/口白，給人眼審（不碰播放、不入隊）。

用法：
  venv_simon/bin/python scripts/preview_story_arc.py --members 狗與露 showay --target-minutes 20
  加 --dry-run 跳過寫 records/dj_story_arcs.jsonl。
"""
import argparse
import asyncio
import datetime as _dt
import sys

sys.path.insert(0, ".")
from dotenv import load_dotenv
load_dotenv()

from diary_comic.parser import parse_log
from dj_story_arc import (
    BgmCursor,
    build_show_intro,
    build_story_candidate_pools,
    curate_story_interjections,
    curate_story_outline,
    estimate_interjection_duration_s,
    gather_story_brief,
    record_story_arc,
    resolve_story_arc,
)
from llm_pool import call_paid_review


async def _standalone_resolve(query: str):
    """鏡像 music_cog._resolve_yt_query 的核心，供離線眼驗 resolve 用，不依賴 cog 實例。
    同 scripts/themed_playlist_dryrun.py 的手法。"""
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
    ap = argparse.ArgumentParser()
    ap.add_argument("--members", nargs="+", required=True, help="故事對象（1個以上）")
    ap.add_argument("--target-minutes", type=float, default=20.0)
    ap.add_argument("--dry-run", action="store_true", help="跳過寫 records/dj_story_arcs.jsonl")
    args = ap.parse_args()

    entries = parse_log(open("records/chat_summary_log.txt", encoding="utf-8").read())
    if not entries:
        print("無 summary entries"); return
    last_ts = _dt.datetime.strptime(entries[-1].ts_str, "%Y-%m-%d %H:%M:%S").timestamp()
    now = last_ts + 60

    # liked_items：每位故事對象各拿近期喜歡過的東西
    from suki_memory import MemoryManager
    suki = MemoryManager()
    liked_items = []
    for m in args.members:
        for item in suki.get_recent_liked_items(m, limit=2):
            liked_items.append(f"{m}喜歡{item}")

    # conv_snippets：離線腳本沒有 runtime conv_buffer，用近幾則核心句當替代近似
    conv_snippets = [e.core for e in entries[-4:] if getattr(e, "core", None)]

    # === Step 1+2：找敘事流 + 共同/個人回憶 ===
    target_duration_s = args.target_minutes * 60.0
    brief = gather_story_brief(entries, args.members, liked_items, conv_snippets,
                               now=now, target_duration_s=target_duration_s)
    if brief is None:
        print("沒有任何一天材料夠（共同回憶 < 2 則）→ 無法生成故事弧"); return
    print(f"=== Story Brief（narrative_day={brief.narrative_day}）===")
    print("故事對象：", "、".join(brief.members))
    print(f"目標時長：{args.target_minutes:.0f} 分鐘 → node_count={brief.node_count}")
    print("共同回憶：")
    for c in brief.shared_cores:
        print("  -", c)
    for m, cs in brief.member_cores.items():
        print(f"{m}的個人回憶：")
        for c in cs:
            print("  -", c)
    print("liked_items：", "、".join(brief.liked_items) or "（無）")

    try:
        from music_memory import MusicMemory
        mm = MusicMemory()
        exclude = mm.get_recently_played_titles(7 * 24 * 3600)
        exclude_vids = mm.get_recently_played_video_ids(7 * 24 * 3600)
        songs = mm.all_songs()
    except Exception:
        exclude, exclude_vids, songs = [], [], {}
    print(f"\n排除清單 {len(exclude)} 首（近 7 天已播）")

    # === Step 3a：真候選池 ===
    pools = build_story_candidate_pools(args.members, songs, exclude, now=now)
    print("\n=== 候選歌池 ===")
    print(f"共同候選 {len(pools.get('shared', []))} 首：",
         "、".join(f"{c.anchor_artist}-{c.anchor_title}" for c in pools.get("shared", [])) or "（無）")
    for m in args.members:
        print(f"{m}的候選 {len(pools.get(m, []))} 首：",
             "、".join(f"{c.anchor_artist}-{c.anchor_title}" for c in pools.get(m, [])) or "（無）")

    # === Step 3b+4：Call 1 — 大綱 + 選歌（候選池只當口味參考，LLM 可自由推薦驚喜歌）===
    print("\n=== Call 1：呼叫付費 LLM 生成大綱+選歌中… ===")
    arc = await curate_story_outline(brief, pools, exclude, call_fn=call_paid_review)
    if arc is None or not arc.nodes:
        print("LLM 生成失敗/解析失敗 → 無故事弧"); return
    n_surprise = sum(1 for n in arc.nodes if not n.taste_match)
    print(f"\n📖 故事弧：《{arc.arc_title}》（{len(arc.nodes)} 個節點，"
         f"其中 {n_surprise} 首是候選池外的驚喜推薦）")
    missing_spotlight = arc.spotlight_coverage(brief.members)
    if missing_spotlight:
        print(f"  ⚠️ 沒有專屬高光節點的成員：{'、'.join(missing_spotlight)}（LLM 沒照 prompt 指示做到）")

    # === Step 5：Call 2 — 口白（拿到定案歌單才寫）===
    print("\n=== Call 2：呼叫付費 LLM 生成口白中… ===")
    arc = await curate_story_interjections(arc, brief, call_fn=call_paid_review)

    # === 片頭：節目開場（模板組字，零額外 LLM call）===
    intro = build_show_intro(arc, brief)
    import os as _os
    print(f"\n=== 🎬 片頭 ===\n口白：「{intro.intro_script}」")
    bgm_duration_s = None
    if _os.path.exists(intro.intro_music_path):
        print(f"片頭音樂：{intro.intro_music_path}（已存在）")
        try:
            import subprocess
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", intro.intro_music_path],
                capture_output=True, text=True, timeout=10)
            bgm_duration_s = float(out.stdout.strip())
        except Exception:
            pass
    else:
        print(f"片頭音樂：{intro.intro_music_path}（⚠️ 檔案不存在，需人工放置節目主題曲）")

    # === 口白 BGM 接續播放模擬（方案B：位置記憶重觸發，僅示意，不動真實狀態檔）===
    import tempfile
    bgm_cursor = BgmCursor(path=_os.path.join(tempfile.gettempdir(), "preview_bgm_cursor.json"))
    bgm_cursor.reset()
    print("\n口白 BGM 位置模擬（用粗估口白時長，非真實 TTS 長度，僅供理解接續邏輯）：")
    for n in arc.nodes:
        start = bgm_cursor.peek()
        est_s = estimate_interjection_duration_s(n.interjection_script)
        bgm_cursor.advance(est_s, track_duration_s=bgm_duration_s)
        print(f"  節點{n.position}：BGM 從 {start:.1f}s 接著播（這段估~{est_s:.1f}s）")

    from track_quality import is_non_song_video, extract_video_id
    print("\n=== resolve_story_arc（真 yt-dlp）… ===")
    infos = await resolve_story_arc(
        arc, resolve_fn=_standalone_resolve, exclude_vids=exclude_vids,
        is_non_song_fn=is_non_song_video, extract_vid_fn=extract_video_id)
    print(f"resolve+品質閘後可播：{len(infos)}/{len(arc.nodes)} 個節點\n")

    actual_s = sum(i.get("duration") or 0 for i in infos)
    delta_songs = (actual_s - target_duration_s) / 240.0
    print(f"預計總時間：目標 {target_duration_s:.0f}s，實際 {actual_s:.0f}s"
          f"（誤差約 {delta_songs:+.1f} 首歌，驗收標準 ±2 首以內）\n")

    for i, info in enumerate(infos, 1):
        spotlight = info.get("_story_spotlight_member")
        resonance = info.get("_story_resonance_link")
        taste = "🎯貼合口味" if info.get("_story_taste_match") else "🎲驚喜推薦"
        tag = f"✨{spotlight}的高光" if spotlight else "共同主線"
        if resonance:
            tag += f"｜🔗呼應{resonance}"
        print(f"  {i}. [{info.get('_story_emotion_tag') or '?'}｜{tag}｜{taste}] "
              f"{info['title'][:50]}（{info.get('duration', '?')}s）")
        print(f"      bpm_target={info.get('_story_bpm_target')} "
              f"volume_delta_db={info.get('_story_volume_delta_db')}")
        print(f"      song_query: {info.get('_story_song_query')}")
        script = info.get("_story_interjection_script") or "（無口白，Call 2 沒拿到）"
        print(f"      口白：「{script}」")
        print(f"      ⚠️人審重點：①這段有沒有把{spotlight or '任何人'}的事講成針對/roast/嘲諷？"
              f"②song_query 裡的細節（上面那行）是不是都能在共同/個人回憶裡找到來源，"
              f"沒有素材沒提過的捏造內容？\n")

    if len(infos) < len(arc.nodes):
        print(f"  ⚠️ {len(arc.nodes) - len(infos)} 個節點 resolve 失敗/被品質閘擋掉"
             f"（存在性驗證，跟候選池無關）")

    if not args.dry_run:
        rec = record_story_arc(arc.arc_title, infos, target_duration_s=target_duration_s,
                               ts=now, narrative_day=brief.narrative_day, intro=intro)
        print(f"\n已寫入 records/dj_story_arcs.jsonl（{len(rec['nodes'])} 個節點）")


if __name__ == "__main__":
    asyncio.run(main())
