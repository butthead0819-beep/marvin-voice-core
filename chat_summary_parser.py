"""解析 records/chat_summary_log.txt → 結構化日記條目。

DJ 分支共用：dj_life_context / dj_story_arc / dj_topic_selector 都吃這裡的
DiaryEntry（串場故事的「最近生活內容」素材來源）。原本跟漫畫排版函式一起放在
diary_comic/parser.py，砍掉漫畫日記功能時抽出來獨立，避免 DJ 功能被連坐。

每篇格式（容忍變體：核心可能無冒號、可能有前綴「- 」、可能夾 *嘆氣*）：
  [YYYY-MM-DD HH:MM:SS] --- 5分鐘對話總結 ---
  【核心】：...
  【摘要】：
  - 說話者：...
  【碎念】：...
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_HEADER = re.compile(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s*---.*?---")
_MARK_STRIP = "：: -　\t"  # 標記後要剝掉的標點/空白（含全形冒號與全形空白）

# bot 自己：TTS 回放被 STT 轉錄成這些名字，不算卡司
_BOT_NAMES = {"marvin", "馬文", "馬汶"}

# 已知卡司名冊：6 月格式的【摘要】是整段不是 bullet，靠掃名冊抽說話者。
DEFAULT_ROSTER = ("狗與露", "狗與鹿", "showay", "陳進文", "大肚", "weakgogo")


@dataclass
class DiaryEntry:
    ts_str: str
    core: str
    speakers: list[str] = field(default_factory=list)
    aside: str = ""
    raw: str = ""
    salience: str = "中"   # 話題顯著度 高|中|低（summarizer 標；舊 entry 無→中）
    meme_id: str = ""      # 語義 meme tag（搬家/宿醉 等）；summarizer 標；無→""


def _extract_after_marker(body: str, key: str) -> str:
    """取 key（'核心'/'摘要'/'碎念'）後的內文。同時吃舊格式【核心】與 6 月格式 核心：。"""
    bracket = f"【{key}】"
    for line in body.splitlines():
        if bracket in line:  # 舊：【核心】： / 【核心】文字
            return line.split(bracket, 1)[1].lstrip(_MARK_STRIP).strip()
        stripped = line.strip().lstrip("-").strip()
        if stripped.startswith(key):  # 6 月：核心：文字
            rest = stripped[len(key):]
            if not rest or rest[0] in "：:":
                return rest.lstrip(_MARK_STRIP).strip()
    return ""


def _extract_speakers(body: str, roster=DEFAULT_ROSTER) -> list[str]:
    """用已知名冊掃整段抽說話者，依首次出現位置排序、去重、濾 bot。

    兩種格式（舊 bullet / 6 月整段）都通吃，因為名字一定出現在文字裡。
    名冊外的新人不偵測（之後對應 character bible 的 fallback 動物）。
    """
    found = [(name, body.find(name)) for name in roster if name in body]
    speakers: list[str] = []
    for name, _pos in sorted(found, key=lambda x: x[1]):
        if name.lower() in _BOT_NAMES or name in speakers:
            continue
        speakers.append(name)
    return speakers


def parse_log(text: str) -> list[DiaryEntry]:
    """切出所有非 SKIP 的日記條目，依出現順序回傳。"""
    entries: list[DiaryEntry] = []
    matches = list(_HEADER.finditer(text))
    for i, m in enumerate(matches):
        ts_str = m.group(1)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if "[SKIPPED" in body or not body:
            continue
        core = _extract_after_marker(body, "核心")
        if not core:
            continue  # 沒核心 = 視為無效，跳過
        raw_meme = _extract_after_marker(body, "meme")
        # LLM 無法歸納時填「-」，視同空字串（走 text hash 冷卻）
        meme_id = "" if raw_meme in ("-", "—", "無") else raw_meme
        entries.append(DiaryEntry(
            ts_str=ts_str,
            core=core,
            speakers=_extract_speakers(body),
            aside=_extract_after_marker(body, "碎念"),  # 6 月無碎念 → ""
            salience=(_extract_after_marker(body, "顯著度") or "中"),  # 舊 entry 無→中
            meme_id=meme_id,  # 新格式；無/"-" → ""（fail-open）
            raw=body,
        ))

    return entries
