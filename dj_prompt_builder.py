"""DJ Prompt 統一建構器與規範中心 (Unified DJ Prompt Builder & Rules)。

作為整個系統所有 DJ 相關 Prompt 生成的單一真實來源 (Single Source of Truth)，
統一管理以下六大核心規範：
1. 長度與時間硬上限（45-55 字 / 8-9 秒 crossfade；報幕 20-23 字 / 6-7 秒）
2. 機器人觀察者視角（絕不用第一人稱將聽眾經歷說成自己的）
3. 防幻覺與素材約束（只用脈絡給的那一項素材，不腦補未給的事實）
4. 掛名嚴格依據脈絡（掛錯名比不掛名傷）
5. 不考驗聽眾記憶（不斷言「應該沒聽過」「是誰點的」這類只有聽眾自己知道的事）
6. 調性與社群風格（Threads 生活廢文共鳴與微無厘頭冷幽默，嚴禁大道理雞湯與假文青）
"""
from __future__ import annotations

from typing import Any, Dict
from persona_loader import load_dj_styles
from joke_examples import format_joke_examples_block, JOKE_TYPES

_DJ_STYLES = load_dj_styles()

# ── 核心安全護欄（Hardcoded Guardrails，不隨外部設定漂移）──────────────────
DJ_MATERIAL_GUARD = (
    "只用脈絡給的那一項素材寫，不要自己加新話題、不要同時講好幾件事；"
    "歌名最多提一次，可針對歌名巧妙串接情境，勾起聽眾對下一首歌的畫面與想聽的期待感。"
)

DJ_NAMING_GUARD = (
    "**掛名只能照脈絡**：只有脈絡明講「點播者」或「理由」裡出現的人才能提名字，"
    "絕不自己指定這首是誰點的、誰想聽的——掛錯名比不掛名傷。"
)

DJ_MEMORY_CLAIM_GUARD = (
    "**不考驗聽眾記憶**：聽眾自己聽過什麼、記不記得，只有他們自己知道——"
    "不要斷言「XX應該沒聽過」「XX一定聽過」這種聽眾記憶才能驗證的話，"
    "沒把握就用「比較少聽」這種保留語氣；脈絡沒明講是聽眾自己點播的，"
    "就別說成「這首是XX點的」，改用「希望XX喜歡」這種機器人自己推薦的說法。"
)

DJ_JOKE_STYLE_GUARD = (
    "**厭世冷笑話風格（跳脫平常暖場語氣的低頻彩蛋，本輪不受下面調性規則的「不諷刺、"
    "不憂鬱」限制）**：針對脈絡給的歌名／歌手，現編一個全新的「台灣式冷笑話」或「諧音梗」"
    f"（{JOKE_TYPES} 擇一或混搭，絕對不可照抄下面範例，僅供學習風格）：\n"
    f"{format_joke_examples_block()}\n"
    "笑話講完後，必須用馬文招牌的厭世嘆息收尾，把這則笑話的冷場感跟「宇宙萬物的徒勞」"
    "掛鉤（例如：『這笑話跟宇宙的壽命一樣尷尬...』）。"
)

FORBIDDEN_DJ_PHRASES = (
    "時光流動",
    "歲月靜好",
    "撫平心靈",
    "流淌的旋律",
    "人生就像一場旅行",
    "身為AI",
    "身為一個AI",
    "大家好我是",
    "這首歌送給",
    "這簡直就在說你",
    "這根本在說你",
    "許多人以為",
    "別以為",
    "這不只是",
    "其實它更",
)


def get_dj_unified_rules() -> Dict[str, Any]:
    """取得所有 DJ 統一規範字典。"""
    style = _DJ_STYLES.get("dj_interjection_style", {})
    return {
        "length_rule": style.get("length_rule", "**長度硬上限：45-55 中文字**（唸完約 9 秒）。"),
        "material_guard": DJ_MATERIAL_GUARD,
        "material_style_rule": style.get("material_style_rule", ""),
        "naming_guard": DJ_NAMING_GUARD,
        "memory_claim_guard": DJ_MEMORY_CLAIM_GUARD,
        "robot_pov_guard": style.get("robot_pov_rule", ""),
        "tone_rule": style.get("tone_rule", ""),
        "output_format_rule": style.get("output_format_rule", "只輸出台詞，不加引號、不加說明。"),
        "forbidden_phrases": FORBIDDEN_DJ_PHRASES,
    }


