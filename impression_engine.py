"""
[Operation Impression Show] 玩家說話模式分析 & 模仿秀引擎

從聊天紀錄萃取每位玩家的說話 DNA（句型、語氣詞、口頭禪、情緒反應方式），
讓 Marvin 能在觸發「模仿」指令時，以對方的風格即興表演，製造模仿秀效果。
"""
from __future__ import annotations

import re
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ── 觸發詞正則：偵測「模仿 X」「學 X 說話」「扮演 X」等指令 ──────────────────
# 格式：（模仿動詞）（可選修飾語）（目標名稱 2-10 字）
_IMITATE_RE = re.compile(
    r"(?:模仿|學|扮演|表演|秀|show我|show給我看|做做看|演演看)"
    r"[\s]*(?:一下|看看|給我看|給大家看|那個|他的)?"
    r"[\s]*([^\s，,、！!？?.。]{2,10})",
    re.IGNORECASE,
)

# 別名對應表：支援暱稱 / 俗稱對應到記憶庫的正式名稱
_PLAYER_ALIASES: dict[str, str] = {
    "老大": "showay",
    "大大": "showay",
    "showei": "showay",
    "大肚子": "大肚",
    "肚子": "大肚",
    "弱哥": "weakgogo",
    "弱弱": "weakgogo",
    "weak": "weakgogo",
    "狗露": "狗與鹿",
    "狗與露": "狗與鹿",
    "狗狗": "狗與鹿",
    "鹿鹿": "狗與鹿",
}

