"""_resolve_requester_avatar：歌曲卡頭像解析。

2026-07-08 bug：我點的歌 overlay 卻是 Marvin 頭像——`getattr(vc,'channel')` 錯（vc 是
VoiceController cog、無 .channel）→ 找不到成員 → 退回 bot 頭像。修＝走 vc.voice_client.channel。
"""
from unittest.mock import MagicMock

from cogs.music_cog import MusicCog


def _cog_with_channel(members):
    bot = MagicMock()
    bot.cogs.get.return_value = None
    bot.user.display_avatar.url = "https://bot/marvin.png"
    cog = MusicCog(bot)
    vc = MagicMock()
    vc.voice_client.channel.members = members
    return cog, vc


def _member(name, avatar, is_bot=False):
    m = MagicMock()
    m.display_name = name
    m.bot = is_bot
    m.display_avatar.url = avatar
    return m


def test_marvin_recommend_uses_bot_avatar():
    cog, vc = _cog_with_channel([])
    assert cog._resolve_requester_avatar(vc, "Marvin推薦（點給大家）") == "https://bot/marvin.png"


def test_real_requester_uses_their_own_avatar():
    cog, vc = _cog_with_channel([
        _member("大肚", "https://u/dadu.png"),
        _member("狗與露", "https://u/gou.png"),
    ])
    assert cog._resolve_requester_avatar(vc, "狗與露") == "https://u/gou.png"   # 不是 bot 頭像


def test_requester_not_in_channel_falls_back_to_bot():
    cog, vc = _cog_with_channel([_member("別人", "https://u/x.png")])
    assert cog._resolve_requester_avatar(vc, "已離開的人") == "https://bot/marvin.png"
