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
# 結束個人歌單：停止詞 + 歌單指涉詞（指名歌單才攔，純「停」留給一般 control_stop）
_STOP_WORDS = "停|關|別|不要|取消|結束|不用|換回|恢復|回到|改回"
_PLAYLIST_WORDS = "個人歌單|我的歌單|我的歌|連續播|輪播"
# 「回到一般/自動播放」說法（不含歌單詞也算）
_RESUME_WORDS = "換回|恢復|回到|改回"
_NORMAL_WORDS = "一般|正常|自動|平常|推薦|原本"


class PersonalShuffleAgent(DeclarativeIntentAgent):
    name = "personal_shuffle"
    # 正常對話與串流播放期間都活著；遊戲模式不該誤觸發
    mode_compatible = frozenset({"normal", "stream"})

    def __init__(self, controller):
        self.ctrl = controller

    def declare_intents(self) -> list[IntentSchema]:
        return [
            # stop 先宣告（first-match-wins）。0.96 > 一般 control_stop(0.95)，這樣「停掉
            # 我的歌單」會結束個人模式而非停掉所有音樂；純「停」不含歌單詞 → 不命中、留給
            # 一般 control_stop。
            IntentSchema(
                "personal_shuffle_stop", 0.96,
                patterns=[
                    f"(?=.*(?:{_STOP_WORDS}))(?=.*(?:{_PLAYLIST_WORDS}))",
                    f"(?=.*(?:{_RESUME_WORDS}))(?=.*(?:{_NORMAL_WORDS}))",
                ],
                reason_template="personal_shuffle_stop",
            ),
            IntentSchema(
                "personal_shuffle_start", 0.90,
                patterns=[f"(?=.*(?:{_LOOP_WORDS}))(?=.*(?:{_MINE_WORDS}))"],
                reason_template="personal_shuffle_start",
            ),
        ]

    def _cog(self):
        bot = getattr(self.ctrl, "bot", None)
        return bot.cogs.get("MusicCog") if bot is not None else None

    def make_handler(self, schema: IntentSchema, slots: dict, ctx: IntentContext):
        if schema.name == "personal_shuffle_stop":
            async def _stop() -> None:
                cog = self._cog()
                if cog is None:
                    return
                if cog.stop_personal_shuffle():
                    vc = cog._vc()
                    ch = getattr(vc, "active_text_channel", None) if vc is not None else None
                    if ch is not None:
                        try:
                            await ch.send("🎲 已結束個人歌單，回到一般推薦／主題歌單。")
                        except Exception:
                            pass
            return _stop

        speaker = ctx.speaker

        async def _start() -> None:
            cog = self._cog()
            if cog is None:
                return
            await cog.start_personal_shuffle(speaker)

        return _start
