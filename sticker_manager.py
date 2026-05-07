import re
import random
import time
import logging
import discord

logger = logging.getLogger(__name__)

# Marvin mood → Clyde 貼圖名稱關鍵字（按優先序，不分大小寫，部分匹配）
# 覆蓋 Clyde 包的常見命名風格
MOOD_TO_KEYWORDS: dict[str, list[str]] = {
    "contempt":   ["unamused", "whatever", "eye", "smirk", "meh"],
    "angry":      ["angry", "rage", "mad", "no", "grr"],
    "sad":        ["sad", "cry", "depressed", "dead", "sob"],
    "excited":    ["hype", "excited", "party", "yay", "tada"],
    "happy":      ["happy", "smile", "joy", "grin", "laugh", "lol"],
    "thinking":   ["think", "hmm", "ponder", "curious", "question"],
    "surprised":  ["wow", "surprised", "shock", "gasp"],
    "greeting":   ["wave", "hi", "hello", "hey"],
    "farewell":   ["bye", "wave", "peace", "later"],
    "cool":       ["cool", "sunglasses", "swag", "flex"],
    "love":       ["love", "heart", "blush"],
    "neutral":    ["thumbs", "ok", "sure", "fine"],
    "sleeping":   ["sleep", "zzz", "tired", "yawn"],
}

# 情緒回退鏈：找不到精確匹配時依序試下一個
FALLBACK_CHAIN: dict[str, list[str]] = {
    "contempt":  ["angry", "neutral"],
    "excited":   ["happy", "neutral"],
    "surprised": ["thinking", "neutral"],
    "sleeping":  ["neutral"],
    "cool":      ["happy", "neutral"],
    "love":      ["happy", "neutral"],
    "farewell":  ["greeting", "neutral"],
}


def infer_mood(response_text: str, toxicity: int, user_emotion: str = "neutral") -> str:
    """根據 Marvin 的 DNA 毒性值、說話者情緒與回應文字推斷此次回應的 mood key。"""
    text_lower = response_text.lower()

    # 規則 1：問號多 → thinking
    if response_text.count("?") + response_text.count("？") >= 2:
        return "thinking"

    # 規則 2：說話者情緒對應
    if user_emotion in ("frustrated", "angry"):
        return "contempt" if toxicity >= 6 else "sad"
    if user_emotion == "excited":
        return "excited" if toxicity <= 5 else "contempt"

    # 規則 3：DNA 毒性 → 基底情緒
    if toxicity >= 8:
        # 看回應是否有強攻擊性詞彙
        if re.search(r"(滾|廢物|白痴|蠢|算了|懶得|無聊|閉嘴)", text_lower):
            return "angry"
        return "contempt"
    if toxicity >= 5:
        return "neutral"
    if toxicity >= 3:
        return "happy"
    return "love"


