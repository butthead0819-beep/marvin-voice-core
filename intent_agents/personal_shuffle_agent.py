"""PersonalShuffleAgent — 語音觸發「連續隨機播我的歌單」。

宣告式 IntentAgent（Template A）：trigger 是語音片語。雙 lookahead 強制同時出現
「連續/隨機/循環」類詞 + 「我的歌單/我點過」類詞，避免吃掉一般點歌（「播周杰倫的歌」）
或一般連播（「連續播放音樂」）。

handler 進 MusicCog.start_personal_shuffle(speaker)，由 cog 端建池→洗牌→一次墊一首
（不塞爆佇列、別人現場點歌照樣進得來）。停止走一般「停」指令 / 自然播完。
"""
from __future__ import annotations

from intent_agents.base import DeclarativeIntentAgent, IntentSchema
from intent_bus import IntentContext

# 「連續/重複播放」意圖詞
_LOOP_WORDS = "連續|一直|循環|輪播|不斷|重複|隨機"
# 「我自己的歌單」指涉詞（必須是『我的』，不接受別人或泛指）
_MINE_WORDS = "我的歌單|我點過|我的歌|我的愛歌|我常聽|個人歌單|我所有的歌|我之前點"


class PersonalShuffleAgent(DeclarativeIntentAgent):
    name = "personal_shuffle"
    # 正常對話與串流播放期間都活著；遊戲模式不該誤觸發
    mode_compatible = frozenset({"normal", "stream"})

    def __init__(self, controller):
        self.ctrl = controller

    def declare_intents(self) -> list[IntentSchema]:
        return [
            IntentSchema(
                "personal_shuffle_start", 0.90,
                patterns=[f"(?=.*(?:{_LOOP_WORDS}))(?=.*(?:{_MINE_WORDS}))"],
                reason_template="personal_shuffle_start",
            ),
        ]

    def make_handler(self, schema: IntentSchema, slots: dict, ctx: IntentContext):
        speaker = ctx.speaker

        async def _start() -> None:
            bot = getattr(self.ctrl, "bot", None)
            cog = bot.cogs.get("MusicCog") if bot is not None else None
            if cog is None:
                return
            await cog.start_personal_shuffle(speaker)

        return _start