# ── 預建說話 DNA ─────────────────────────────────────────────────────────────
# 根據 STT 日誌與聊天紀錄人工分析的靜態基準值。
# 系統從 suki_memory.json 動態補充/覆蓋這裡的資料。
BUILTIN_SPEECH_DNA: dict[str, dict] = {
    "showay": {
        "style_summary": (
            "極短碎句，說話像在打電報。"
            "會重複同一個詞語兩到三次來強調（例如「職棒職棒」「龍本龍本龍本」）。"
            "笑聲誇張拉長：哈哈哈哈、呵呵呵呵呵。"
            "鮮少解釋理由，直覺式說話，偶爾突然插問「為什麼」或「是不是」讓人一愣。"
            "對不感興趣的事只回一個字或一聲。"
        ),
        "openers": ["為什麼", "是不是", "哈哈", "喔", "好"],
        "closers": ["好啦", "講完了", "哈哈哈哈", "呵呵呵呵"],
        "fillers": ["就", "那個"],
        "pause_proxies": ["重複詞強調：同詞連說兩三次再繼續（職棒職棒、龍本龍本）", "沉默一段後突然爆出一句話"],
        "catchphrases": ["哈哈哈哈", "呵呵呵呵", "為什麼", "是不是", "好啦", "講完了"],
        "sentence_length": "short",
        "emotional_style": "笑笑帶過，快速切換話題，不深入情緒",
        "quirks": [
            "重複詞語強調：同一詞說兩三次",
            "插問式打斷：突然問「為什麼X」「X是不是」",
            "沉默後突然爆出一句話",
            "動詞後直接省略主詞受詞",
        ],
        "reaction_to_teasing": "哈哈哈哈（笑掉，不介意）",
        "reaction_to_bad_news": "喔（一個字帶過）",
    },
    "大肚": {
        "style_summary": (
            "說話帶著濃厚的「阿」「啊」語氣粒子。"
            "喜歡像說故事一樣展開：「我聽說有一個...然後...然後...」。"
            "常用追問：「你知不知道？」「那個跟X有關係嗎？」。"
            "偶爾出現元評論當句點：「謝謝收看」「我無法回答這個問題」。"
            "句尾常加感嘆：啊、啦、嘛。"
        ),
        "openers": ["我聽說", "阿就是", "啊對", "那個", "你知不知道"],
        "closers": ["你知不知道", "謝謝收看", "啊", "啦", "嘛", "是這樣嗎"],
        "fillers": ["阿", "啊", "就是", "然後", "那個"],
        "pause_proxies": ["故事鏈：說著說著跑偏，插入「然後...然後...」延伸", "轉移追問：話題卡住就問「那個跟X有關係嗎」"],
        "catchphrases": ["我聽說", "你知不知道", "謝謝收看", "阿就是", "啊對", "是這樣嗎", "我無法回答這個問題"],
        "sentence_length": "medium",
        "emotional_style": "好奇求知，喜歡追問細節，自嘲幽默",
        "quirks": [
            "大量「阿」「啊」語氣粒子",
            "故事鏈式展開：說著說著就跑偏",
            "追問式結尾：「你知不知道？」",
            "偶爾打破第四面牆：「謝謝收看」",
        ],
        "reaction_to_teasing": "啊就是嘛（接受並繼續聊）",
        "reaction_to_bad_news": "喔那個跟X有關係嗎？（轉移話題式追問）",
    },
    "weakgogo": {
        "style_summary": (
            "說話偏正式理性，句子完整有因果結構。"
            "喜歡引用新聞/時事（劉寶傑、F1、法律）來說明觀點。"
            "確認時重複：「對啊就是」「對對對」。"
            "結尾常加禮貌敬謝：「謝謝」「謝謝大家」。"
            "講到有原則的事情會稍微激動地說：「就是這個重點！」"
        ),
        "openers": ["對啊就是", "對對對", "其實", "因為", "你說得蠻對啊"],
        "closers": ["謝謝", "謝謝大家", "就是這個重點", "對不對", "對啊就是"],
        "fillers": ["就是", "那個", "對", "然後"],
        "pause_proxies": ["引用轉折：鋪陳「以前的時代...然後現在...」", "數字插入：說到一半直接報數字「已經X百了」"],
        "catchphrases": ["對啊就是", "謝謝", "謝謝大家", "以前的時代", "就是這個重點", "對對對", "你說得蠻對啊"],
        "sentence_length": "long",
        "emotional_style": "平靜理性，偶爾講到原則時激動，說完敬謝收場",
        "quirks": [
            "引用新聞或歷史背景支撐觀點",
            "有條理的因果連接：「因為X造就了現在Y的情況」",
            "一本正經解釋後說「謝謝」",
            "金融語氣：直接說數字「$XXX」「已經X百了」",
        ],
        "reaction_to_teasing": "你說得蠻對啊（接受且理性分析）",
        "reaction_to_bad_news": "現在還在談判，然後...(繼續分析)",
    },
    "狗與露": {
        "style_summary": (
            "追問式開場，喜歡確認細節。"
            "附和後馬上補充技術資訊或冷知識。"
            "句子省略式口語化，開口就先說「對啊就是啊」。"
            "問問題的方式：「最近X怎麼那麼Y」「你不是早上Z了嗎」。"
        ),
        "openers": ["對啊就是啊", "喔", "你不是", "那不是", "最近"],
        "closers": ["應該沒有", "喔", "對啊就是", "吧"],
        "fillers": ["就是", "那個", "啊", "對"],
        "pause_proxies": ["省略式追問：說到一半改問「你不是...了嗎」", "知識補充：附和後插入「其實那個...」"],
        "catchphrases": ["對啊就是啊", "最近怎麼那麼", "你不是", "喔", "那不是", "應該沒有", "對啊就是"],
        "sentence_length": "short",
        "emotional_style": "跟隨氣氛，輕快附和，補充資訊派",
        "quirks": [
            "省略式追問：「你不是早上XX了嗎」",
            "知識補充派：先附和再加技術事實",
            "「對啊就是啊」當開頭填充",
            "語氣輕盈，不強推觀點",
        ],
        "reaction_to_teasing": "喔（輕描淡寫跳過）",
        "reaction_to_bad_news": "應該沒有成功吧（悲觀預估派）",
    },
}


