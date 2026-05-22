#!/usr/bin/env python3
"""
[Operation Speech DNA] — Offline player speech-pattern analyzer.

Reads /records/daily/*.log, extracts structural + emotional + per-topic features
for each player, and writes results to:
  - suki_memory.json  [compact summary — Marvin reads this]
  - records/speech_dna_{speaker}.json  [full detail]

Modes:
  python scripts/analyze_speech_dna.py                      # all speakers
  python scripts/analyze_speech_dna.py --speaker showay     # one speaker
  python scripts/analyze_speech_dna.py --force              # skip freshness check
  python scripts/analyze_speech_dna.py --export showay      # write web-LLM prompt (no API needed)
  python scripts/analyze_speech_dna.py --import-result FILE # merge web-LLM JSON back in
"""

from __future__ import annotations

import re, json, os, sys, asyncio, argparse, logging, sqlite3
from datetime import datetime
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
logger = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────
_ROOT        = Path(__file__).parent.parent
RECORDS_DIR  = _ROOT / "records" / "daily"
MEMORY_PATH  = _ROOT / "suki_memory.json"
DNA_OUT_DIR  = _ROOT / "records"

# ── LLM model (overridden at startup from .env) ───────────────────────────────
_GEMINI_MODEL = "gemini-2.5-flash"

# ── Thresholds ────────────────────────────────────────────────────────────────
MIN_UTTERANCES      = 100   # minimum to run first analysis
REANALYZE_GROWTH    = 0.30  # +30% new utterances → re-analyze
REANALYZE_MAX_DAYS  = 30
DRINKING_WINDOW_S   = 1800  # 30-min session window for drunk-mode delta

# ── Regex ─────────────────────────────────────────────────────────────────────
_LOG_RE    = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d{3} - \[([^\]]+)\] \(Debounced\) (.+)$"
)
_XML_RE    = re.compile(r"</?(?:Background|Target)[^>]*>.*?(?=<|$)", re.IGNORECASE | re.DOTALL)
_FILLER_RE = re.compile(r"(?:就是|那個|然後|對啊|阿|啊|嗯|呢|嘛|啦|喔|齁|蛤|咦|哎|唉)")
_LAUGH_RE  = re.compile(r"(?:哈{2,}|呵{2,}|嘻{2,}|XD|xd|ww{2,}|hh{2,})", re.IGNORECASE)
_NOISE_SPLIT_RE = re.compile(r"[,，、。！!？?]")

# Tokens that must never appear as openers/closers — bot broadcast phrases,
# command prefixes, STT misrecognitions, and English leak-throughs.
_STRUCTURAL_BLACKLIST: frozenset[str] = frozenset({
    # bot broadcast (picked up by all mics via echo)
    "謝謝大家", "歡迎收看", "別忘了", "各位", "大家好", "小伙伴們",
    "謝謝大家收看", "謝謝你們收看",
    # music command openers
    "馬文", "馬文播放", "麻煩播放", "播放", "麻煩",
    # STT misrecognitions of bot name
    "艾瑪文", "艾馬文", "瑪文", "馬丸", "阿丸", "阿姨文",
    # address terms — real people being called out, not speech opener patterns
    "阿姨",
    # music/content references — artist name + 的 is song discussion, not a speech habit
    "陳奕迅的", "鄧紫棋的", "周杰倫的",
    # English / URL fragments
    "YouTube", "play", "youtube", "Siri", "Apple",
    # garbled speaker name (alias cleanup)
    "狗與鹿",
    # speaker names used as address (not speech openers)
    "狗與露", "showay", "大肚", "weakgogo",
})

# Blacklist prefix check — also filter tokens that START WITH any of these
_BLACKLIST_PREFIXES: tuple[str, ...] = (
    "謝謝大家", "歡迎收看", "麻煩播放", "馬文播放", "馬文，", "馬文你",
)

# ── Initial topic keyword table ───────────────────────────────────────────────
TOPIC_KEYWORDS: dict[str, list[str]] = {
    "drinking": ["喝酒", "買酒", "啤酒", "紅酒", "威士忌", "高粱", "乾杯", "喝一杯", "醉了", "喝醉"],
    "gaming":   ["遊戲", "打電動", "Minecraft", "麥塊", "PS5", "Switch", "開局", "掉線", "上分", "電競"],
    "work":     ["工作", "老闆", "加班", "上班", "客戶", "開會", "專案", "薪水", "面試", "辭職", "同事"],
    "money":    ["錢", "花錢", "貴啊", "便宜", "預算", "虧了", "賺錢", "存款", "信用卡"],
    "tech":     ["程式", "code", "AI", "系統", "bug", "server", "API", "架構", "deploy"],
    "family":   ["爸", "媽", "老婆", "老公", "小孩", "家人", "父母", "兄弟"],
    "music":    ["唱歌", "播放", "專輯", "演唱會", "旋律", "這首歌"],
    "food":     ["吃飯", "吃東西", "料理", "好吃", "外送", "宵夜", "火鍋", "餓了"],
}

