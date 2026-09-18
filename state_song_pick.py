"""個人狀態驅動選歌（state pick）— 純函式部分。

自動點歌輪到某人（spotlight）時，若他 48h 內有「狀態」（callback_queue 可分享項 /
非 annoyed 的情緒高光，例如「感冒喉嚨痛」），讓 LLM 從**他自己的候選池**挑一首最貼合
的歌＋一句關心口吻理由；這首排本輪第一首，DJ 口白用這句理由開場（memory_match mode）。

隱私邊界（2026-09-18 使用者拍板）：當事人在場才用；健康/疲累可講但只能關心口吻；
taboos 與 valence=annoyed 一律排除。只從候選池挑、不准提名新歌（防編歌名）。
cog 端協調（env gate / 在場判斷 / 冷卻 / LLM 呼叫）見 `MusicCog._maybe_state_pick`。
"""
from __future__ import annotations

import json
import re

STATE_MAX_AGE_S = 48 * 3600
PERSON_COOLDOWN_S = 3 * 3600
MAX_STATES = 3
MAX_TITLES = 15


def collect_fresh_states(mem: dict, *, now: float, max_age_s: float = STATE_MAX_AGE_S) -> list[str]:
    """這個人近期可公開講的狀態文字（新到舊、去重、最多 MAX_STATES 條）。"""
    found: list[tuple[float, str]] = []

    cbq = mem.get("callback_queue")
    for item in cbq if isinstance(cbq, list) else []:
        if not isinstance(item, dict) or item.get("shareable") is not True:
            continue
        ts, text = item.get("ts"), str(item.get("text") or "").strip()
        if isinstance(ts, (int, float)) and now - ts <= max_age_s and text:
            found.append((float(ts), text))

    highlights = mem.get("emotional_highlights")
    for h in highlights if isinstance(highlights, list) else []:
        if not isinstance(h, dict) or h.get("valence") == "annoyed":
            continue
        ts, text = h.get("timestamp"), str(h.get("moment") or "").strip()
        if isinstance(ts, (int, float)) and now - ts <= max_age_s and text \
                and not text.startswith("__META__"):
            found.append((float(ts), text))

    taboos = [t for t in (mem.get("taboos") if isinstance(mem.get("taboos"), list) else [])
              if isinstance(t, str) and t]
    found.sort(key=lambda x: -x[0])
    out: list[str] = []
    for _, text in found:
        if text in out or any(t in text for t in taboos):
            continue
        out.append(text)
        if len(out) >= MAX_STATES:
            break
    return out


_STATE_PICK_SYS = (
    "你是懂朋友的 DJ。根據這個人最近的狀態，從「候選歌單」挑一首最適合現在放給他的歌。\n"
    "規則：\n"
    "1) 只能從候選歌單挑，用編號回答；沒有合適的就 index 回 null，不准硬湊。\n"
    "2) state 回你依據的那條狀態編號。\n"
    "3) reason：一句話、≤40 字、直接叫他的名字、用關心或打氣的口吻；只能提到狀態裡寫的事，"
    "不准自己補細節、不准編故事、不准嘲諷。\n"
    '只回 JSON：{"index": 編號或null, "state": 狀態編號, "reason": "…"}'
)


def build_state_pick_prompt(person: str, states: list[str], titles: list[str]) -> tuple[str, str]:
    """純函式 → (system, user)。"""
    state_lines = "\n".join(f"{i}. {s}" for i, s in enumerate(states))
    title_lines = "\n".join(f"{i}. {t}" for i, t in enumerate(titles[:MAX_TITLES]))
    user = (
        f"這個人：{person}\n"
        f"他最近的狀態：\n{state_lines}\n\n"
        f"候選歌單：\n{title_lines}\n\n"
        "請回 JSON。"
    )
    return _STATE_PICK_SYS, user


def parse_state_pick(resp: str, *, n_titles: int, n_states: int) -> tuple[int, int, str] | None:
    """LLM JSON → (歌編號, 狀態編號, 理由)；任何不合格 → None。"""
    m = re.search(r"\{.*\}", resp or "", re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    idx, s_idx = d.get("index"), d.get("state")
    reason = str(d.get("reason") or "").strip()
    if not isinstance(idx, int) or isinstance(idx, bool) or not 0 <= idx < n_titles:
        return None
    if not isinstance(s_idx, int) or isinstance(s_idx, bool) or not 0 <= s_idx < n_states:
        return None
    if not 6 <= len(reason) <= 60:
        return None
    return idx, s_idx, reason
