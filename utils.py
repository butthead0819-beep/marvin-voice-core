import json
import re
import logging

logger = logging.getLogger(__name__)

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

# 🚀 喚醒詞清單中心化管理
# 排列原則：3 音節優先（聲學辨識度高、誤觸率低），再列 2 音節，最後 STT 容錯變體
WAKE_WORDS_LIST = [
    # 3 音節主力（最難誤觸，優先讓 regex 先比對到長串）
    "嗨馬文", "艾馬文", "艾瑪文", "阿姨文", "馬文同學",
    # 英文呼叫（在中文語流中極具辨識度）
    "hey marvin", "oh marvin", "marvin", "marv", "marwen", "mavin",
    # 2 音節主詞
    "馬文",
    # STT 容錯變體（馬文的音近誤識，排除高頻日常詞）
    "馬聞", "馬溫", "麻文", "馬問", "馬穩", "馬門", "馬萌",
    # ── 以下為「僅句首」喚醒詞（FAST_ONLY_WAKE_WORDS）────────────────────────
    # 這些詞在日常中文極常見（老馬識途、馬哥你好、杜比音效），
    # 不放入 force_intervene 的 pattern，只允許句首 fast_intervene。
    # 🗑️ 已移至 FAST_ONLY_WAKE_WORDS：馬哥、老馬、杜比
    # 🗑️ "龍蝦" 完全移除：NemoClaw 觸發由 _NEMOCLAW_RE 位置感知處理。
]

# 只允許句首觸發（fast_intervene），不進 force_intervene 的呼格比對
FAST_ONLY_WAKE_WORDS = [
    "馬哥", "老馬",   # 台灣口語常見稱謂，mid-sentence 歧義高
    "杜比",            # 杜比音效/杜比全景聲 是正常詞彙
]

def _load_wake_override() -> None:
    """從 records/wake_words_override.json 動態擴充 / 剔除喚醒詞（每日分析後自動更新）。"""
    try:
        from pathlib import Path
        _p = Path(__file__).parent / "records" / "wake_words_override.json"
        if not _p.exists():
            return
        data = json.loads(_p.read_text(encoding="utf-8"))
        for w in data.get("additions", []):
            if w and w not in WAKE_WORDS_LIST:
                WAKE_WORDS_LIST.append(w)
        for w in data.get("removals", []):
            if w in WAKE_WORDS_LIST:
                WAKE_WORDS_LIST.remove(w)
    except Exception:
        pass

_load_wake_override()

# WAKE_PATTERN: full list (WAKE_WORDS_LIST + FAST_ONLY_WAKE_WORDS) 用於 any_pos 和 fast_wake 偵測
_ALL_WAKE_WORDS = WAKE_WORDS_LIST + FAST_ONLY_WAKE_WORDS
WAKE_PATTERN = "|".join(_ALL_WAKE_WORDS)
# FORCE_WAKE_PATTERN: 只有低歧義詞才允許 mid-sentence + vocative → force_intervene
_FORCE_WAKE_PATTERN = "|".join(WAKE_WORDS_LIST)

def check_cleaned_text_for_wake(cleaned_text: str) -> bool:
    """[Track B] LLM 清洗後再次比對喚醒詞 (用於補充誤辨識情況，如『馬門』->『馬文』)"""
    wake_pattern = re.compile(rf'({WAKE_PATTERN})', re.IGNORECASE)
    return bool(wake_pattern.search(cleaned_text))

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
    if all(p in prompt_tokens for p in parts):
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


def pre_filter_speech(raw_text: str) -> dict:
    """[Operation Paranoid Android] 喚醒詞與情境關鍵詞的正則過濾

    回傳 action 語意：
      fast_intervene  — 句首喚醒詞，立即觸發（最可靠，假觸率最低）
      force_intervene — 低歧義詞在句前段 + 呼格後綴，立即觸發
      llm_verify      — 非句首且無呼格，送 Track B LLM 驗證後再決定
      process         — 情境緊急觸發（重複關鍵字）
      drop            — 無任何命中，丟棄
    """
    text = raw_text.strip()

    # 句首匹配（全詞表） → 立即喚醒（假觸機率最低）
    fast_wake_regex = re.compile(rf'^({WAKE_PATTERN})', re.IGNORECASE)

    # 非句首 + 呼格後綴 → 立即喚醒
    # ⚠️ 僅使用 _FORCE_WAKE_PATTERN（低歧義詞），排除「老馬/馬哥/杜比」等高頻日常詞
    # ⚠️ 加位置限制：喚醒詞必須在句首 6 字元以內，排除「我覺得說馬文可以...」等深層嵌入
    _VOCATIVE_SUFFIX = (
        r'[，,、\s]*'
        r'(?:你|幫|來|去|說|告訴|可以|能不能|快|給|接|查|播|開|關'
        r'|怎麼|多少|要|什麼|幫我|告我|解釋|唱|找|停|繼續|重複)'
    )
    force_wake_regex = re.compile(rf'({_FORCE_WAKE_PATTERN}){_VOCATIVE_SUFFIX}', re.IGNORECASE)

    # 任意位置有喚醒詞但不符合呼格 → 送 LLM 驗證，不直接觸發
    any_pos_regex = re.compile(rf'({WAKE_PATTERN})', re.IGNORECASE)

    context_triggers = ["完了", "死定", "救我", "救命", "怎麼辦", "找不到", "迷路", "好無聊", "好累", "炸了", "完蛋"]

    if fast_wake_regex.search(text):
        return {"action": "fast_intervene", "text": raw_text}

    m = force_wake_regex.search(text)
    # 位置限制：喚醒詞起始位置 ≤ 2 字元（約 1 個填充詞「哎/那個/好啦」）
    # 超過此位置視為深層被動嵌入（我覺得/書上說/我聽說…馬文），降為 llm_verify
    if m and m.start() <= 2:
        return {"action": "force_intervene", "text": raw_text}

    if any_pos_regex.search(text):
        # 中段出現喚醒詞：不直接觸發，由 Track B LLM 驗證呼叫意圖
        return {"action": "llm_verify", "text": raw_text}

    # 情境觸發：重複兩次以上緊急關鍵字（e.g. 完了完了）
    if any(re.search(rf"({re.escape(trigger)})\s*\1", text) for trigger in context_triggers):
        return {"action": "process", "text": raw_text}

    return {"action": "drop"}