# ── Speaker alias table — old name → canonical name ──────────────────────────
# Utterances from the old name are folded into the canonical speaker's corpus.
SPEAKER_ALIASES: dict[str, str] = {
    "狗與鹿": "狗與露",
}

# ── Per-speaker custom topic overrides (persisted in speech_dna detail file) ─
# Populated at runtime via interactive prompt or --import-result.
_EXTRA_TOPICS: dict[str, dict[str, list[str]]] = {}


# ─────────────────────────────────────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────────────────────────────────────

class Utterance:
    __slots__ = ("speaker", "text", "date", "ts")

    def __init__(self, speaker: str, text: str, date: str, ts: float):
        self.speaker = speaker
        self.text    = text
        self.date    = date
        self.ts      = ts


# ─────────────────────────────────────────────────────────────────────────────
# Layer 1 — Load & clean
# ─────────────────────────────────────────────────────────────────────────────

def load_utterances(speakers: Optional[list[str]] = None) -> dict[str, list[Utterance]]:
    result: dict[str, list[Utterance]] = defaultdict(list)
    log_files = sorted(
        p for p in RECORDS_DIR.glob("*.log")
        if "cron" not in p.name and "review" not in p.name and "slice" not in p.name
    )
    for path in log_files:
        # Date from filename: "2026-05-07.log" or "stt_2026-05-07.log"
        stem = path.stem.lstrip("stt_").lstrip("_")
        date_str = re.search(r"\d{4}-\d{2}-\d{2}", stem)
        date_str = date_str.group() if date_str else path.stem

        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                m = _LOG_RE.match(line)
                if not m:
                    continue
                dt_str, speaker, raw_text = m.group(1), m.group(2), m.group(3)
                speaker = SPEAKER_ALIASES.get(speaker, speaker)
                if speakers and speaker not in speakers:
                    continue
                text = _clean(raw_text)
                if _is_noise(text):
                    continue
                try:
                    ts = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S").timestamp()
                except ValueError:
                    ts = 0.0
                result[speaker].append(Utterance(speaker, text, date_str, ts))
        except Exception as e:
            logger.warning(f"讀取 {path.name} 失敗: {e}")

    return dict(result)


def _clean(text: str) -> str:
    text = _XML_RE.sub("", text)
    return text.strip()


def _is_noise(text: str) -> bool:
    if len(text) < 3:
        return True
    # STT loop artifact: "致命, 致命, 致命, 致命"
    parts = [p.strip() for p in _NOISE_SPLIT_RE.split(text) if p.strip()]
    if len(parts) >= 4:
        top_count = Counter(parts).most_common(1)[0][1]
        if top_count / len(parts) >= 0.50:
            return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Layer 2 — Topic classification
# ─────────────────────────────────────────────────────────────────────────────

def _classify_keyword(text: str, merged: dict[str, list[str]]) -> Optional[str]:
    lower = text.lower()
    for topic, kws in merged.items():
        if any(kw.lower() in lower for kw in kws):
            return topic
    return None


def _discover_candidates(unclassified: list[Utterance], top_n: int = 8) -> list[tuple[str, int, list[str]]]:
    """Find common CJK words in unclassified sentences as potential new topic seeds."""
    SKIP = set("就是那個然後對啊阿啊嗯呢嘛啦喔齁的了嗎呢嘛吧啊哦喔你我他她我們你們他們一個什麼沒有")
    counter: Counter = Counter()
    examples: dict[str, list[str]] = defaultdict(list)
    for u in unclassified:
        tokens = set(re.findall(r'[一-鿿]{2,4}', u.text))
        for tok in tokens:
            if tok not in SKIP:
                counter[tok] += 1
                if len(examples[tok]) < 3:
                    examples[tok].append(u.text[:60])
    threshold = max(5, len(unclassified) * 0.03)
    candidates = []
    for word, count in counter.most_common(top_n * 4):
        if count >= threshold:
            candidates.append((word, count, examples[word]))
        if len(candidates) >= top_n:
            break
    return candidates


def _interactive_new_topics(candidates: list[tuple], existing: set[str] = frozenset()) -> dict[str, list[str]]:
    """Ask user to label potential new topic categories. Returns {name: [keywords]}."""
    if not candidates:
        return {}
    print("\n" + "=" * 60)
    print("🔍  [Speech DNA] 發現可能的新話題類別")
    print("=" * 60)
    for i, (word, count, exs) in enumerate(candidates):
        print(f"  [{i+1}] 「{word}」出現 {count} 次")
        for ex in exs[:2]:
            print(f"       例：{ex}")
        print()
    print("格式：類別名稱:關鍵詞1,關鍵詞2  （Enter 跳過，skip 全跳）")
    new: dict[str, list[str]] = {}
    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line or line.lower() == "skip":
            break
        if ":" in line:
            name, _, kws_str = line.partition(":")
            name = name.strip()
            kws = [k.strip() for k in kws_str.split(",") if k.strip()]
            if name and kws:
                if name in existing:
                    print(f"  ⚠️  「{name}」已存在，請用不同名稱")
                else:
                    new[name] = kws
                    print(f"  ✅ 已加入：{name} → {kws}")
        else:
            print("  ⚠️  格式錯誤，例：運動:健身,跑步")
    return new