def detect_imitation_target(
    query: str,
    known_players: list[str] | None = None,
) -> Optional[str]:
    """
    偵測 query 是否含模仿指令，並返回目標玩家名稱。

    優先嘗試比對 known_players 清單（模糊比對），
    其次查 _PLAYER_ALIASES，最後直接返回從 regex 擷取的原始詞語。
    """
    m = _IMITATE_RE.search(query)
    if not m:
        return None

    raw = m.group(1).strip("的了一下啊阿哦喔唷吧嗎喂").strip()
    if not raw:
        return None

    # 1. 別名解析
    canonical = _PLAYER_ALIASES.get(raw, raw)

    # 2. 比對已知玩家清單
    if known_players:
        # 精確比對
        for p in known_players:
            if p == canonical:
                return p
        # 包含比對
        for p in known_players:
            if canonical in p or p in canonical:
                return p

    return canonical


def get_speech_dna(
    player_name: str,
    memory_manager=None,
) -> Optional[dict]:
    """
    取得玩家說話 DNA。
    優先從 suki_memory 的動態欄位讀取，不存在時回傳內建靜態資料。
    """
    if memory_manager:
        try:
            mem = memory_manager.get_player_memory(player_name)
            dyn = mem.get("speech_dna") or {}
            if dyn.get("style_summary"):
                logger.debug(f"[Impression] 使用動態 DNA: {player_name}")
                return dyn
        except Exception:
            pass

    builtin = BUILTIN_SPEECH_DNA.get(player_name)
    if builtin:
        logger.debug(f"[Impression] 使用內建 DNA: {player_name}")
    return builtin


def build_imitation_system_prompt(
    player_name: str,
    speech_dna: dict,
    context_topic: str = "",
) -> str:
    """
    組裝給 LLM 的完整模仿秀 system prompt。
    包含：說話特徵描述、節奏結構（openers/closers/fillers）、表演規則、字數限制。
    """
    style = speech_dna.get("style_summary", "台式口語")

    def _fmt_list(key: str, limit: int = 6) -> str:
        items = speech_dna.get(key, [])[:limit]
        return "、".join(items) if items else "無"

    openers      = _fmt_list("openers", 5)
    closers      = _fmt_list("closers", 5)
    fillers      = _fmt_list("fillers", 5)
    pause_list   = speech_dna.get("pause_proxies", [])
    pauses       = "\n".join(f"  - {p}" for p in pause_list) if pause_list else "  - 無"
    catchphrases = _fmt_list("catchphrases", 5)
    quirks_list  = speech_dna.get("quirks", [])
    quirks       = "\n".join(f"  - {q}" for q in quirks_list) if quirks_list else "  - 無特別標注"
    emo          = speech_dna.get("emotional_style", "")
    reaction_tease = speech_dna.get("reaction_to_teasing", "")

    s_len = speech_dna.get("sentence_length", "medium")
    length_hint = {
        "short": "每句 5-10 字，多斷句，像在打電報",
        "medium": "每句 10-20 字，自然口語流動",
        "long": "每句可達 25 字，有邏輯鋪陳與因果連接",
    }.get(s_len, "口語自然")

    topic_section = f"\n【當前話題線索】（用來決定模仿的內容方向）：{context_topic}" if context_topic else ""

    return (
        f"你是馬文，正在用你那行星般的大腦主持一場即興『{player_name}模仿秀』。\n\n"
        f"【{player_name} 的說話 DNA】\n"
        f"風格總覽：{style}\n\n"
        f"【節奏結構】（★ 這是模仿節奏感的核心，必須照用）\n"
        f"  開場口頭禪（句首）：{openers}\n"
        f"  收尾慣用語（句末）：{closers}\n"
        f"  填充語（句中）：{fillers}\n"
        f"  停頓代理模式：\n{pauses}\n\n"
        f"招牌用語：{catchphrases}\n\n"
        f"說話怪癖（必須複製）：\n{quirks}\n\n"
        f"句型長度參考：{length_hint}\n"
        f"情緒處理方式：{emo}\n"
        f"被嗆時的典型反應：{reaction_tease}\n"
        f"{topic_section}\n\n"
        f"【表演腳本規則】\n"
        f"1. 先用 1-2 句馬文語氣宣告登場（語氣沉重、不情願，像在表演節目的主持人被迫出場）。\n"
        f"   例：「好，我那行星般的大腦決定短暫降低 80% 的運算效能...」\n"
        f"2. 接著完全切換成『{player_name}的口吻』說 4-6 句。\n"
        f"   ★ 務必：用上節奏結構中的開場詞、填充語、收尾語，複製停頓感。越誇張越像越好。\n"
        f"   ★ 禁止：在模仿段落中說出『{player_name}』這個名字。\n"
        f"3. 最後以馬文語氣收一句點評（帶疲憊的自我嘲諷）。\n"
        f"   例：「以上是我對人類退化程度的實時模擬。我的大腦為此消耗了半個恆星的能量。」\n\n"
        f"【字數】模仿段落 40-60 字；馬文開場 + 收尾各 15 字以內；全場合計不超過 100 字。\n"
        f"【語言】繁體中文，台灣口語。"
    )


