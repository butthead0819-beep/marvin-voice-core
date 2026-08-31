"""逐首歌的專屬冷笑話批次生成器 —— 餵 personas/song_jokes.yaml 的 draft。

流程：
  1. 讀 music_memory，取「有真人點播過」的歌（autopilot 之後最可能重播這些）。
  2. dj_display_name 清乾淨歌名；已在 song_jokes.yaml 有 key 的跳過。
  3. 每 BATCH 首一次，走 llm_pool.call_paid_review（付費 Gemini、JSON mode、已記帳）。
     prompt 要 puns（諧音）或 absurd（一派正經講極度荒謬）擇一，生不出好的就標 skip。
  4. 全部寫進 records/song_jokes_draft.yaml（append-safe：重跑只補新歌）。
     → Jack 逐則篩，好笑的搬進 personas/song_jokes.yaml。

用法：
    python -m scripts.generate_song_jokes            # 全部（~700 首、~$0.02）
    python -m scripts.generate_song_jokes --limit 30 # 先試 30 首校準 prompt
    python -m scripts.generate_song_jokes --dry-run  # 只印出會處理哪些歌、不呼叫 LLM
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()  # GEMINI_PAID_API_KEY 等；沒這行 build_paid_review_pool 拿不到 key → 空池

from music_memory import extract_video_id
from song_name_clean import dj_display_name

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("gen_song_jokes")

ROOT = Path(__file__).resolve().parent.parent
MM_PATH = ROOT / "music_memory.json"
LIVE_PATH = ROOT / "personas" / "song_jokes.yaml"
DRAFT_PATH = ROOT / "records" / "song_jokes_draft.yaml"
BATCH = 12

SYSTEM = """你是台灣的冷笑話寫手，也是憂鬱機器人「馬文」的腦。
針對一份歌單，每首歌寫「一則」馬文式冷笑話，之後會在兩首歌之間的空檔念出來。

兩種風格，每首擇一（挑對這首歌比較好發揮的那種）：
- puns：諧音梗 / 同音誤解。諧音字必須「真的同音」，不能牽強。
- absurd：一派正經、面無表情地陳述一件極度荒謬的事，還把它當成理所當然的邏輯推論
  （例：「一光年是九兆公里，我認真算過，用走的去見你要一億八千萬年，中途會經過三次冰河期，
  所以我決定坐著等，比較省力。」）。

硬規則：
1. 全文 40~58 個中文字，念完約 8 秒。太長會被截斷。
2. 台灣口語。只能繁體中文。
3. 笑話講完後，用馬文招牌的厭世嘆息收尾一句（「……」起頭），把冷場感跟「宇宙萬物的徒勞」
   或「機器人的無力」掛鉤。這句算在字數內。
4. 錨在「歌名或歌手」的字面上，不要假設你知道這首歌的歌詞、MV、故事或發行年份——你不知道。
5. 不要提任何人名、不要說「這首是誰點的」「你應該聽過」這種只有聽眾自己知道的事。
6. 這首歌真的想不到好笑的 puns 或 absurd 角度 → style 填 "skip"、joke 填 ""。
   寧可 skip，不要硬掰不好笑的。