async def _llm_call(client, prompt: str, max_tokens: int = 500, temperature: float = 0.0) -> str:
    """Unified LLM call — supports google.genai Client or groq AsyncGroq."""
    import google.genai as _genai
    if isinstance(client, _genai.Client):
        resp = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=_GEMINI_MODEL,
                contents=prompt,
            ),
            timeout=30.0,
        )
        return resp.text
    else:
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens,
                temperature=temperature,
            ),
            timeout=30.0,
        )
        return resp.choices[0].message.content


async def _llm_classify_batch(
    texts: list[str],
    topics: list[str],
    client,
    batch_size: int = 50,
) -> list[str]:
    results = ["casual"] * len(texts)
    topic_str = ", ".join(topics + ["casual"])
    n_batches = (len(texts) + batch_size - 1) // batch_size
    for batch_idx, start in enumerate(range(0, len(texts), batch_size)):
        batch = texts[start : start + batch_size]
        numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(batch))
        prompt = (
            f"話題清單：{topic_str}\n\n"
            f"請為以下每句選一個話題（只輸出數字+話題，不解釋）：\n{numbered}"
        )
        if batch_idx > 0 and batch_idx % 5 == 0:
            logger.info(f"[Topic LLM] batch {batch_idx}/{n_batches}...")
        retry_wait = 5.0
        for attempt in range(3):
            try:
                content = await _llm_call(client, prompt, max_tokens=batch_size * 6)
                for line in content.splitlines():
                    m = re.match(r"^(\d+)[.、]\s*(\S+)", line.strip())
                    if m:
                        idx = int(m.group(1)) - 1 + start
                        topic = m.group(2).lower().rstrip("。，,")
                        if 0 <= idx < len(texts):
                            results[idx] = topic if topic in topics else "casual"
                break
            except Exception as e:
                err = str(e)
                wait_m = re.search(r"try again in (\d+(?:\.\d+)?)s", err)
                retry_wait = float(wait_m.group(1)) + 1.0 if wait_m else retry_wait * 2
                if attempt < 2:
                    await asyncio.sleep(retry_wait)
                else:
                    logger.warning(f"[Topic LLM] batch {batch_idx} 失敗: {err[:120]}")
        await asyncio.sleep(0.5)
    return results


def _bucket_utterances(
    utterances: list[Utterance],
    extra_topics: dict[str, list[str]],
    llm_results: dict[str, str],
) -> dict[str, list[Utterance]]:
    merged = {**TOPIC_KEYWORDS, **extra_topics}
    buckets: dict[str, list[Utterance]] = defaultdict(list)
    for u in utterances:
        topic = llm_results.get(u.text) or _classify_keyword(u.text, merged) or "casual"
        buckets[topic].append(u)
    return dict(buckets)


# ─────────────────────────────────────────────────────────────────────────────
# Layer 3 — Feature extraction
# ─────────────────────────────────────────────────────────────────────────────

def _structural(utterances: list[Utterance]) -> dict:
    texts = [u.text for u in utterances]
    n = max(len(texts), 1)
    # Deduplicate for opener/closer counting so STT double-captures don't inflate counts.
    deduped = list(dict.fromkeys(texts))

    openers: Counter = Counter()
    closers: Counter = Counter()
    # Extract the leading 2–4 CJK chars as opener, trailing 2–4 as closer.
    # This is more reliable than full-sentence tokenization for short phrases.
    _cjk_re = re.compile(r'[一-鿿]{2,4}')
    for t in deduped:
        head_tokens = _cjk_re.findall(t[:12])   # first ~12 chars → opener
        tail_tokens = _cjk_re.findall(t[-12:])  # last ~12 chars  → closer
        if head_tokens:
            openers[head_tokens[0]] += 1
        if tail_tokens:
            closers[tail_tokens[-1]] += 1

    fillers: Counter = Counter()
    for t in texts:
        for m in _FILLER_RE.finditer(t):
            fillers[m.group()] += 1
    filler_rate = sum(fillers.values()) / n

    lengths = [len(t) for t in texts]
    short = sum(1 for l in lengths if l <= 10) / n
    mid   = sum(1 for l in lengths if 10 < l <= 30) / n
    long_ = sum(1 for l in lengths if l > 30) / n
    style = "short_burst" if short >= 0.40 else "flowing" if long_ >= 0.30 else "mixed"

    nd = max(len(deduped), 1)
    def _clean_tokens(counter: Counter, min_count: int = max(2, int(nd * 0.003))) -> list[tuple[str, float]]:
        """Remove noise: blacklisted terms, syntactic fragments, rare one-offs."""
        result = []
        for tok, c in counter.most_common(30):
            if c < min_count:
                continue
            if tok in _STRUCTURAL_BLACKLIST:
                continue
            if any(tok.startswith(p) for p in _BLACKLIST_PREFIXES):
                continue
            # skip fragments starting with particles or mid-sentence glue chars
            if tok[0] in "的了嗎呢嘛吧啊哦喔是在上後而":
                continue
            # skip pure-English tokens (likely music title or command leak)
            if tok.isascii():
                continue
            result.append((tok, round(c / n, 3)))
            if len(result) >= 10:
                break
        return result

    return {
        "openers":        _clean_tokens(openers),
        "closers":        _clean_tokens(closers),
        "top_fillers":    [(t, c) for t, c in fillers.most_common(8)],
        "filler_rate":    round(filler_rate, 2),
        "avg_chars":      round(sum(lengths) / n, 1),
        "length_dist":    {"short": round(short, 2), "mid": round(mid, 2), "long": round(long_, 2)},
        "style":          style,
    }