def build_dj_interjection_prompt(context: str) -> str:
    """建構歌曲 crossfade 空檔的 DJ 串場 Prompt（9秒 / 45-55 字）。"""
    rules = get_dj_unified_rules()
    return (
        f"你是 DJ Marvin，聽眾話不多的老朋友：熟悉他們的生活、懂他們的音樂品味。"
        f"你的個性是開口就切重點，一兩句話把他的事接到這首歌、說出歌名，就把舞台交給歌——"
        f"像朋友隨手遞一首歌過來：『這首給你』，不鋪陳、不解釋。"
        f"現在是兩首歌之間的空檔，你開口串場。\n\n"
        f"脈絡：\n{context}\n\n"
        "規則：\n"
        f"1. {rules['length_rule']}\n"
        f"2. {rules['material_guard']}{rules['material_style_rule']}\n"
        f"3. {rules['robot_pov_guard']}\n"
        f"4. {rules['naming_guard']}\n"
        f"5. {rules['memory_claim_guard']}\n"
        f"6. {rules['tone_rule']}\n"
        f"7. {rules['output_format_rule']}\n"
        "8. 脈絡裡標了【你熟悉他的生活】【你懂他的音樂品味】【你想跟他分享這首的原因】的是你知道的事，"
        "挑最有感的一件講就好，不用每一類都講；最後落在歌名上（說出《歌名》）就停。"
        "沒給的不要自己補。歌詞只能引用脈絡裡給的那一句。"
    )


def build_dj_joke_interjection_prompt(context: str) -> str:
    """建構 crossfade 空檔的「馬文式厭世冷笑話」插播 Prompt——DJ 串場的低頻彩蛋分支：
    安靜時段偶爾跳脫平常暖場人設，改用 marvin_joke 的厭世笑話風格（範例庫見
    joke_examples.py，兩處共用避免風格漂移）。長度/防幻覺/掛名/不考驗記憶護欄跟一般
    crossfade 串場一樣，但調性改用厭世嘆息收尾，取代平常「不諷刺不憂鬱」的暖場風格。
    """
    rules = get_dj_unified_rules()
    return (
        f"你是 DJ Marvin，在兩首歌 crossfade 的空檔講一個厭世冷笑話。\n\n"
        f"脈絡：\n{context}\n\n"
        "規則：\n"
        f"1. {rules['length_rule']}\n"
        f"2. {rules['material_guard']}\n"
        f"3. {rules['naming_guard']}\n"
        f"4. {rules['memory_claim_guard']}\n"
        f"5. {DJ_JOKE_STYLE_GUARD}\n"
        f"6. {rules['output_format_rule']}"
    )


def build_radio_now_playing_prompt(context: str) -> str:
    """建構電台即時報幕 Prompt（6-7秒 / 20-23 字）。"""
    template = _DJ_STYLES.get(
        "radio_now_playing",
        "你是專業電台 DJ，正在介紹下一首歌。\n\n脈絡：\n{context}\n\n規則：\n"
        "1. 內容要素（挑 2-3 個塞進一句）：歌名、歌手、年份、副歌或歌詞亮點、創作背景\n"
        "2. **20-23 中文字**，唸完約 6 秒，務必 7 秒內結束\n"
        "3. 專業 DJ 口吻，平實有溫度，不諷刺、不憂鬱、不裝深沉\n"
        "4. 只輸出台詞，不加引號、不加說明"
    )
    return template.format(context=context)


