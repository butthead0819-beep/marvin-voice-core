"""D1 backfill gate for AmbientQA（design doc AmbientQA-20260830）。

離線、零成本、零 LLM。**寫任何 feature code 之前跑**，用來裁決 `GroundedQAAgent`
值不值得做。

做的事：跑過 `marvin.db` transcripts 表裡「帶喚醒詞」的 utterance，套一份 `looks_like_factual_question` 純規則，輸出：

⚠️ 2026-08-30 review：實作後 `intent_agents/grounded_qa_agent.py` 用的是**更窄**的
`parse_grounded_qa`（查動詞 + 具名事實問句尾），跟這裡的寬版不同。這支量的是
寬版的觸發率/誤命中率——要 gate 實際 shipped 規則，改跑 parse_grounded_qa（見
tests/test_grounded_qa_agent.py 對照）。寬版 verdict=FIX_REGEX；窄版實測 ~0.32/週。

  1. 觸發率        —— 命中數 / 時間跨度 → 換算「次/週」
  2. 誤命中率      —— 命中裡「其實是指令 / 點歌 / find-song」的比例（自動 proxy）
  3. 下游現況      —— 命中的 utterance 今天實際被怎麼處理（對
                     `records/agent_gaps.jsonl` 做 ts×speaker join）
  4. 人工抽樣      —— 抽 N 筆命中寫到 records/backfill_ambient_qa_sample.jsonl，
                     供人眼看「這題 grounded 回答會不會比現況好」

Gate 條件（design D1）：
  觸發率 < ~1/週    → 停，整個 plan 重議
  誤命中率 > 30%    → 先改 looks_like_factual_question regex 再繼續
  兩者都過          → 進實作（T2/T3）

限制：transcripts 沒存 `low_conf_wake`，真規則會 AND `not low_conf_wake`，
所以 live 觸發率 ≤ 這裡估的數字。

用法：
  python scripts/backfill_ambient_qa.py
  python scripts/backfill_ambient_qa.py --sample 40 --since 2026-01-01
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from command_fastpath import match_command_action  # noqa: E402
from intent_agents.constants import (  # noqa: E402
    MUSIC_PAUSE_KW, MUSIC_PLAY_KW, MUSIC_RESUME_KW, MUSIC_SKIP_KW, MUSIC_STOP_KW,
)
from intent_agents.game_knowledge_agent import GAME_KNOWLEDGE_MARKERS  # noqa: E402
from intent_agents.hallucination_guard_agent import _WAKE_RE  # noqa: E402

DB_PATH = ROOT / "marvin.db"
GAPS_PATH = ROOT / "records" / "agent_gaps.jsonl"
SAMPLE_OUT = ROOT / "records" / "backfill_ambient_qa_sample.jsonl"


# ── looks_like_factual_question（純規則，T2 會搬進 grounded_qa_agent）───────────

# design：有疑問標記（必要）
_QUESTION_MARKERS = (
    "嗎", "什麼", "甚麼", "為什麼", "為何", "多少", "幾", "哪", "誰",
    "怎麼", "怎樣", "如何", "是不是", "有沒有", "對不對", "?", "？",
)
_QUESTION_RE = re.compile("|".join(re.escape(m) for m in _QUESTION_MARKERS))

# music / 控制 / 音量 排除
_MUSIC_KW = tuple(set(MUSIC_PLAY_KW + MUSIC_SKIP_KW + MUSIC_STOP_KW
                      + MUSIC_PAUSE_KW + MUSIC_RESUME_KW))
_MUSIC_RE = re.compile("|".join(re.escape(k) for k in sorted(_MUSIC_KW, key=len, reverse=True)))
_VOLUME_RE = re.compile(r"大聲|小聲|音量|靜音|mute|volume\s*(up|down)", re.IGNORECASE)
# 裸「搜尋 <歌名>」是點歌，constants 只收「搜尋歌曲」→ 這裡補裸動詞
_MUSIC_EXTRA_RE = re.compile(r"^搜尋|播放|放一?首|點一?首")

# find-song / lyrics 排除（outside voice：「這首歌是什麼」含「什麼」會誤命中，
# 搶走 search_lyrics_grounded / find_song_agent）
_FINDSONG_RE = re.compile(
    r"這(首|是).{0,4}(什麼|甚麼)歌"
    r"|什麼歌|甚麼歌|哪(一)?首(歌)?|哪首|這首歌(叫|是)"
    r"|歌名(是|叫)?什麼|這(是|首).{0,2}哪首"
    r"|找.{0,8}(的歌|歌詞|這首歌)"
    r"|誰唱的|誰的歌|(這|那)(首|是).{0,6}(唱|歌)"
)

# 誤命中自動 proxy：命中裡還帶動作 / 音樂字眼 → 大概率是指令/點歌被規則漏接
_ACTIONISH_RE = re.compile(r"播|放|點歌|唱|切歌|跳過|下一首|上一首|暫停|繼續播|轉到|跳到")

_GAME_RE = re.compile("|".join(re.escape(m) for m in GAME_KNOWLEDGE_MARKERS))
_STRIP_EDGE = " \t，,、。.!！?？~～"


def strip_wake(text: str) -> str:
    """剝掉第一個喚醒詞，回傳實際 query。"""
    return _WAKE_RE.sub("", text, count=1).strip(_STRIP_EDGE)


_HAN_RE = re.compile(r"[一-鿿]")
_RHETORICAL_RE = re.compile(
    r"知道嗎$|好嗎$|對吧$|是喔$|欸$|齁$"
    r"|^(你|妳|他|她|它)(好嗎|說什麼|在說什麼|懂嗎)"
    r"|辦得到嗎$|可以嗎$|對不對$"
)


def looks_rhetorical_or_self(query: str) -> bool:
    """粗略切掉『閒聊反問 / 講給馬文聽 / STT 碎片』——非真·對外求證問句。

    這一層 regex 治不了根（banter vs 真問句要 LLM 判，見 gap_research wedge 的
    領域不匹配結論）。只當第二個保守數字報出來，讓灰色地帶好判。
    """
    han = _HAN_RE.findall(query)
    if len(han) < 5:                       # 太短 → 碎片 / tag
        return True
    if _WAKE_RE.search(query):             # 剝完還有喚醒詞 → run-on 講話
        return True
    if _RHETORICAL_RE.search(query):
        return True
    if query.endswith("嗎") and len(han) < 7:   # 短 tag-question，多半反問
        return True
    return False


def looks_like_factual_question(query: str, *, low_conf_wake: bool = False) -> bool:
    """design 的純規則第一版。transcripts 沒 low_conf_wake → 呼叫端傳 False。"""
    if low_conf_wake:
        return False
    if not _QUESTION_RE.search(query):
        return False
    if _MUSIC_RE.search(query) or _VOLUME_RE.search(query) or _MUSIC_EXTRA_RE.search(query):
        return False
    if match_command_action(query) is not None:
        return False
    if _FINDSONG_RE.search(query):
        return False
    return True


# ── loaders ──────────────────────────────────────────────────────────────────

def load_wake_utterances(since_ts: float, until_ts: float) -> list[dict]:
    """transcripts 表裡『前導帶喚醒詞』的 utterance，去掉相鄰重複列。"""
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT speaker, text, timestamp FROM transcripts "
        "WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp",
        (since_ts, until_ts),
    ).fetchall()
    con.close()

    out: list[dict] = []
    seen: set[tuple] = set()
    for speaker, text, ts in rows:
        if not text:
            continue
        # 喚醒詞要出現在句首附近（前 4 字），否則是句中提到「馬文」的閒聊
        m = _WAKE_RE.search(text[:4])
        if not m:
            continue
        key = (speaker, text.strip(), round(ts))
        if key in seen:
            continue
        seen.add(key)
        out.append({"speaker": speaker, "raw": text.strip(), "ts": ts})
    return out


def load_gaps() -> list[dict]:
    if not GAPS_PATH.exists():
        return []
    out = []
    for line in GAPS_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def match_downstream(hit: dict, gaps: list[dict]) -> str:
    """對 agent_gaps.jsonl 做 ts(±3s)×speaker join，回今天的處理方式。"""
    for g in gaps:
        if g.get("speaker") != hit["speaker"]:
            continue
        gts = g.get("ts")
        if gts is None or abs(gts - hit["ts"]) > 3.0:
            continue
        it = g.get("intent_type") or "UNKNOWN"
        return f"gap:{it}" if g.get("acknowledged") else f"gap-unacked:{it}"
    return "not_in_gaps"  # 走了閒聊 LLM / music agent / 靜默，backfill 分不出


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-01-01",
                    help="起始日期 YYYY-MM-DD（濾掉 1970 壞列）")
    ap.add_argument("--until", default=None, help="結束日期 YYYY-MM-DD（預設今天）")
    ap.add_argument("--sample", type=int, default=30, help="人工抽樣命中筆數")
    args = ap.parse_args()

    since_ts = datetime.strptime(args.since, "%Y-%m-%d").timestamp()
    until_ts = (datetime.strptime(args.until, "%Y-%m-%d").timestamp()
                if args.until else datetime.now().timestamp())

    utterances = load_wake_utterances(since_ts, until_ts)
    if not utterances:
        print("no wake utterances in range", file=sys.stderr)
        return 1

    gaps = load_gaps()

    span_start = min(u["ts"] for u in utterances)
    span_end = max(u["ts"] for u in utterances)
    span_weeks = max((span_end - span_start) / 86400 / 7, 1e-9)

    hits: list[dict] = []
    for u in utterances:
        q = strip_wake(u["raw"])
        if not q:
            continue
        if looks_like_factual_question(q):
            rec = dict(u, query=q)
            rec["downstream"] = match_downstream(u, gaps)
            rec["actionish"] = bool(_ACTIONISH_RE.search(q))
            rec["game_marker"] = bool(_GAME_RE.search(q))
            rec["rhetorical"] = looks_rhetorical_or_self(q)
            hits.append(rec)

    n_hits = len(hits)
    mis = sum(1 for h in hits if h["actionish"] or h["rhetorical"])
    mis_rate = mis / n_hits if n_hits else 0.0
    trigger_per_week = n_hits / span_weeks
    strict_hits = [h for h in hits if not h["actionish"] and not h["rhetorical"]]
    strict_per_week = len(strict_hits) / span_weeks

    downstream_dist: dict[str, int] = {}
    for h in hits:
        downstream_dist[h["downstream"]] = downstream_dist.get(h["downstream"], 0) + 1

    # 抽樣（均勻取，不是只取前 N）
    step = max(1, n_hits // args.sample) if n_hits else 1
    sample = hits[::step][: args.sample]
    SAMPLE_OUT.parent.mkdir(parents=True, exist_ok=True)
    with SAMPLE_OUT.open("w", encoding="utf-8") as f:
        for h in sample:
            f.write(json.dumps({
                "date": date.fromtimestamp(h["ts"]).isoformat(),
                "speaker": h["speaker"],
                "raw": h["raw"],
                "query": h["query"],
                "downstream_today": h["downstream"],
                "looks_actionish": h["actionish"],
                "looks_rhetorical": h["rhetorical"],
                "game_marker": h["game_marker"],
            }, ensure_ascii=False) + "\n")

    # ── shipped 規則量測（2026-08-30 review）：wide rule 上面量的是「值不值得做」，
    # 這裡量的是「實際 ship 的 parse_grounded_qa 會怎樣」——這才是真的 D1 gate。
    from intent_agents.grounded_qa_agent import parse_grounded_qa  # noqa: E402
    shipped_hits = []
    for u in utterances:
        topic = parse_grounded_qa(u["raw"])
        if topic is not None:
            shipped_hits.append(dict(u, topic=topic,
                                     rhetorical=looks_rhetorical_or_self(strip_wake(u["raw"])),
                                     actionish=bool(_ACTIONISH_RE.search(strip_wake(u["raw"])))))
    shipped_per_week = len(shipped_hits) / span_weeks
    shipped_mis = sum(1 for h in shipped_hits if h["rhetorical"] or h["actionish"])
    shipped_mis_rate = shipped_mis / len(shipped_hits) if shipped_hits else 0.0

    report = {
        "window": {
            "since": date.fromtimestamp(span_start).isoformat(),
            "until": date.fromtimestamp(span_end).isoformat(),
            "weeks": round(span_weeks, 1),
        },
        "wake_utterances_scanned": len(utterances),
        "_wide_rule_looks_like_factual_question": {
            "hits": n_hits,
            "trigger_rate_per_week": round(trigger_per_week, 2),
            "strict_trigger_rate_per_week": round(strict_per_week, 2),
            "mis_trigger_rate": round(mis_rate, 3),
            "note": "值不值得做的訊號；比 shipped 規則寬",
        },
        "shipped_rule_parse_grounded_qa": {
            "hits": len(shipped_hits),
            "trigger_rate_per_week": round(shipped_per_week, 2),
            "mis_trigger_rate": round(shipped_mis_rate, 3),
            "topics": [h["topic"] for h in shipped_hits],
        },
        "hits_with_game_marker": sum(1 for h in hits if h["game_marker"]),
        "downstream_today": dict(sorted(downstream_dist.items(),
                                        key=lambda kv: -kv[1])),
        "sample_written_to": str(SAMPLE_OUT.relative_to(ROOT)),
        "gate": {
            # D1 gate 現在看的是 **shipped 規則**（parse_grounded_qa），不是 wide rule。
            "shipped_trigger_rate_per_week": round(shipped_per_week, 2),
            "shipped_trigger_rate_ok": shipped_per_week >= 1.0,
            "shipped_mis_trigger_ok": shipped_mis_rate <= 0.30,
            "verdict": (
                "PROCEED" if shipped_per_week >= 1.0 and shipped_mis_rate <= 0.30
                else "FIX_REGEX" if shipped_per_week >= 1.0
                else "LOW_VOLUME"  # 2026-08-30：< 1/週。owner 明確接受低量上線，靠 ambient_qa.jsonl 人工回看
            ),
            "_wide_rule_gate": {
                "strict_trigger_rate_ok": strict_per_week >= 1.0,
                "mis_trigger_ok": mis_rate <= 0.30,
                "verdict": (
                    "PROCEED" if strict_per_week >= 1.0 and mis_rate <= 0.30
                    else "FIX_REGEX" if strict_per_week >= 1.0
                    else "STOP_REPLAN"
                ),
            },
            "note": "strict 仍含 regex 治不了的 banter；人工看 sample 才算數",
        },
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