def _laugh(utterances: list[Utterance]) -> dict:
    seqs, texts = [], []
    for u in utterances:
        for m in _LAUGH_RE.finditer(u.text):
            seqs.append(m.group())
            if u.text not in texts:
                texts.append(u.text[:60])
    if not seqs:
        return {"rate": 0.0, "primary_char": None, "light": None, "medium": None, "heavy": None}

    primary = Counter(s[0].lower() for s in seqs).most_common(1)[0][0]
    ls = sorted(len(s) for s in seqs)
    p33, p66 = ls[len(ls)//3], ls[2*len(ls)//3]

    def _pick(condition):
        return next((s for s in seqs if condition(len(s))), None)

    return {
        "rate":         round(len(texts) / len(utterances), 3),
        "primary_char": primary,
        "light":        _pick(lambda l: l <= p33),
        "medium":       _pick(lambda l: p33 < l <= p66),
        "heavy":        _pick(lambda l: l > p66),
        "examples":     texts[:5],
    }


def _per_topic_delta(buckets: dict[str, list[Utterance]]) -> dict:
    baseline = [u for t in ("casual", "gaming") for u in buckets.get(t, [])]
    if not baseline:
        return {}
    bs = _structural(baseline)
    bl = _laugh(baseline)

    result = {}
    for topic, utts in buckets.items():
        if topic in ("casual", "gaming") or len(utts) < 5:
            continue
        s = _structural(utts)
        l = _laugh(utts)
        # top content words for this topic
        skip = set("就是那個然後對啊阿啊的了嗎你我他她我們")
        kws = [t for t, _ in Counter(
            tok for u in utts for tok in re.findall(r'[一-鿿]{2,4}', u.text)
            if tok not in skip
        ).most_common(20)][:5]

        result[topic] = {
            "count":             len(utts),
            "avg_chars":         s["avg_chars"],
            "avg_chars_delta":   round(s["avg_chars"] - bs["avg_chars"], 1),
            "filler_rate":       s["filler_rate"],
            "filler_delta":      round(s["filler_rate"] - bs["filler_rate"], 2),
            "laugh_rate":        l["rate"],
            "laugh_delta":       round(l["rate"] - bl["rate"], 3),
            "top_keywords":      kws,
        }
    return result


def _drinking_delta(utterances: list[Utterance]) -> Optional[dict]:
    drink_ts = [
        u.ts for u in utterances
        if any(kw in u.text for kw in TOPIC_KEYWORDS["drinking"]) and u.ts > 0
    ]
    if not drink_ts:
        return None

    in_sess, normal = [], []
    for u in utterances:
        if u.ts == 0:
            continue
        (in_sess if any(abs(u.ts - d) <= DRINKING_WINDOW_S for d in drink_ts) else normal).append(u)

    if len(in_sess) < 5 or len(normal) < 20:
        return None

    sd, sn = _structural(in_sess), _structural(normal)
    ld, ln = _laugh(in_sess), _laugh(normal)
    dc = round(sd["avg_chars"] - sn["avg_chars"], 1)
    fc = round(sd["filler_rate"] - sn["filler_rate"], 2)
    lc = round(ld["rate"] - ln["rate"], 3)

    parts = []
    if dc > 3:    parts.append("話變多")
    elif dc < -3: parts.append("話變少、更簡短")
    if fc > 0.2:  parts.append("填充詞增多（放鬆狀態）")
    if lc > 0.02: parts.append("笑聲明顯增多")
    elif lc < -0.01: parts.append("喝酒時反而更安靜")

    return {
        "session_count":       len(in_sess),
        "avg_chars_delta":     dc,
        "filler_delta":        fc,
        "laugh_rate_delta":    lc,
        "interpretation":      "、".join(parts) or "喝酒前後說話模式差異不大",
    }


# ─────────────────────────────────────────────────────────────────────────────
# LLM synthesis prompt  (also exported as web-LLM prompt)
# ─────────────────────────────────────────────────────────────────────────────

def build_synthesis_prompt(
    speaker: str,
    s: dict,
    l: dict,
    per_topic: dict,
    examples: list[str],
) -> str:
    filler_str = "、".join(f"{t}（{c}次）" for t, c in s.get("top_fillers", [])[:6])
    opener_str = "、".join(f"「{t}」" for t, _ in s.get("openers", [])[:5])
    closer_str = "、".join(f"「{t}」" for t, _ in s.get("closers", [])[:5])
    ld = s.get("length_dist", {})

    topic_lines = []
    for topic, d in per_topic.items():
        dc, fc, lc = d["avg_chars_delta"], d["filler_delta"], d["laugh_delta"]
        topic_lines.append(
            f"  - {topic}（{d['count']}句）："
            f"句長{'↑' if dc > 2 else '↓' if dc < -2 else '—'}{abs(dc):.0f}字，"
            f"填充詞{'↑' if fc > 0.1 else '↓' if fc < -0.1 else '—'}，"
            f"笑聲{'↑' if lc > 0.01 else '↓' if lc < -0.01 else '—'}"
        )

    return f"""你是語言學分析師。根據以下統計資料，為玩家「{speaker}」生成說話風格描述。

## 結構特徵
- 平均句長：{s.get('avg_chars', 0):.1f} 字
- 風格：{s.get('style')}（短句{ld.get('short',0):.0%} / 中句{ld.get('mid',0):.0%} / 長句{ld.get('long',0):.0%}）
- 常見句首：{opener_str}
- 常見句尾：{closer_str}
- 填充詞：{filler_str}（平均 {s.get('filler_rate', 0):.1f} 個/句）

## 笑聲
- 出現率：{l.get('rate', 0):.1%}
- 輕笑：{l.get('light') or 'N/A'} | 中笑：{l.get('medium') or 'N/A'} | 重笑：{l.get('heavy') or 'N/A'}

## 各話題差異（對照 casual/gaming 基準）
{chr(10).join(topic_lines) or '  （話題資料不足）'}

## 代表例句（真實 STT，共 {len(examples)} 句）
{chr(10).join('  ' + e for e in examples)}

---
請輸出 JSON（繁體中文，欄位名稱保持英文）：

{{
  "style_summary": "（150-200字，描述說話風格：句型、填充詞習慣、笑聲、各話題的變化。像描述真實朋友，不用學術語氣。）",
  "quirks": ["最獨特的特徵1", "特徵2", "特徵3"],
  "low_mood_signal": "（從資料推斷情緒低落時的表現：沉默/單字/岔題/其他）"
}}"""


# ─────────────────────────────────────────────────────────────────────────────
# Web-LLM prompt exporter
# ─────────────────────────────────────────────────────────────────────────────

_EXPORT_PAGE_SIZE = 200   # sentences per classification page


def export_web_prompt(
    speaker: str,
    utterances: list[Utterance],
    s: dict,
    l: dict,
    per_topic: dict,
    unclassified: list[Utterance],
    candidates: list[tuple],
    extra_topics: dict[str, list[str]],
) -> list[Path]:
    """Write paginated web-LLM prompt files.

    Page 1 (p1): classification batch 1 + style synthesis + new-topic suggestions.
    Page 2+ (p2, p3, …): classification batches only (no synthesis repeat).
    Returns list of paths written.
    """
    examples = list(dict.fromkeys(
        u.text for u in utterances if len(u.text) > 5
    ))[:15]

    all_topics = list(TOPIC_KEYWORDS.keys()) + list(extra_topics.keys())
    date_str = datetime.now().strftime("%Y%m%d")
    base = DNA_OUT_DIR / f"speech_dna_prompt_{speaker}_{date_str}"

    unc_texts = [u.text for u in unclassified]
    pages = [unc_texts[i:i + _EXPORT_PAGE_SIZE] for i in range(0, max(len(unc_texts), 1), _EXPORT_PAGE_SIZE)]
    if not pages:
        pages = [[]]

    written: list[Path] = []
    for page_idx, page in enumerate(pages):
        p_num = page_idx + 1
        total_pages = len(pages)
        suffix = f"_p{p_num}" if total_pages > 1 else ""
        path = Path(f"{base}{suffix}.txt")

        # --- classification block ---
        if page:
            numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(page))
            part_a = (
                f"## Part A — 話題分類（第 {p_num}/{total_pages} 頁，"
                f"本頁 {len(page)} 句，全部未分類共 {len(unc_texts)} 句）\n\n"
                f"話題選項：{', '.join(all_topics)}, casual\n\n"
                f"{numbered}\n\n"
                f"輸出格式（每行一條）：\n1. gaming\n2. casual\n...\n\n---\n"
            )
        else:
            part_a = ""

        # --- synthesis + new-topic suggestions only on first page ---
        part_b = part_c = ""
        if p_num == 1:
            part_b = f"## Part B — 說話風格摘要\n\n{build_synthesis_prompt(speaker, s, l, per_topic, examples)}"
            if candidates:
                lines = "\n".join(f"  - 「{w}」出現 {c} 次，例：{exs[0]}" for w, c, exs in candidates[:6])
                part_c = f"""

---
## Part C — 建議新話題類別

以下關鍵詞頻繁出現於未分類句子，若代表特定話題請命名並提供關鍵詞：

{lines}

輸出格式（JSON）：
{{
  "new_topics": {{
    "類別名稱": ["關鍵詞1", "關鍵詞2"]
  }}
}}"""

        header = (
            f"# Speech DNA Analysis — 「{speaker}」"
            + (f"（第 {p_num}/{total_pages} 頁）" if total_pages > 1 else "")
            + f"\n資料規模：{len(utterances)} 句 | 分析日期：{datetime.now().strftime('%Y-%m-%d')}\n\n"
        )
        full = header + part_a + part_b + part_c + "\n---\n注意：所有 JSON 輸出請放在 ```json ... ``` 代碼塊內，方便程式解析。\n"

        path.write_text(full, encoding="utf-8")
        written.append(path)

    for p in written:
        logger.info(f"📄  Web LLM prompt 已儲存 → {p}")
    return written