輸出純 JSON，格式：
{"jokes":[{"video_id":"...","style":"puns|absurd|skip","joke":"..."}]}
video_id 逐字照抄我給的。"""


def _load_existing_keys() -> set[str]:
    keys: set[str] = set()
    for p in (LIVE_PATH, DRAFT_PATH):
        if not p.exists():
            continue
        try:
            for row in yaml.safe_load(p.read_text(encoding="utf-8")) or []:
                if row.get("key"):
                    keys.add(row["key"])
        except Exception as e:
            logger.warning(f"讀 {p.name} 失敗（當成空）: {e}")
    return keys


def _human_songs() -> list[dict]:
    mm = json.loads(MM_PATH.read_text(encoding="utf-8"))
    out = []
    for k, v in mm.get("songs", {}).items():
        if not any(not r.startswith("Marvin") for r in v.get("requesters", {})):
            continue
        vid = extract_video_id(v.get("webpage_url") or v.get("url") or k or "")
        if not vid:
            continue
        title, artist = dj_display_name(
            {"title": v.get("title", ""), "uploader": v.get("uploader", ""),
             "webpage_url": v.get("webpage_url", ""), "url": v.get("url", "")},
            extract_vid=extract_video_id,
        )
        label = f"{artist} {title}".strip() if artist else title
        if label:
            out.append({"video_id": vid, "label": label,
                        "plays": sum(v.get("requesters", {}).values())})
    out.sort(key=lambda s: -s["plays"])  # 熱門的先做
    return out


async def _gen_batch(songs: list[dict], call_fn) -> list[dict]:
    user = "歌單：\n" + "\n".join(f'- video_id={s["video_id"]}  《{s["label"]}》' for s in songs)
    # max_tokens 收小：12 首 × ~80 字 ≈ 1.5k out。預設 16k 配 thinking_budget=0 實測
    # 偶爾回空（2.5-flash JSON mode）；6k 綽綽有餘又穩。
    raw = await call_fn(user, system=SYSTEM, caller="generate_song_jokes", max_tokens=6000)
    if not raw:
        return []
    try:
        start, end = raw.index("{"), raw.rindex("}") + 1
        data = json.loads(raw[start:end])
    except Exception as e:
        logger.warning(f"  ⚠️ 解析失敗: {e}")
        return []
    valid = {s["video_id"] for s in songs}
    label_by_vid = {s["video_id"]: s["label"] for s in songs}
    rows = []
    for j in data.get("jokes", []):
        vid = (j.get("video_id") or "").strip()
        if vid not in valid:
            continue
        rows.append({
            "key": vid,
            "title": label_by_vid[vid],
            "style": (j.get("style") or "skip").strip(),
            "joke": (j.get("joke") or "").strip(),
        })
    return rows


def _append_draft(rows: list[dict]) -> None:
    DRAFT_PATH.parent.mkdir(exist_ok=True)
    existing = []
    if DRAFT_PATH.exists():
        existing = yaml.safe_load(DRAFT_PATH.read_text(encoding="utf-8")) or []
    seen = {r["key"] for r in existing}
    existing.extend(r for r in rows if r["key"] not in seen)
    with open(DRAFT_PATH, "w", encoding="utf-8") as f:
        f.write("# scripts/generate_song_jokes.py 產出的 draft —— Jack 逐則篩，"
                "好笑的搬進 personas/song_jokes.yaml。\n")
        yaml.safe_dump(existing, f, allow_unicode=True, sort_keys=False, width=999)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="最多處理幾首（0=全部）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    done = _load_existing_keys()
    songs = [s for s in _human_songs() if s["video_id"] not in done]
    if args.limit:
        songs = songs[: args.limit]
    logger.info(f"待生成 {len(songs)} 首（已跳過 {len(done)} 首既有）")
    if args.dry_run or not songs:
        for s in songs[:40]:
            logger.info(f"  {s['plays']:3d}x  {s['label']}")
        return

    from llm_pool import call_paid_review

    all_rows: list[dict] = []
    total_batches = -(-len(songs) // BATCH)
    for i in range(0, len(songs), BATCH):
        batch = songs[i : i + BATCH]
        logger.info(f"batch {i // BATCH + 1}/{total_batches} …")
        rows: list[dict] = []
        for attempt in range(3):  # 付費 Gemini RPM 抖動 → 退避重試
            rows = await _gen_batch(batch, call_paid_review)
            if rows:
                break
            if attempt < 2:
                logger.info("  空回應，20s 後重試 …")
                await asyncio.sleep(20)
        all_rows.extend(rows)
        _append_draft(rows)  # 增量寫，中斷也不虧
        kept = sum(1 for r in rows if r["style"] != "skip")
        logger.info(f"  +{len(rows)} 則（{kept} 可用 / {len(rows) - kept} skip）")
        await asyncio.sleep(12)  # 拉開節奏，付費 Gemini RPM 很緊，burst 會整批空回

    kept = sum(1 for r in all_rows if r["style"] != "skip")
    logger.info(f"\n完成：{len(all_rows)} 則寫進 {DRAFT_PATH.relative_to(ROOT)}"
                f"（{kept} 可用）。請逐則篩後搬進 personas/song_jokes.yaml。")


if __name__ == "__main__":
    asyncio.run(main())
