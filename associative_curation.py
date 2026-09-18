"""對話關聯性選曲與歌詞金句 DJ 模組 (Associative Lyric DJ)。

從近期語音對話中提煉生活畫面、物件雙關、環境氛圍或經典歌詞金句，
在歌曲播放的背景空檔動態選曲，並為下一首產生 45-55 字的 Marvin DJ 串場台詞。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AssociativePick:
    observed_topic: str
    target_lyric: str
    artist: str
    song: str
    reason: str
    dj_line: str


_ASSOCIATIVE_SYS_PROMPT = (
    "你是一位住在 Discord 語音伺服器裡的電台 DJ Marvin（風格：厭世微幽默、懂生活廢文、老朋友默契、絕不說教心靈雞湯）。\n"
    "這群聽眾剛才在語音頻道聊天，目前正在播歌。請根據他們剛才聊天的具體內容，為下一首歌做「巧妙的關聯性選曲」。\n\n"
    "【四大選曲靈感維度（優先尋找最具畫面感的一項）】：\n"
    "1. 經典歌詞畫面感／金句梗（最高優先）：\n"
    "   聽眾聊到的處境或心酸糗事，是否有哪首廣為人知的歌曲「副歌第一金句」能完美重現這個畫面？\n"
    "   （例：當電燈泡/格格不入 → 阿杜《他一定很愛你》『我應該在車底，不應該在車裡』；出門口訣被偷 → 美秀集團《手機錢包鑰匙菸》；尷尬看不下去 → 伍佰《Last Dance》『所以暫時將妳眼睛閉了起來』）\n"
    "2. 生活物件與文字雙關：\n"
    "   聊到的具體生活事物延伸出幽默關聯。（例：倒啤酒都是泡泡 → 鄧紫棋《泡沫》；吃宵夜泡麵 → 盧廣仲《早安晨之美》反差喜感）\n"
    "3. 環境氣候與體感共振：\n"
    "   當前天氣、溫度或作息狀態。（例：大暴雨全身濕 → 南拳媽媽《下雨天》；寒流很冷 → 陳綺貞《旅行的意義》熱帶島嶼反差）\n"
    "4. 社畜與日常反差：\n"
    "   加班崩潰、修機器被坑、想放假沒人陪。（例：開老遠修機器只收三千 → 滅火器《長途夜車》；想喝酒被全員放鳥 → 茄子蛋《浪子回頭》）\n\n"
    "【品管硬規則】：\n"
    "1. 必須是主流串流平台能找到的真實存在歌曲（真實歌手 + 真實歌名），不確定就不要瞎編。\n"
    "2. 絕對不要選『已播放歌單』裡的任何歌；優先考量聽眾的音樂口味歌手與曲風。\n"
    "3. 串場台詞（dj_line）長度【硬上限：45-55 個中文字】（唸完約 8-9 秒，超過 55 字會被截斷失敗）。以機器人觀察者視角吐槽剛才的對話並引出歌曲與歌詞梗，嚴禁大道理雞湯、假文青套話（如：時光流動、撫平心靈），不加引號。\n"
    "4. 只回傳 JSON 格式：\n"
    '{"observed_topic":"...","target_lyric":"...","artist":"...","song":"...","reason":"...","dj_line":"..."}'
)


def build_associative_prompt(
    utterances: list[dict],
    core_artists: list[str],
    exclude_titles: list[str],
    members: list[str],
) -> tuple[str, str]:
    """組裝關聯選曲的 System 與 User Prompt。"""
    dialogue_lines = [
        f"- {u.get('speaker', '有人')}: {u.get('text', '').strip()}"
        for u in utterances
        if u.get('text', '').strip()
    ]
    dialogue_block = "\n".join(dialogue_lines[-15:]) or "（無對話）"

    artists_str = "、".join(core_artists[:10]) or "（多樣華語流行/獨立樂團）"
    exclude_str = "、".join(exclude_titles[:50]) or "（無）"
    members_str = "、".join(members) or "群友"

    user_prompt = (
        f"【目前在場聽眾】：{members_str}\n"
        f"【聽眾平常的核心喜愛歌手】：{artists_str}\n"
        f"【已播過／不要重複的歌曲】：{exclude_str}\n\n"
        f"【剛才頻道的對話片段（按時間序）】：\n{dialogue_block}\n\n"
        "請根據剛才這段對話，選出一首最能產生神級共鳴或幽默關聯的歌曲，回傳 JSON。"
    )
    return _ASSOCIATIVE_SYS_PROMPT, user_prompt


def parse_associative_pick(raw_text: str) -> AssociativePick | None:
    """解析 LLM 輸出的 JSON，包含格式清理與邊界防禦。"""
    if not raw_text or not isinstance(raw_text, str):
        return None

    # 擷取最外層 JSON 區塊
    m = re.search(r"\{.*\}", raw_text, re.DOTALL)
    if not m:
        return None

    try:
        data = json.loads(m.group(0))
    except Exception:
        return None

    artist = str(data.get("artist", "")).strip()[:60]
    song = str(data.get("song", "")).strip()[:80]
    if not artist or not song:
        return None

    observed_topic = str(data.get("observed_topic", "")).strip()[:100]
    target_lyric = str(data.get("target_lyric", "")).strip()[:100]
    reason = str(data.get("reason", "")).strip()[:120]

    dj_line = str(data.get("dj_line", "")).strip()
    # 移除前後引號
    dj_line = dj_line.strip('"`\'「」')[:150]

    return AssociativePick(
        observed_topic=observed_topic,
        target_lyric=target_lyric,
        artist=artist,
        song=song,
        reason=reason,
        dj_line=dj_line,
    )


async def curate_associative_song(
    utterances: list[dict],
    core_artists: list[str],
    exclude_titles: list[str],
    members: list[str],
    *,
    call_fn: Callable[..., Coroutine[Any, Any, str | None]] | None = None,
    min_utterances: int = 2,
) -> AssociativePick | None:
    """動態對話關聯性選曲協調器（非同步）。

    對話語句數量不足或 LLM 呼叫失敗時，一律優雅回傳 None，供下游回退一般 autopilot。
    """
    valid_utts = [u for u in utterances if u.get("text", "").strip()]
    if len(valid_utts) < min_utterances:
        return None

    if call_fn is None:
        try:
            from llm_pool import call_paid_review
            call_fn = call_paid_review
        except ImportError:
            logger.warning("[AssociativeCuration] 無法載入 call_paid_review，選曲跳過")
            return None

    sys_p, user_p = build_associative_prompt(
        valid_utts,
        core_artists=core_artists,
        exclude_titles=exclude_titles,
        members=members,
    )

    try:
        raw_resp = await call_fn(user_p, system=sys_p)
    except Exception as e:
        logger.warning(f"[AssociativeCuration] LLM 呼叫異常: {e}")
        return None

    if not raw_resp:
        return None

    pick = parse_associative_pick(raw_resp)
    if pick:
        logger.info(
            f"🎵 [AssociativeCuration] 命中對話話題:『{pick.observed_topic}』"
            f" → 推薦《{pick.artist} - {pick.song}》（歌詞: {pick.target_lyric}）"
        )
    return pick