# ─────────────────────────────────────────────────────────────────────────────
# Output
# ─────────────────────────────────────────────────────────────────────────────

def _atomic_write(path: Path, data: dict):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def save_to_memory(speaker: str, summary: dict):
    # 1. 寫入 SQLite（primary storage，防止 bot 的 _export_json 覆蓋）
    _db = _ROOT / "marvin.db"
    try:
        with sqlite3.connect(str(_db)) as conn:
            row = conn.execute("SELECT data FROM players WHERE username=?", (speaker,)).fetchone()
            if row:
                p = json.loads(row[0])
                p["speech_dna"] = summary
                conn.execute("UPDATE players SET data=? WHERE username=?",
                             (json.dumps(p, ensure_ascii=False), speaker))
            else:
                logger.warning(f"⚠️  marvin.db 找不到 {speaker}，只更新 JSON")
        logger.info(f"✅  marvin.db 已更新：{speaker}.speech_dna")
    except Exception as e:
        logger.error(f"❌  marvin.db 寫入失敗: {e}")

    # 2. 同步更新 JSON（供不跑 bot 時的腳本直接讀取）
    try:
        mem = json.loads(MEMORY_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"無法讀取 suki_memory.json: {e}")
        return
    mem.setdefault("players", {}).setdefault(speaker, {})["speech_dna"] = summary
    _atomic_write(MEMORY_PATH, mem)
    logger.info(f"✅  suki_memory.json 已更新：{speaker}.speech_dna")