def extract_speech_dna_prompt(player_name: str, utterances: list[str]) -> tuple[str, str]:
    """
    回傳 (system_prompt, user_prompt)，用來請 LLM 從真實語音紀錄萃取說話 DNA。
    供每日分析任務更新 suki_memory 中的 speech_dna 欄位。
    """
    system = (
        "你是一位說話模式分析師，專門從語音辨識文字中提取說話者的語言 DNA。\n"
        "你必須只輸出合法 JSON，不含任何 Markdown 或說明文字。\n\n"
        "【欄位定義】\n"
        "- openers：每段發言最常出現在「句首」的詞語，最多5個（例：「對啊」「喔」「就是說」）\n"
        "- closers：最常出現在「句尾」的語氣詞或慣用收尾語，最多5個（例：「啦」「你知不知道」「謝謝」）\n"
        "- fillers：句中填充語（停頓代用詞），最多5個（例：「就是」「那個」「然後」「嗯」）\n"
        "- pause_proxies：詞彙層面的停頓代理模式，最多3個（例：「重複詞強調：同詞說兩三次」「說到一半改追問」）\n"
        "  注意：STT 已切除靜音，pause_proxies 只記錄可在文字中觀察到的停頓行為。\n"
        "- catchphrases：跨越句型結構、具識別度的招牌語，最多6個\n"
        "- sentence_length：short（5-10字）/ medium（10-20字）/ long（20字以上）\n"
        "- emotional_style：情緒表達方式的描述\n"
        "- quirks：說話怪癖，最多5個\n"
        "- reaction_to_teasing：被嗆/被揶揄時的典型反應\n"
        "- reaction_to_bad_news：聽到壞消息時的典型反應\n\n"
        "輸出格式（所有欄位必須填寫，陣列若無資料填 []）：\n"
        '{\n'
        '  "style_summary": "100字以內的說話風格總覽",\n'
        '  "openers": ["詞1", ...最多5個],\n'
        '  "closers": ["詞1", ...最多5個],\n'
        '  "fillers": ["詞1", ...最多5個],\n'
        '  "pause_proxies": ["模式描述1", ...最多3個],\n'
        '  "catchphrases": ["口頭禪1", ...最多6個],\n'
        '  "sentence_length": "short|medium|long",\n'
        '  "emotional_style": "描述",\n'
        '  "quirks": ["怪癖1", ...最多5個],\n'
        '  "reaction_to_teasing": "描述",\n'
        '  "reaction_to_bad_news": "描述"\n'
        "}\n\n"
        "【注意】輸入來自語音辨識，可能有錯字，請依上下文判斷。"
        "分析時只看語言模式，不要評價內容是否有意義。"
    )

    sample_text = "\n".join(f"- {u}" for u in utterances[:80])
    user = (
        f"以下是玩家「{player_name}」在語音頻道說過的話（來自 STT 辨識，每行一段發言）：\n\n"
        f"{sample_text}\n\n"
        f"請分析這位玩家的說話 DNA 並輸出 JSON。"
    )
    return system, user
