"""Ack 模板 registry — 一份宣告驅動所有 ack 的渲染與播放。

統一前：5 支 render 腳本（generate_acks / _en / _music / _music_fail /
render_status）各寫一份，外加 voice_controller 內 _play_ack_sound /
_play_nemoclaw_ack / _play_status_ack / _play_random_filler 各自分支。
維護痛點：每加一類 ack 就要新腳本 + 新播放方法 + 問使用者要台詞。

統一後：
- **加新 ack = registry 加一條 AckCategory（必要時加一個 AckPool），零新程式碼。**
- scripts/render_acks.py 掃 POOLS 補缺檔（skip-existing）。
- VoiceController._play_ack(category) 查 CATEGORIES 套播放政策。

兩層分離（關鍵）：
- **AckPool**：要預渲染的一組音檔（一個 asset 目錄 + 台詞清單）。
- **AckCategory**：一個播放語義（指向 pool + 播放政策）。
  assets/acks 同一 pool 被 wake / nemoclaw / filler 三 category 共用，
  但 prewarm / hotswap / lock / barge-in 政策各異 → 必須分離。

新增 ack 的台詞語氣慣例（Claude 自行撰寫、不需問使用者）：
- 馬文本人聲：短促、厭世、帶點不耐或宇宙級虛無；繁中 ≤6 字、urgent 類 ≤5 字。
- urgent=True 代表久候/降級/插話場景：要能在音樂中切入（走熱切換），字數更短。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


# ---------------------------------------------------------------------------
# Voice（與 tts_engine.SukiTTS 預設對齊）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AckVoice:
    voice: str
    rate: str
    pitch: str


VOICES: dict[str, AckVoice] = {
    "marvin_zh": AckVoice("zh-TW-YunJheNeural", "-20%", "-15Hz"),
    "marvin_en": AckVoice("en-GB-RyanNeural", "-20%", "-15Hz"),
}


# ---------------------------------------------------------------------------
# Pool（要預渲染的音檔）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AckPool:
    key: str
    directory: str
    voice_key: str
    items: tuple[tuple[str, str], ...]  # (text, filename)


def _status_items() -> tuple[tuple[str, str], ...]:
    """4 狀態 × 2 tier × 2 變體 = 16；檔名 {state}_{tier}_{i}.mp3。"""
    acks: dict[str, dict[str, tuple[str, str]]] = {
        "thinking":  {"first": ("等我想想", "容我一下"), "second": ("還在想", "快好了")},
        "searching": {"first": ("查資料中", "我去查"),   "second": ("還在查", "快查到")},
        "busy":      {"first": ("線路塞爆", "排隊中"),   "second": ("還在排", "快輪到")},
        "fallback":  {"first": ("切備援腦", "降級中"),   "second": ("備援頂著", "將就用")},
    }
    out: list[tuple[str, str]] = []
    for state, tiers in acks.items():
        for tier, variants in tiers.items():
            for i, text in enumerate(variants, 1):
                out.append((text, f"{state}_{tier}_{i}.mp3"))
    return tuple(out)


POOLS: dict[str, AckPool] = {
    "wake_zh": AckPool("wake_zh", "assets/acks", "marvin_zh", (
        ("嗯。。。", "ack_1.mp3"),
        ("好吧。。。", "ack_2.mp3"),
        ("我在聽。", "ack_3.mp3"),
        ("說來聽聽。。。", "ack_4.mp3"),
        ("嗯嗯。。。", "ack_5.mp3"),
        ("繼續說。。。", "ack_6.mp3"),
        ("收到了。。。", "ack_7.mp3"),
        ("好。。。 我在。", "ack_8.mp3"),
        ("嗯，我明白了。。。", "ack_9.mp3"),
        ("（歎氣）。。。 說吧。", "ack_10.mp3"),
    )),
    "wake_en": AckPool("wake_en", "assets/acks_en", "marvin_en", (
        ("Hmm...", "ack_en_1.mp3"),
        ("Fine...", "ack_en_2.mp3"),
        ("I'm listening.", "ack_en_3.mp3"),
        ("Go on...", "ack_en_4.mp3"),
        ("Yes, yes...", "ack_en_5.mp3"),
        ("Continue...", "ack_en_6.mp3"),
        ("Acknowledged...", "ack_en_7.mp3"),
        ("I'm here. Unfortunately.", "ack_en_8.mp3"),
        ("What is it this time...", "ack_en_9.mp3"),
        ("...sigh. Speak.", "ack_en_10.mp3"),
    )),
    "music": AckPool("music", "assets/acks/music", "marvin_zh", (
        ("挑歌中", "music_ack_01.mp3"),
        ("加入歌單", "music_ack_02.mp3"),
        ("這首好聽", "music_ack_03.mp3"),
        ("太會挑了", "music_ack_04.mp3"),
        ("好品味", "music_ack_05.mp3"),
        ("馬上放", "music_ack_06.mp3"),
        ("立刻播", "music_ack_07.mp3"),
        ("我來放", "music_ack_08.mp3"),
        ("開始播", "music_ack_09.mp3"),
        ("收到", "music_ack_10.mp3"),
        ("識貨", "music_ack_11.mp3"),
        ("點對了", "music_ack_12.mp3"),
        ("你內行", "music_ack_13.mp3"),
        ("對味", "music_ack_14.mp3"),
        ("我懂你", "music_ack_15.mp3"),
        ("沒問題", "music_ack_16.mp3"),
        ("找到了", "music_ack_17.mp3"),
        ("好歌", "music_ack_18.mp3"),
        ("經典款", "music_ack_19.mp3"),
        ("好選擇", "music_ack_20.mp3"),
    )),
    "music_fail": AckPool("music_fail", "assets/acks/music_fail", "marvin_zh", (
        ("無法播放", "music_fail.mp3"),
    )),
    "status": AckPool("status", "assets/acks_status", "marvin_zh", _status_items()),
}


# ---------------------------------------------------------------------------
# Category（播放政策）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AckCategory:
    key: str
    pool_by_lang: Mapping[str, str]      # lang -> pool key；"*" 為預設
    urgent: bool = False                 # True → 音樂中走熱切換注入，不打斷音樂
    prewarm_tts: bool = False            # 播放時並行暖 edge-tts（ack 預告 Marvin 回應）
    use_lock: bool = False               # True → 走 playback_lock 序列化（nemoclaw / status）
    skip_if_busy: bool = False           # True → vc 播放中直接跳過（不疊、不等）
    wait_if_busy: float = 0.0            # >0 → 等 vc 空檔最多 N 秒再播（skip_if_busy=False 時）
    await_completion: bool = False       # True → 播完才返回（ack_done event + 5s timeout）
    empty_fallback_pool: str | None = None  # 該 pool 空 → 改用此 pool key
    text_fallback: tuple[str, ...] = ()  # 連檔都沒 → 即時合成這些字（中文）
    text_fallback_en: tuple[str, ...] = ()  # en speaker 的即時合成 fallback
    variant_glob: bool = False           # True → 檔名前綴是 variant（status 的 {state}_{tier}）


CATEGORIES: dict[str, AckCategory] = {
    # 一般喚醒 ack：厭世馬文，預告即將回應 → 暖 TTS；音樂中走熱切換；等空檔後播完才返回
    "wake": AckCategory(
        "wake", {"zh": "wake_zh", "en": "wake_en"},
        urgent=True, prewarm_tts=True, wait_if_busy=4.0, await_completion=True,
        text_fallback=("嗯。。。", "好吧。。。", "我在聽。", "嗯嗯。。。"),
        text_fallback_en=("Hmm...", "Fine...", "I'm listening.", "Yes..."),
    ),
    # 點歌成功確認：DJ 口吻短句；音樂中切入；子 pool 空退回 wake；等空檔後播完才返回
    "music": AckCategory(
        "music", {"*": "music"},
        urgent=True, empty_fallback_pool="wake_zh", wait_if_busy=4.0, await_completion=True,
    ),
    # 點歌失敗：不切音樂；子 pool 空退回 wake
    "music_fail": AckCategory(
        "music_fail", {"*": "music_fail"},
        urgent=False, empty_fallback_pool="wake_zh", wait_if_busy=4.0, await_completion=True,
    ),
    # NemoClaw 處理中 ack：走 lock、播放中跳過，不暖 TTS、不切音樂
    "nemoclaw": AckCategory(
        "nemoclaw", {"zh": "wake_zh", "en": "wake_en"},
        urgent=False, use_lock=True, skip_if_busy=True,
    ),
    # LLM 久候/降級狀態安撫：音樂中切入；走 lock、播放中跳過；檔名前綴 = {state}_{tier}
    "status": AckCategory(
        "status", {"*": "status"},
        urgent=True, use_lock=True, skip_if_busy=True, variant_glob=True,
    ),
    # 延遲遮蔽 filler：故意不鎖、僅空檔插隊
    "filler": AckCategory(
        "filler", {"zh": "wake_zh", "en": "wake_en"},
        urgent=False, use_lock=False, skip_if_busy=True,
    ),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def pool_for(category_key: str, *, lang: str = "zh") -> AckPool:
    cat = CATEGORIES[category_key]
    pool_key = cat.pool_by_lang.get(lang) or cat.pool_by_lang.get("*")
    if pool_key is None:
        pool_key = next(iter(cat.pool_by_lang.values()))
    return POOLS[pool_key]


def voice_for(pool_key: str) -> AckVoice:
    return VOICES[POOLS[pool_key].voice_key]


def glob_pattern(category_key: str, *, lang: str = "zh", variant: str | None = None) -> str:
    """回該 category 對應 pool 目錄下的 mp3 glob pattern。

    variant_glob 的 category（status）需傳 variant（如 "searching_first"）→
    只挑該前綴的檔；否則挑整個 pool。
    """
    pool = pool_for(category_key, lang=lang)
    cat = CATEGORIES[category_key]
    if cat.variant_glob and variant:
        return f"{pool.directory}/{variant}_*.mp3"
    return f"{pool.directory}/*.mp3"