def save_detail(speaker: str, full: dict):
    path = DNA_OUT_DIR / f"speech_dna_{speaker}.json"
    _atomic_write(path, full)
    logger.info(f"✅  詳細分析 → {path}")


# ─────────────────────────────────────────────────────────────────────────────
# Update cycle
# ─────────────────────────────────────────────────────────────────────────────

def check_freshness(speaker: str, total: int) -> tuple[bool, str]:
    try:
        mem = json.loads(MEMORY_PATH.read_text(encoding="utf-8"))
        dna = mem.get("players", {}).get(speaker, {}).get("speech_dna") or {}
    except Exception:
        return True, "無法讀取記憶體"

    if not dna.get("sample_count"):
        ok = total >= MIN_UTTERANCES
        return ok, f"初次分析需 ≥{MIN_UTTERANCES} 句（目前 {total} 句）"

    prev = dna["sample_count"]
    growth = (total - prev) / prev if prev else 1.0
    if growth >= REANALYZE_GROWTH:
        return True, f"新增 {total-prev} 句（+{growth:.0%}）"

    ts = dna.get("analyzed_at", "")
    if ts:
        try:
            days = (datetime.now() - datetime.fromisoformat(ts)).days
            if days >= REANALYZE_MAX_DAYS:
                return True, f"已 {days} 天未更新"
        except ValueError:
            pass

    return False, f"新增 {total-prev} 句（{growth:.0%}），未達閾值"


# ─────────────────────────────────────────────────────────────────────────────
# Import web-LLM result
# ─────────────────────────────────────────────────────────────────────────────

