import json
import re
import logging

logger = logging.getLogger(__name__)

# Wake-detection constants and helpers have moved to wake_detector.py.
# Re-exported here so existing callers (discord_voice_engine, stt_cleaner, …) keep working.
from wake_detector import (
    WAKE_WORDS_LIST,
    FAST_ONLY_WAKE_WORDS,
    WAKE_PATTERN,
    pre_filter_speech,
    check_cleaned_text_for_wake,
    _load_wake_override,
)

__all__ = [
    "safe_json_loads",
    "is_whisper_hallucination",
    "parse_lyrics_sections",
    "pick_lyrics_snippet",
    # re-exported from wake_detector for backward compat
    "WAKE_WORDS_LIST",
    "FAST_ONLY_WAKE_WORDS",
    "WAKE_PATTERN",
    "pre_filter_speech",
    "check_cleaned_text_for_wake",
    "_load_wake_override",
]

def safe_json_loads(raw_str: str, default_value: dict = None) -> dict:
    """🛡️ [Robust JSON] 嘗試多種策略解析 LLM 產出的 JSON (Operation Fault Tolerance)"""
    if default_value is None:
        default_value = {}
    if not raw_str:
        return default_value
    
    # 1. 基本清理與 Python 字面量修正 (Operation Fault Tolerance)
    clean_str = raw_str.replace('```json', '').replace('```', '').strip()
    
    # 修正常見的 Python 風格 JSON 錯誤 (如 True/False/None)
    # 使用的正則確保不在引號內才替換
    clean_str = re.sub(r'(\b)True(\b)', r'\1true\2', clean_str)
    clean_str = re.sub(r'(\b)False(\b)', r'\1false\2', clean_str)
    clean_str = re.sub(r'(\b)None(\b)', r'\1null\2', clean_str)
    
    # 2. 嘗試直接解析
    try:
        return json.loads(clean_str)
    except json.JSONDecodeError:
        pass

    # 3. 🛡️ [Repair Strategy A] 補齊被截斷的 JSON (包含未閉合引號與括號)
    repaired = clean_str
    try:
        # 統計引號數量，若是奇數，補一個引號
        if repaired.count('"') % 2 != 0:
            repaired += '"'
        
        # 統計括號數量並補齊
        open_braces = repaired.count('{')
        close_braces = repaired.count('}')
        if open_braces > close_braces:
            repaired += '}' * (open_braces - close_braces)
        
        return json.loads(repaired)
    except:
        pass

    # 4. 🛡️ [Repair Strategy B] 正則提取第一個 JSON 對象
    match = re.search(r'(\{.*\})', clean_str, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except:
            pass
    
    # 5. 🛡️ [Extreme Repair] 給 local 模型專用的暴力提取
    if "{" in clean_str:
        try:
            # 至少嘗試把開頭到最後一個 } 的部分抓出來
            last_brace = clean_str.rfind("}")
            if last_brace != -1:
                return json.loads(clean_str[:last_brace+1])
        except:
            pass

    logger.warning(f"⚠️ [JSON Failure] 無法修復 LLM 產出的 JSON: {raw_str[:50]}...")
    return default_value


# YouTube 標題垃圾詞（不該進 STT contextualStrings 的雜訊 token）
_TITLE_NOISE_TOKENS = frozenset({
    "official", "music", "video", "mv", "hd", "hq", "lyric", "lyrics",
    "audio", "live", "cover", "feat", "ft", "remaster", "remastered",
    "官方", "官方版", "官方完整版", "完整版", "高畫質", "字幕版", "動態歌詞",
    "歌詞版", "現場版", "演唱會",
})


def build_stt_context(base: str, game_dict: str, song_pairs: list[tuple[str, str]],
                      members: list[str], cap: int = 60) -> str:
    """組裝 STT contextualStrings（2026-06-13 動態擴充：歌名/歌手/活躍講者）。

    - 標題先按空白/括號類符號切 token，濾掉 YouTube 垃圾詞與超長 token（>12 字）
    - 去重保序、空白剔除、總數 cap（Apple contextualStrings 宜短小聚焦）
    - ⚠️ 鐵則：回傳的字串 caller 必須同步餵給 is_whisper_hallucination 過濾 echo-back
    """
    import re as _re

    out: list[str] = []
    seen: set[str] = set()

    def _add(token: str) -> None:
        token = token.strip()
        if not token or len(token) > 12 or "," in token:
            return
        if token.lower() in _TITLE_NOISE_TOKENS:
            return
        if token in seen or len(out) >= cap:
            return
        seen.add(token)
        out.append(token)

    for part in base.split(","):
        _add(part)
    for part in game_dict.split(","):
        _add(part)
    for title, artist in song_pairs:
        for field in (title, artist):
            for token in _re.split(r"[\s\[\]【】()（）「」『』/|\-_–—:：]+", field or ""):
                _add(token)
    for m in members:
        _add(m)
    return ",".join(out)


def is_whisper_hallucination(text: str, prompt: str) -> bool:
    """偵測 Whisper 幻覺：靜音或 TTS 殘影時，Whisper 會把 initial_prompt 內容吐回來。
    模式1：同一短語重複 3 次以上（e.g. 嗨馬文,嗨馬文,嗨馬文）
    模式2：主導 token 重複 ≥3 次且佔比 ≥55%（e.g. 嗨馬文,啵 嗨馬文,啵 嗨馬文）
    模式3：輸出完全由 prompt 關鍵詞組成（e.g. 嗨馬文,Hi Marvin）"""
    parts = [p.strip() for p in re.split(r'[,，。！.、\s]+', text) if p.strip()]
    if not parts:
        return False
    if len(parts) >= 3 and len(set(parts)) <= 1:
        return True
    if len(parts) >= 4:
        from collections import Counter
        top_token, top_count = Counter(parts).most_common(1)[0]
        if top_count >= 3 and top_count / len(parts) >= 0.55:
            return True
    prompt_tokens = {p.strip() for p in re.split(r'[,，。！.\s]+', prompt) if p.strip()}
    if len(parts) >= 2 and all(p in prompt_tokens for p in parts):
        return True
    return False

def parse_lyrics_sections(md_path: str) -> dict:
    """解析歌詞 .md，回傳 {section_name: lyrics_text}。
    支援兩種格式：
      - MD 格式：# [Verse 1: description] 前綴帶 # 號
      - 純文字格式：[Chorus] 獨立一行
    只保留中文歌詞行，跳過純英文的樂器說明 (...)。"""
    sections = {}
    current_section = None
    lines = []
    try:
        with open(md_path, encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.rstrip()
                # 剝除開頭的 # 與空白（MD 格式）
                stripped = line.lstrip("#").strip()
                if not stripped:
                    continue
                # 判斷是否為 section header：[Section Name: optional description]
                if stripped.startswith("[") and "]" in stripped:
                    if current_section is not None and lines:
                        sections[current_section] = "\n".join(lines).strip()
                    inner = stripped[1:stripped.index("]")]
                    section_name = inner.split(":")[0].strip()
                    current_section = section_name
                    lines = []
                elif stripped.startswith("("):
                    # 樂器演奏說明，跳過
                    continue
                elif current_section is not None:
                    lines.append(stripped)
        if current_section is not None and lines:
            sections[current_section] = "\n".join(lines).strip()
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning(f"⚠️ [Lyrics] 解析失敗 {md_path}: {e}")
    return {k: v for k, v in sections.items() if v}


def pick_lyrics_snippet(md_path: str, max_chars: int = 80) -> tuple[str, str] | tuple[None, None]:
    """從歌詞 .md 隨機挑一個段落，優先從 Chorus/Verse/Bridge 中隨機選，
    若無則從全部段落隨機選。回傳 (section_name, lyrics_text) 或 (None, None)。"""
    import random
    sections = parse_lyrics_sections(md_path)
    if not sections:
        return None, None
    preferred_prefixes = ["Chorus", "副歌", "Verse", "第一段", "第二段", "Bridge", "橋段"]
    preferred_keys = [k for k in sections if any(k.startswith(p) for p in preferred_prefixes)]
    pool = preferred_keys if preferred_keys else list(sections.keys())
    key = random.choice(pool)
    return key, sections[key][:max_chars]