class StickerManager:
    """依情緒選擇 Guild Sticker 發送至頻道，附帶頻道冷卻防止洗版。

    策略：
    1. 優先從 Bot 已加入的 Guild 搜尋名稱含 Clyde 關鍵字的 GuildSticker（可直接發送）。
    2. 若 Guild 沒有安裝 Clyde 貼圖，fallback 為情緒 emoji（純文字）。
    
    注意：StandardSticker（官方貼圖包）無法透過 Bot 的 channel.send() 發送，
    因此捨棄 fetch_sticker_packs() 方案。
    """

    COOLDOWN_SECONDS = 25  # 同一頻道最短間隔

    # 情緒 → emoji fallback（當無 Guild Sticker 時使用）
    MOOD_EMOJI_MAP: dict[str, str] = {
        "contempt":  "😒",
        "angry":     "😤",
        "sad":       "😞",
        "excited":   "✨",
        "happy":     "😊",
        "thinking":  "🤔",
        "surprised": "😮",
        "greeting":  "👋",
        "farewell":  "🌙",
        "cool":      "😎",
        "love":      "💜",
        "neutral":   "😑",
        "sleeping":  "💤",
    }

    def __init__(self):
        self._guild_stickers: dict[str, discord.GuildSticker] = {}  # name.lower() → GuildSticker
        self._mood_cache: dict[str, discord.GuildSticker | None] = {}
        self._last_sent: dict[int, float] = {}  # channel_id → timestamp
        self._emoji_mode: bool = False  # 若無 GuildSticker 則切換為 emoji 模式

    async def load(self, bot: discord.Client) -> None:
        """啟動時從所有 Guild 搜尋名稱含 'clyde' 的 GuildSticker 並索引。"""
        found = 0
        for guild in bot.guilds:
            try:
                # guild.stickers 是快取屬性（不耗 API），fetch_stickers() 才是 API 呼叫
                stickers = guild.stickers or await guild.fetch_stickers()
                for s in stickers:
                    if "clyde" in s.name.lower() or True:  # 收錄所有 Guild Sticker
                        self._guild_stickers[s.name.lower()] = s
                        found += 1
            except Exception as e:
                logger.warning(f"⚠️ [Sticker] 無法取得 {guild.name} 的貼圖: {e}")

        if found == 0:
            self._emoji_mode = True
            logger.warning("⚠️ [Sticker] 所有 Guild 均無可用 GuildSticker，切換為 Emoji 模式。")
        else:
            logger.info(
                f"🎭 [Sticker] 已從 Guild 載入 {found} 張 GuildSticker：\n"
                + ", ".join(list(self._guild_stickers.keys())[:20])
            )

    def _find_by_keywords(self, keywords: list[str]) -> discord.GuildSticker | None:
        """依優先關鍵字清單找第一個命中的 GuildSticker。"""
        for kw in keywords:
            for name, sticker in self._guild_stickers.items():
                if kw in name:
                    return sticker
        return None

    def pick(self, mood: str) -> discord.GuildSticker | None:
        """回傳指定情緒對應的 GuildSticker，找不到時回傳隨機一張。"""
        if not self._guild_stickers:
            return None
        if mood in self._mood_cache:
            return self._mood_cache[mood]

        # 嘗試主關鍵字
        sticker = self._find_by_keywords(MOOD_TO_KEYWORDS.get(mood, []))

        # 嘗試回退鏈
        if sticker is None:
            for fallback_mood in FALLBACK_CHAIN.get(mood, ["neutral"]):
                sticker = self._find_by_keywords(MOOD_TO_KEYWORDS.get(fallback_mood, []))
                if sticker:
                    break

        # 終極回退：隨機一張
        if sticker is None:
            sticker = random.choice(list(self._guild_stickers.values()))
            logger.debug(f"🎲 [Sticker] mood='{mood}' 無匹配，隨機選用：{sticker.name}")
        else:
            logger.debug(f"🎭 [Sticker] mood='{mood}' → {sticker.name}")

        self._mood_cache[mood] = sticker
        return sticker

    async def send(self, channel: discord.TextChannel, mood: str) -> bool:
        """發送對應情緒的 GuildSticker（或 emoji fallback）至頻道，有冷卻限制。"""
        now = time.time()
        if now - self._last_sent.get(channel.id, 0) < self.COOLDOWN_SECONDS:
            return False

        # Emoji Fallback 模式
        if self._emoji_mode:
            emoji = self.MOOD_EMOJI_MAP.get(mood, "😑")
            try:
                await channel.send(emoji)
                self._last_sent[channel.id] = now
                return True
            except Exception as e:
                logger.warning(f"⚠️ [Sticker] Emoji fallback 失敗：{e}")
            return False

        sticker = self.pick(mood)
        if sticker is None:
            return False

        try:
            await channel.send(stickers=[sticker])
            self._last_sent[channel.id] = now
            return True
        except discord.Forbidden:
            logger.warning("⚠️ [Sticker] 缺少 USE_EXTERNAL_STICKERS 權限，切換為 Emoji 模式。")
            self._emoji_mode = True
            # 降級重試
            try:
                emoji = self.MOOD_EMOJI_MAP.get(mood, "😑")
                await channel.send(emoji)
                self._last_sent[channel.id] = now
                return True
            except Exception:
                pass
        except discord.HTTPException as e:
            logger.warning(f"⚠️ [Sticker] 發送失敗：{e}")
        return False