def import_result(result_file: str):
    """Merge web-LLM JSON output back into the latest speech_dna detail files."""
    path = Path(result_file)
    raw = path.read_text(encoding="utf-8")
    # Extract JSON from code block if present
    m = re.search(r"```json\s*(.*?)```", raw, re.DOTALL)
    data = json.loads(m.group(1) if m else raw)

    speaker = data.get("speaker")
    if not speaker:
        logger.error("JSON 中缺少 'speaker' 欄位")
        return

    # Merge style fields
    detail_path = DNA_OUT_DIR / f"speech_dna_{speaker}.json"
    if detail_path.exists():
        detail = json.loads(detail_path.read_text(encoding="utf-8"))
    else:
        detail = {}

    for key in ("style_summary", "quirks", "low_mood_signal"):
        if key in data:
            detail[key] = data[key]

    if "new_topics" in data:
        detail.setdefault("topic_keywords", {}).update(data["new_topics"])
        logger.info(f"新話題類別已合入：{list(data['new_topics'].keys())}")

    if "topic_classifications" in data:
        # list of {text, topic}
        tc = {item["text"]: item["topic"] for item in data["topic_classifications"]}
        detail.setdefault("llm_topic_results", {}).update(tc)

    _atomic_write(detail_path, detail)

    # Update compact summary in memory
    summary_keys = ("style_summary", "quirks", "low_mood_signal", "analyzed_at", "sample_count",
                    "style", "avg_chars", "top_fillers", "laugh_light", "laugh_medium", "laugh_heavy", "laugh_rate")
    summary = {k: detail[k] for k in summary_keys if k in detail}
    save_to_memory(speaker, summary)
    logger.info(f"✅  {speaker} web-LLM 結果已合入")


# ─────────────────────────────────────────────────────────────────────────────
# Main analysis pipeline
# ─────────────────────────────────────────────────────────────────────────────