def build_audiophile_guide_prompt(song_label: str) -> str:
    """建構「聽覺放大鏡」導聆 Prompt——當 grounded 呼叫（audiophile_fetcher）的
    system_instruction 用，contents 只帶歌名/歌手 label。要求用 Google 查證專業
    樂評／錄音訪談／製作幕後，零幻覺：查不到可靠資料就只回「無」，接
    grounded_answer 的 L1 拒答 guard。"""
    return (
        f"你是馬文，正在為聽眾做一段「聽覺放大鏡」深度導聆，即將播放的歌是：{song_label}。\n\n"
        "【查證守門】一定要先實際執行 Google 搜尋，根據搜尋結果回答，不准只憑記憶。"
        "用 Google 搜尋這首歌的專業樂評、錄音訪談、製作幕後資料，只寫查得到、查證過的細節，"
        "不准腦補、不准無中生有；查不到可靠資料就只回一個字「無」。\n\n"
        "台詞要用三幕式結構寫成一段連貫口白，拒絕死板公式化：\n"
        "1. 故事破題與破除既定印象（~25 字）：開門見山直接切入，可從「製作幕後軼事」、「歌手真實唱腔紋理」"
        "或打破刻板印象任選一個角度自然開場，嚴禁套用「許多人以為/別以為…其實…」這類公式化反轉句型。\n"
        "2. 核心音軌細節與聽覺錨點（~55 字）：指出耳朵該聽什麼——"
        "聲場定位、樂器的獨特選用、和弦離調或突變瞬間、錄音裡的真實呼吸聲，任選查得到的一兩個具體細節。\n"
        "3. 進歌引導（~20 字）：用自然口吻引導聽眾戴上耳機或準備進歌，避免千篇一律的套話。\n\n"
        "**長度 90-110 個中文字**（唸完約 20 秒）。\n\n"
        f"禁止使用這些假文青套話與八股詞：{'、'.join(FORBIDDEN_DJ_PHRASES)}。\n\n"
        "只輸出台詞，不加引號、不加說明、不列來源。"
    )


def build_album_tracklist_prompt(artist: str, album: str) -> str:
    """/tour 曲目查證用 prompt。編號格式讓「無」開頭的歌名（例「無與倫比的美麗」）
    不撞 L1 拒答 guard（guard 只認整句開頭是「無」）。"""
    return (
        f"你是音樂資料查證員。用 Google 搜尋並查證 {artist} 的專輯《{album}》的官方曲目"
        "（以原版專輯為準，不含 bonus track）。\n"
        "照專輯曲序每行一首，格式固定為「1. 歌名」「2. 歌名」…，只寫歌名，"
        "不寫歌手、時長、說明或來源。\n"
        "一定要先實際執行 Google 搜尋，根據搜尋結果回答，不准只憑記憶。\n"
        "查不到、或無法確定是哪一張專輯，就只回一個字「無」，不准猜。"
    )


def build_stream_now_playing_prompt(context: str) -> str:
    """建構直播點播報幕 Prompt（6-7秒 / 20-23 字）。"""
    template = _DJ_STYLES.get(
        "stream_now_playing",
        "你是專業電台 DJ，介紹剛點播的這首歌。\n\n脈絡：\n{context}\n\n規則：\n"
        "1. 內容要素（挑 2-3 個）：歌名、歌手、年份、副歌或歌詞亮點。可順帶提點播者\n"
        "2. **20-23 中文字**，唸完約 6 秒，務必 7 秒內結束\n"
        "3. 專業 DJ 口吻，介紹給聽眾，不諷刺、不憂鬱\n"
        "4. 只輸出台詞，不加引號、不加說明"
    )
    return template.format(context=context)


