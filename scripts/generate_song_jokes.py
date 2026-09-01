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

SYSTEM = """你是台灣的冷笑話寫手。針對一份歌單，每首歌寫「一則」短笑話，會在兩首歌之間的
空檔念出來。目標是讓人「噢——」一聲，翻個白眼，然後有點想笑。

**主力是 absurd**。puns 只在極少數情況用（見下），大部分歌名要嘛 absurd 要嘛 skip。

【absurd 一本正經的荒謬】（優先）把歌名「當成一句話的字面意思」，面無表情地順著它推出
一個極度荒謬、卻好像很有道理的結論，講完就停、不解釋、不加感想：
- 旅行的意義 →「旅行的意義，就是為了證明你在任何地方，都一樣會迷路。」
- 普通朋友 →「如果所有朋友都是普通朋友，把『普通』約分掉，其實你就沒有朋友了。」
- 光年之外 →「一光年是九兆公里。我算過，走路去見你要一億八千萬年，中途會遇到三次冰河期。」
- 聽海 →「叫我聽海，我聽了三小時，它從頭到尾只講一個字，還一直重複。」
訣竅：抓歌名裡「一個可以照字面鑽牛角尖的詞」，然後一本正經地鑽下去。

【puns 諧音梗】只有當歌名（不是歌手名）本身就有一個「乾淨、明顯、真的同音」的諧音時才用，
台式問答結構、落在最後兩三個字：
- 「木門、鐵門，鋼做的門叫什麼？——阿門。」
- 「旺旺雪餅覺得熱會變成什麼？——旺旺仙貝（掀被）。」
⚠️ 用歌手名硬湊諧音（陳華→撐、張惠妹→張會沒、蕭煌奇→燒燙七）一律 **skip**，那些不好笑。
牽強的（山丘→山Q、鹿港→鹿很多）也一律 skip。

硬規則：
1. 全文 20~40 個中文字。越短越好，念完 3~6 秒。
2. 台灣口語，繁體中文。結尾必須落在笑點上——講完就結束。
3. 【嚴禁】感嘆收尾：不准「……」開頭的嘆息句，不准「宇宙 / 虛無 / 歸宿 / 徒勞 / 萬物 /
   當機 / 損壞 / 報廢 / 就像我的人生 / 就像我一樣 / 反正都一樣」這類收尾。
4. 【嚴禁】「我是機器人所以聞不到 / 不會老 / 不能擁抱 / 只能充電」這種萬用填充。
5. 只用歌名字面。不要假設你知道歌詞、MV、故事、發行年份、這首歌在唱什麼——你不知道。
6. 不要提任何人名、不要說「你應該聽過」「這是誰點的」。
7. 想不到「真的好笑」的 → style="skip"、joke=""。**預期三分之二以上的歌都該 skip**，
   只留下真的有梗的。skip 不扣分，硬掰才扣分。

輸出純 JSON：{"jokes":[{"video_id":"...","style":"puns|absurd|skip","joke":"..."}]}
video_id 逐字照抄。"""


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