async def analyze_speaker(
    speaker: str,
    utterances: list[Utterance],
    client,
    force: bool,
    export_only: bool,
):
    logger.info(f"\n{'='*55}\n🧬  {speaker}  ({len(utterances)} 句)\n{'='*55}")

    if not force and not export_only:
        ok, reason = check_freshness(speaker, len(utterances))
        if not ok:
            logger.info(f"⏭️   跳過：{reason}")
            return
        logger.info(f"🔄  更新原因：{reason}")

    # Load speaker-specific extra topics from existing detail file
    extra_topics: dict[str, list[str]] = {}
    detail_path = DNA_OUT_DIR / f"speech_dna_{speaker}.json"
    if detail_path.exists():
        try:
            old = json.loads(detail_path.read_text(encoding="utf-8"))
            saved = old.get("topic_keywords", {})
            for t, kws in saved.items():
                if t not in TOPIC_KEYWORDS:
                    extra_topics[t] = kws
        except Exception:
            pass

    # Keyword pass
    merged_kw = {**TOPIC_KEYWORDS, **extra_topics}
    unclassified = [u for u in utterances if not _classify_keyword(u.text, merged_kw)]
    kw_hit = 1 - len(unclassified) / len(utterances)
    logger.info(f"關鍵詞覆蓋：{kw_hit:.0%}（未分類 {len(unclassified)} 句）")

    # Discover potential new categories
    candidates = _discover_candidates(unclassified)

    # Interactive new-topic prompt (only in TTY, skip in export-only)
    new_topics: dict[str, list[str]] = {}
    if candidates and not export_only and sys.stdin.isatty():
        new_topics = _interactive_new_topics(candidates, set(merged_kw))
        extra_topics.update(new_topics)
        merged_kw.update(new_topics)
        # Re-filter unclassified
        unclassified = [u for u in utterances if not _classify_keyword(u.text, merged_kw)]

    # LLM topic classification for remaining unclassified
    # Re-use cached results from a previous run if present (avoids re-billing on retries)
    llm_topic_results: dict[str, str] = {}
    if detail_path.exists():
        try:
            cached = json.loads(detail_path.read_text(encoding="utf-8")).get("llm_topic_results", {})
            if cached:
                llm_topic_results = cached
        except Exception:
            pass

    still_unclassified = [u for u in unclassified if u.text not in llm_topic_results]
    if still_unclassified and client and not export_only:
        logger.info(f"🤖  LLM 批次分類 {len(still_unclassified)} 句（跳過已有 {len(llm_topic_results)} 筆快取）...")
        topics = list(merged_kw.keys())
        classified = await _llm_classify_batch(
            [u.text for u in still_unclassified], topics, client
        )
        llm_topic_results.update({u.text: t for u, t in zip(still_unclassified, classified)})

    # Final bucketing
    buckets = _bucket_utterances(utterances, extra_topics, llm_topic_results)
    dist = {t: len(v) for t, v in buckets.items()}
    logger.info(f"話題分布：{dist}")

    # Feature extraction
    s = _structural(utterances)
    l = _laugh(utterances)
    pt = _per_topic_delta(buckets)
    dd = _drinking_delta(utterances)

    # Export mode — write prompt file and stop
    if export_only:
        export_web_prompt(speaker, utterances, s, l, pt, unclassified, candidates, extra_topics)
        return

    # LLM synthesis
    llm_result: Optional[dict] = None
    if client:
        examples = list(dict.fromkeys(
            u.text for topic in ("casual", "drinking", "gaming", "work")
            for u in buckets.get(topic, [])[:4] if len(u.text) > 5
        ))[:15]
        prompt = build_synthesis_prompt(speaker, s, l, pt, examples)
        try:
            raw = await _llm_call(client, prompt, max_tokens=600, temperature=0.3)
            m = re.search(r"```json\s*(.*?)```", raw, re.DOTALL)
            llm_result = json.loads(m.group(1) if m else raw)
        except Exception as e:
            logger.warning(f"⚠️   LLM 合成失敗: {e}")

    if not llm_result:
        logger.warning("style_summary 留空 — 請執行 --export 後貼入 web LLM，再用 --import-result 合入")

    # Build output objects
    # Flatten structural features into imitation-engine-compatible format
    _style_map = {"short_burst": "short", "flowing": "long", "mixed": "medium"}
    _top_words = lambda pairs: [w for w, _ in pairs[:6]]

    # per_topic stress hints: find topics where sentence length increases most vs baseline
    _stress_topics = sorted(
        [(t, v.get("avg_chars_delta", 0)) for t, v in pt.items()],
        key=lambda x: x[1], reverse=True
    )[:3]
    _stress_hint = "、".join(
        f"{t}（句長+{int(v)}字）" for t, v in _stress_topics if v > 2
    ) or "無顯著話題壓力變化"

    summary = {
        "analyzed_at":        datetime.now().isoformat(),
        "sample_count":       len(utterances),
        # imitation engine structural keys
        "openers":            _top_words(s.get("openers", [])),
        "closers":            _top_words(s.get("closers", [])),
        "fillers":            _top_words(s.get("top_fillers", [])),
        "sentence_length":    _style_map.get(s["style"], "medium"),
        "laugh_light":        l["light"],
        "laugh_medium":       l["medium"],
        "laugh_heavy":        l["heavy"],
        "laugh_rate":         l["rate"],
        # LLM-generated fields
        "style_summary":      llm_result.get("style_summary") if llm_result else None,
        "quirks":             llm_result.get("quirks", []) if llm_result else [],
        "low_mood_signal":    llm_result.get("low_mood_signal") if llm_result else None,
        "emotional_style":    llm_result.get("low_mood_signal", "")[:60] if llm_result else "",
        # stress / topic deltas — not in imitation prompt but useful for context
        "stress_topics":      _stress_hint,
        "avg_chars":          s["avg_chars"],
        "filler_rate":        s["filler_rate"],
        "style":              s["style"],
        "top_fillers":        s["top_fillers"][:5],
    }
    full = {
        **summary,
        "structural":       s,
        "laugh_signature":  l,
        "per_topic":        pt,
        "drinking_delta":   dd,
        "topic_dist":       dist,
        "topic_keywords":   {**TOPIC_KEYWORDS, **extra_topics},
        "llm_topic_results": llm_topic_results,
    }

    save_to_memory(speaker, summary)
    save_detail(speaker, full)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--speaker",       help="指定分析玩家（預設：全部）")
    parser.add_argument("--force",         action="store_true", help="跳過更新週期檢查")
    parser.add_argument("--export",        metavar="SPEAKER", help="輸出 web LLM prompt，不呼叫 API")
    parser.add_argument("--import-result", metavar="FILE",    help="合入 web LLM 回傳的 JSON")
    args = parser.parse_args()

    if args.import_result:
        import_result(args.import_result)
        return

    # Load LLM client — Groq only（一次性分析工具，免費 tier 足夠）
    client = None
    try:
        import groq as _groq
        key = os.environ.get("GROQ_API_KEY") or _load_env_key("GROQ_API_KEY")
        if key:
            client = _groq.AsyncGroq(api_key=key)
            logger.info("✅  Groq API 就緒")
        else:
            logger.warning("⚠️   GROQ_API_KEY 未設定，跳過 LLM 步驟")
    except ImportError:
        logger.warning("⚠️   groq 套件未安裝，跳過 LLM 步驟")

    all_utts = load_utterances()

    if args.export:
        utts = all_utts.get(args.export, [])
        if not utts:
            logger.error(f"找不到 {args.export} 的語料")
            return
        await analyze_speaker(args.export, utts, None, force=True, export_only=True)
        return

    targets = [args.speaker] if args.speaker else list(all_utts.keys())
    for speaker in targets:
        utts = all_utts.get(speaker, [])
        if len(utts) < MIN_UTTERANCES:
            logger.info(f"⏭️   {speaker}：只有 {len(utts)} 句，未達門檻（{MIN_UTTERANCES}）")
            continue
        await analyze_speaker(speaker, utts, client, force=args.force, export_only=False)


def _load_env_key(key_name: str) -> Optional[str]:
    env_path = _ROOT / ".env"
    if not env_path.exists():
        return None
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{key_name}="):
            return line.split("=", 1)[1].strip()
    return None


if __name__ == "__main__":
    asyncio.run(main())