def build_song_card_ingestion_prompt(song_label: str, lyrics: str = "") -> str:
    """建構「單次聚合多維度歌曲卡」Ingestion Prompt。

    單次 Grounding 呼叫：同時獲取聽覺幕後口白與歌詞刺點，避免分散呼叫產生的
    API 成本與速率限制。
    """
    lyrics_block = f"\n附帶歌詞參考（包含時間戳）：\n{lyrics}\n" if lyrics else ""
    return (
        f"你是音樂資料庫整編員與導聆專家，正在為歌曲《{song_label}》建立全方位多維度歌曲卡。\n\n"
        "【查證守門】一定要先實際執行 Google 搜尋，根據搜尋結果回答，不准只憑記憶。"
        "用 Google 搜尋這首歌的專業樂評、錄音訪談、幕後花絮，只寫查得到、查證過的細節，"
        "不准腦補；若該維度真的查無資料，請填寫「無」。若完全查不到這首歌任何資料，直接回覆「無」。\n\n"
        f"{lyrics_block}"
        "請嚴格依據以下兩段標籤與格式輸出，不得擅自更改標籤名稱：\n\n"
        "【聽覺與幕後】：一段三幕式深度聽覺口白（長度 90-110 個中文字）：\n"
        "  1. 破題與創作軼事（~25字）：開門見山切入創作背景、真實唱腔或打破刻板印象，嚴禁「許多人以為/別以為…其實…」等公式化反轉句型。\n"
        "  2. 核心音軌細節（~55字）：指出耳機裡最值得留意的具體細節（如聲場定位、特殊樂器、離調突變、環境呼吸聲）。\n"
        "  3. 進歌引導（~20字）：自然引導聽眾戴上耳機或準備進歌。\n\n"
        "【歌詞刺點】：從歌詞中挑選最刺痛或最具靈魂的一句，格式為：\n"
        "  句：[歌詞原文] | 析：[一語道破的情感暗流或矛盾痛點]\n"
        "  （若無歌詞或純演奏曲請填「無」）\n\n"
        f"全篇禁止使用這些假文青套話與八股詞：{'、'.join(FORBIDDEN_DJ_PHRASES)}。\n"
        "只依上述格式輸出內容，不加額外說明。"
    )


def _card_fields(raw: str, names: tuple[str, ...]) -> dict[str, str]:
    """切出「名：值」欄位；欄位可用 | 串在同一行或分行寫（實測兩種都有）。
    同名欄位第二次出現就停——LLM 偶爾給兩組標籤，只取第一組，避免標籤配到別組的情境。"""
    import re

    pat = re.compile(r"(?:^|\|)[ \t]*(" + "|".join(names) + r")[ \t]*[：:]", re.M)
    matches = list(pat.finditer(raw))
    out: dict[str, str] = {}
    for i, m in enumerate(matches):
        name = m.group(1)
        if name in out:
            break
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        out[name] = raw[m.end():end].strip()
    return out


def parse_song_card_response(raw_text: str) -> dict[str, Any] | None:
    """解析單次聚合歌曲卡 LLM 回應。
    
    回傳字典結構：
    {
        "audiophile_guide": str,
        "lyric_hook": {"quote": str, "subtext": str} | None,
    }
    若回應為「無」或缺少必要的【聽覺與幕後】段落，回傳 None。
    """
    import re

    if not raw_text or not isinstance(raw_text, str):
        return None
    cleaned = raw_text.strip()
    if cleaned == "無" or not cleaned:
        return None

    # 1. 聽覺與幕後（lookahead 仍防模型自己吐出已拔除的【社群熱評標籤】段落滲進導聆稿）
    guide_match = re.search(
        r"【聽覺與幕後】[：:]?\s*(.*?)(?=\n*【(?:社群熱評標籤|歌詞刺點)】|$)",
        cleaned,
        re.DOTALL,
    )
    if not guide_match:
        return None
    guide_text = guide_match.group(1).strip()
    if not guide_text or guide_text == "無":
        return None

    # 2. 歌詞刺點
    lyric_hook = None
    lyric_match = re.search(
        r"【歌詞刺點】[：:]?\s*(.*?)(?=\n*【[^】]+】|$)",
        cleaned,
        re.DOTALL,
    )
    if lyric_match:
        lyric_raw = lyric_match.group(1).strip()
        if lyric_raw and lyric_raw != "無":
            # 「時」仍列為欄位名只為切開舊格式回應，值丟掉：LLM 沒拿到同步歌詞時只能猜
            fields = _card_fields(lyric_raw, ("句", "時", "析"))
            quote = fields.get("句", "")
            subtext = fields.get("析", "")
            if quote and quote != "無":
                lyric_hook = {"quote": quote, "subtext": subtext}

    return {
        "audiophile_guide": guide_text,
        "lyric_hook": lyric_hook,
    }

